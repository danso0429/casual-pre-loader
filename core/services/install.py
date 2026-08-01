import json
import logging
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from time import perf_counter
from typing import Callable, Optional

from valve_parsers import PCFFile

from core.backup_manager import prepare_working_copy
from core.constants import (
    BACKUP_MAINMENU_FOLDER,
    CUSTOM_VPK_NAME,
    CUSTOM_VPK_NAMES,
    CUSTOM_VPK_SPLIT_PATTERN,
    DX8_LIST,
)
from core.folder_setup import folder_setup
from core.handlers.file_handler import FileHandler, copy_config_files, generate_config
from core.handlers.paint_handler import disable_paints, enable_paints
from core.handlers.pcf_handler import (
    apply_particle_restore,
    check_parents,
    prepare_particle_restore,
    restore_particle_files,
    update_materials,
)
from core.handlers.skybox_handler import (
    handle_skybox_mods,
    remove_staged_skybox_vmts,
    restore_skybox_files,
)
from core.handlers.sound_handler import SoundHandler
from core.services.install_state import (
    InstallStateStore,
    capture_addon_inventory,
    capture_install_inputs,
    make_request_header,
)
from core.operations.file_processors import (
    check_game_type,
    game_type,
    get_from_custom_dir,
    initialize_pcf,
)
from core.operations.for_the_love_of_god_add_vmts_to_your_mods import (
    generate_missing_vmt_files,
)
from core.operations.mdl_relocate import relocate_mdl_paths
from core.operations.pcf_compress import remove_duplicate_elements
from core.operations.pcf_rebuild import extract_elements, load_particle_system_map
from core.operations.vgui_preload import patch_mainmenuoverride
from core.quickprecache.precache_list import make_precache_list
from core.quickprecache.quick_precache import QuickPrecache
from core.util.file import check_writable, copy, delete, move
from core.util.perf import StageTimer
from core.util.profiled_vpk import create_profiled_vpk
from core.util.pcf_path_walk import apply_particle_selections as stage_particle_selections
from core.util.vpk import get_vpk_name

log = logging.getLogger()

ProgressCallback = Callable[[int, str], None]
MAX_FILE_IO_WORKERS = 8
COPY_BATCH_SIZE = 128


def _io_worker_count(task_count: int) -> int:
    platform_limit = MAX_FILE_IO_WORKERS if os.name == "nt" else 1
    return min(platform_limit, max(1, task_count))


def _build_staging_plan(files_to_copy, patched_dir: Path, vpk_dir: Path):
    """Keep only the last selected source for each deterministic destination."""
    by_destination = {}
    for src_path, addon_dir, addon_index, src_size in files_to_copy:
        rel_path = src_path.relative_to(addon_dir)
        destination_root = patched_dir if src_path.suffix.lower() == ".pcf" else vpk_dir
        dest_path = destination_root / rel_path
        by_destination[dest_path] = (
            src_path,
            dest_path,
            addon_index,
            src_size,
            f"{addon_dir.name}/{rel_path.as_posix()}",
        )
    return sorted(
        by_destination.values(),
        key=lambda task: task[1].as_posix().casefold(),
    )


class InstallService:
    def __init__(self):
        self.sound_handler = SoundHandler()
        self.cancel_requested = False

    def request_cancel(self):
        self.cancel_requested = True

    def _check_cancelled(self):
        if self.cancel_requested:
            raise Exception("Installation cancelled by user")

    @staticmethod
    def is_modified(tf_path: str) -> bool:
        if not tf_path:
            return False
        gameinfo_path = Path(tf_path) / 'gameinfo.txt'
        return check_game_type(gameinfo_path) if gameinfo_path.exists() else False

    @staticmethod
    def cleanup_huds(custom_dir: Path) -> None:
        # clean up old HUDs that we installed (they have mod.json with preloader_installed flag)
        items_to_delete = []
        for item in custom_dir.iterdir():
            if item.is_dir() and not item.name.startswith('_'):
                mod_json = item / 'mod.json'
                if mod_json.exists():
                    try:
                        with open(mod_json, 'r') as f:
                            mod_info = json.load(f)
                            if mod_info.get('type', '').lower() == 'hud' and mod_info.get('preloader_installed', False):
                                items_to_delete.append(item)
                    except json.JSONDecodeError:
                        log.warning(f"Invalid JSON in {mod_json}", exc_info=True)

        for item in items_to_delete:
            delete(item)

    def install(
        self,
        tf_path: Path | str,
        selected_addons: list[str],
        on_progress: Optional[ProgressCallback] = None,
        apply_particle_selections: Optional[Callable[[], None]] = None,
        disable_paint_colors: bool = False,
        show_console_on_startup: bool = True,
        fix_mdl_paths: bool = True,
        skip_quickprecache: bool = False,
        game_target: str = "Team Fortress 2",
        particle_selections: Optional[dict[str, str]] = None,
        ) -> bool:
        """
        Install selected addons to the game directory.

        Args:
            tf_path: Path to the tf/ directory
            selected_addons: List of addon directory names to install
            on_progress: Callback for progress updates (percent, message)
            apply_particle_selections: Callback to apply particle selections from UI
            disable_paint_colors: Whether to disable paint colors
            show_console_on_startup: Whether to show console on startup
        """

        self.cancel_requested = False
        timer = StageTimer(
            log,
            "install",
            report_path=folder_setup.install_performance_file,
        )
        state_store = InstallStateStore(folder_setup.install_state_file)

        def progress(pct: int, msg: str):
            if on_progress:
                on_progress(pct, msg)

        request_header = None
        reusable_external_custom_paths = set()
        precache_models_for_state = None
        direct_game_files_reused = False
        is_tf2 = game_target == "Team Fortress 2"
        if particle_selections is not None:
            request_header = make_request_header(
                selected_addons,
                particle_selections,
                disable_paint_colors=disable_paint_colors,
                show_console_on_startup=show_console_on_startup,
                fix_mdl_paths=fix_mdl_paths,
                skip_quickprecache=skip_quickprecache,
                game_target=game_target,
            )
        else:
            state_store.clear(tf_path)

        addon_inventory = capture_addon_inventory(
            selected_addons,
            addons_dir=folder_setup.addons_dir,
            profiler=timer,
            operation_category="scan_addon",
        )
        total_files = 0
        total_bytes = 0
        files_to_copy = []
        hud_addons = {}

        for addon in addon_inventory.addons:
            if not addon.exists:
                continue

            mod_json_path = addon.directory / "mod.json"
            if mod_json_path.is_file():
                try:
                    with mod_json_path.open("r", encoding="utf-8") as file:
                        mod_info = json.load(file)
                    if mod_info.get("type", "").lower() == "hud":
                        addon_key = addon.name.lower()
                        if addon_key in hud_addons:
                            raise Exception(
                                "There are 2 mods that have directory names which "
                                "resolve to the same case-insensitive name:\n"
                                f"'{hud_addons[addon_key].name}'\n'{addon.directory.name}'"
                            )
                        hud_addons[addon_key] = addon.directory
                        continue
                except json.JSONDecodeError:
                    log.warning(f"Invalid JSON in {mod_json_path}", exc_info=True)

            for file in addon.files:
                src_path = file.path
                rel_path = file.relative
                if src_path.name in {"mod.json", "sound.cache"}:
                    continue
                if (
                    rel_path.parts[0] == "scripts"
                    and len(rel_path.parts) >= 2
                    and "sound" in src_path.name.lower()
                    and src_path.suffix == ".txt"
                ):
                    continue
                total_files += 1
                total_bytes += file.size
                source_label = f"{addon.directory.name}/{rel_path.as_posix()}"
                timer.record_inventory("selected-addon", source_label, file.size)
                files_to_copy.append(
                    (src_path, addon.directory, addon.index, file.size)
                )

        self._check_cancelled()
        timer.checkpoint(
            "scan_addons",
            addons=len(selected_addons),
            files=total_files,
            bytes=total_bytes,
            huds=len(hud_addons),
            workers=addon_inventory.workers,
        )

        captured_inputs = None
        if particle_selections is not None:
            captured_inputs = capture_install_inputs(
                selected_addons,
                particle_selections,
                disable_paint_colors,
                include_direct_game=is_tf2,
                profiler=timer,
                addon_inventory=addon_inventory,
            )
            is_current, reason = state_store.evaluate(
                tf_path,
                request_header,
                selected_addons,
                particle_selections,
                profiler=timer,
                captured_inputs=captured_inputs,
            )
            timer.checkpoint("check_install_state")
            log.info("Install state result=%s", reason)
            if is_current:
                progress(100, "Mods are already up to date")
                timer.finish()
                return False
            reusable_external_custom_paths = state_store.reusable_external_custom_paths(
                tf_path,
                request_header,
                profiler=timer,
            )
            log.info(
                "Reusing finalized external custom files count=%d",
                len(reusable_external_custom_paths),
            )
            if game_target == "Team Fortress 2":
                direct_game_files_reused = state_store.can_reuse_direct_game_files(
                    tf_path,
                    request_header,
                    selected_addons,
                    particle_selections,
                    disable_paint_colors,
                    profiler=timer,
                    captured_inputs=captured_inputs,
                )
                log.info("Reusing direct game VPK patches=%s", direct_game_files_reused)

        try:
            file_handler = None
            base_default_pcf = None
            base_default_parents = None
            particle_restore_plan = None
            if is_tf2:
                working_vpk_path = Path(tf_path) / get_vpk_name(tf_path)
                if not check_writable(working_vpk_path):
                    raise PermissionError("Please close TF2 before installing.")
                if not direct_game_files_reused:
                    try:
                        particle_restore_plan = prepare_particle_restore(
                            tf_path,
                            profiler=timer,
                        )
                    finally:
                        timer.checkpoint(
                            "preflight_particle_backups",
                            required=True,
                        )
                    file_handler = FileHandler(str(working_vpk_path))
                    base_default_pcf, base_default_parents = initialize_pcf(folder_setup.temp_to_be_referenced_dir)
                else:
                    timer.checkpoint(
                        "preflight_particle_backups",
                        required=False,
                    )
            progress(0, "Installing addons...")
            timer.checkpoint("initialize")

            custom_dir = Path(tf_path) / 'custom'
            custom_dir.mkdir(exist_ok=True)

            tf_path_obj = Path(tf_path)

            if is_tf2:
                self.cleanup_huds(custom_dir)

                for addon_name, addon_dir in hud_addons.items():
                    hud_dest = custom_dir / addon_name
                    if hud_dest.exists():
                        log.info(f'{hud_dest} already exists, skipping as to not overwrite possible user-modified files')
                        continue
                    with timer.measure("copy_hud", addon_dir.name):
                        copy(addon_dir, hud_dest)

                    hud_mod_json = hud_dest / 'mod.json'
                    if hud_mod_json.exists():
                        try:
                            with open(hud_mod_json, 'r') as f:
                                mod_info = json.load(f)
                            mod_info['preloader_installed'] = True
                            with open(hud_mod_json, 'w') as f:
                                json.dump(mod_info, f, indent=2)
                        except json.JSONDecodeError:
                            log.warning(f"Invalid JSON in {hud_mod_json}, skipping preloader_installed flag", exc_info=True)
            timer.checkpoint("install_huds", huds=len(hud_addons))

            self._check_cancelled()
            if is_tf2 and not direct_game_files_reused:
                with timer.measure("restore_skybox_files", "game VPK"):
                    restore_skybox_files(tf_path)
                apply_particle_restore(particle_restore_plan, profiler=timer)
                with timer.measure("restore_paint_files", "game VPK"):
                    enable_paints(tf_path)
            timer.checkpoint("restore_game_files", reused=direct_game_files_reused)

            self._check_cancelled()

            if particle_selections is not None:
                stage_particle_selections(particle_selections, profiler=timer)
            elif apply_particle_selections:
                apply_particle_selections()
            timer.checkpoint("apply_particle_selections")

            # dest_path -> addon load-order index, used by mdl_relocate to
            # resolve per-file collisions when merging un-prefixed mod content
            # into a destination that another mod already shipped pre-prefixed.
            file_origin: dict[Path, int] = {}

            if files_to_copy:
                staging_plan = _build_staging_plan(
                    files_to_copy,
                    folder_setup.temp_to_be_patched_dir,
                    folder_setup.temp_to_be_vpk_dir,
                )
                staged_files = len(staging_plan)
                staged_bytes = sum(task[3] for task in staging_plan)
                workers = _io_worker_count(staged_files)
                batches = [
                    staging_plan[index:index + COPY_BATCH_SIZE]
                    for index in range(0, staged_files, COPY_BATCH_SIZE)
                ]
                for _src_path, dest_path, addon_index, _src_size, _label in staging_plan:
                    file_origin[dest_path] = addon_index

                progress_range = 25
                completed_files = 0
                progress(10, f"Installing addons... (0/{staged_files} files)")

                def copy_batch(batch):
                    started_at = perf_counter()
                    batch_bytes = 0
                    for src_path, dest_path, _addon_index, src_size, _label in batch:
                        self._check_cancelled()
                        copy(src_path, dest_path)
                        batch_bytes += src_size
                    return len(batch), batch_bytes, perf_counter() - started_at

                with ThreadPoolExecutor(
                    max_workers=workers,
                    thread_name_prefix="preloader-copy",
                ) as executor:
                    for batch_index, (batch_count, batch_bytes, duration) in enumerate(
                        executor.map(copy_batch, batches),
                        start=1,
                    ):
                        timer.record_operation(
                            "copy_addon_batch",
                            f"batch={batch_index} files={batch_count}",
                            duration,
                            size_bytes=batch_bytes,
                        )
                        completed_files += batch_count
                        current_progress = 10 + int(
                            (completed_files / staged_files) * progress_range
                        )
                        progress(
                            current_progress,
                            f"Installing addons... ({completed_files}/{staged_files} files)",
                        )
                timer.checkpoint(
                    "stage_addon_files",
                    bytes=staged_bytes,
                    files=staged_files,
                    source_files=total_files,
                    workers=workers,
                )

                if is_tf2:
                    progress(35, "Processing sound mods...")
                    backup_scripts_dir = folder_setup.backup_dir / 'scripts'

                    vpk_paths = []
                    misc_vpk = tf_path_obj / "tf2_sound_misc_dir.vpk"
                    if misc_vpk.exists():
                        vpk_paths.append(misc_vpk)
                    vo_vpks = list(tf_path_obj.glob("tf2_sound_vo_*_dir.vpk"))
                    vpk_paths.extend(vo_vpks)

                    sound_result = self.sound_handler.process_temp_sound_mods(
                        folder_setup.temp_to_be_vpk_dir,
                        backup_scripts_dir,
                        vpk_paths,
                        profiler=timer,
                    )
                    if sound_result:
                        progress(50, sound_result['message'])
                timer.checkpoint("process_sounds")

                self._check_cancelled()

                if is_tf2:
                    if direct_game_files_reused:
                        remove_staged_skybox_vmts(folder_setup.temp_to_be_vpk_dir)
                    else:
                        handle_skybox_mods(folder_setup.temp_to_be_vpk_dir, tf_path)

                if is_tf2 and disable_paint_colors and not direct_game_files_reused:
                    progress(52, "Disabling paint colors...")
                    disable_paints(tf_path)
            timer.checkpoint(
                "patch_skyboxes_and_paints",
                reused=direct_game_files_reused,
            )

            if is_tf2 and not direct_game_files_reused:
                duplicate_effects = [
                    "item_fx.pcf",
                    "halloween.pcf",
                    "bigboom.pcf",
                    "dirty_explode.pcf",
                ]
                for duplicate_effect in duplicate_effects:
                    target_path = folder_setup.temp_to_be_patched_dir / duplicate_effect
                    if not target_path.exists():
                        source_path = folder_setup.temp_to_be_referenced_dir / duplicate_effect
                        target_path.parent.mkdir(parents=True, exist_ok=True)
                        if source_path.exists():
                            extract_elements(PCFFile(source_path).decode(),
                                             load_particle_system_map(folder_setup.particle_system_map_file)
                                             [f'particles/{target_path.name}']).encode(target_path)

                if (folder_setup.temp_to_be_patched_dir / "blood_trail.pcf").exists():
                    move(folder_setup.temp_to_be_patched_dir / "blood_trail.pcf",
                         folder_setup.temp_to_be_patched_dir / "npc_fx.pcf")

                particle_files = list(folder_setup.temp_to_be_patched_dir.glob("*.pcf"))
                dx8_files = sum(1 for pcf_file in particle_files if pcf_file.stem in DX8_LIST)
                total_files = len(particle_files) + dx8_files
                start_progress = 55
                progress_range = 25
                completed_files = 0
                progress(start_progress, f"Processing particle files... (0/{total_files})")

                for pcf_file in particle_files:
                    self._check_cancelled()

                    base_name = pcf_file.name
                    particle_started = timer.start_operation()
                    try:
                        particle_size = pcf_file.stat().st_size
                    except OSError:
                        particle_size = 0

                    mod_pcf = PCFFile(pcf_file).decode()

                    if base_name != base_default_pcf.input_file.name and check_parents(mod_pcf, base_default_parents):
                        timer.end_operation(
                            "process_particle_file",
                            base_name,
                            particle_started,
                            size_bytes=particle_size,
                        )
                        continue

                    if base_name == base_default_pcf.input_file.name:
                        mod_pcf = update_materials(base_default_pcf, mod_pcf)

                    processed_pcf = remove_duplicate_elements(mod_pcf)

                    if pcf_file.stem in DX8_LIST:
                        dx_80_name = pcf_file.stem + "_dx80.pcf"
                        file_handler.process_file(dx_80_name, processed_pcf)

                        completed_files += 1
                        current_progress = start_progress + int((completed_files / total_files) * progress_range)
                        progress(current_progress, f"Processing particle files... ({completed_files}/{total_files})")

                    file_handler.process_file(base_name, processed_pcf)
                    pcf_file.unlink()

                    completed_files += 1
                    current_progress = start_progress + int((completed_files / total_files) * progress_range)
                    progress(current_progress, f"Processing particle files... ({completed_files}/{total_files})")
                    timer.end_operation(
                        "process_particle_file",
                        base_name,
                        particle_started,
                        size_bytes=particle_size,
                    )
            elif not is_tf2:
                particle_files = list(folder_setup.temp_to_be_patched_dir.glob("*.pcf"))
                if particle_files:
                    particles_dir = folder_setup.temp_to_be_vpk_dir / 'particles'
                    particles_dir.mkdir(parents=True, exist_ok=True)

                    total_files = len(particle_files)
                    start_progress = 50
                    progress_range = 30
                    progress(start_progress, f"Copying particle files... (0/{total_files})")

                    for i, pcf_file in enumerate(particle_files):
                        self._check_cancelled()

                        try:
                            particle_size = pcf_file.stat().st_size
                        except OSError:
                            particle_size = 0
                        with timer.measure(
                            "copy_particle_file",
                            pcf_file.name,
                            size_bytes=particle_size,
                        ):
                            move(pcf_file, particles_dir / pcf_file.name)

                        current_progress = start_progress + int(((i + 1) / total_files) * progress_range)
                        progress(current_progress, f"Copying particle files... ({i + 1}/{total_files})")
            else:
                particle_files = list(folder_setup.temp_to_be_patched_dir.glob("*.pcf"))
            timer.checkpoint(
                "process_particles",
                files=len(particle_files),
                reused=direct_game_files_reused,
            )

            self._check_cancelled()

            progress(80, "Making custom VPK")

            game_type(Path(tf_path) / 'gameinfo.txt', uninstall=False)

            if is_tf2:
                backup_mainmenu_folder = custom_dir / BACKUP_MAINMENU_FOLDER
                delete(backup_mainmenu_folder, not_exist_ok=True)

            for custom_vpk in CUSTOM_VPK_NAMES:
                vpk_path = custom_dir / custom_vpk
                cache_path = custom_dir / (custom_vpk + ".sound.cache")
                if vpk_path.exists():
                    vpk_path.unlink()
                if cache_path.exists():
                    cache_path.unlink()

            custom_content_dir = folder_setup.temp_to_be_vpk_dir
            copy_config_files(custom_content_dir)

            if is_tf2:
                patch_mainmenuoverride(tf_path)
                if fix_mdl_paths:
                    progress(78, "Relocating model material paths...")
                    relocate_mdl_paths(custom_content_dir, file_origin=file_origin)
                generate_missing_vmt_files(custom_content_dir, tf_path)
            timer.checkpoint("prepare_custom_content")

            vpk_input_files = 0
            vpk_input_bytes = 0
            if custom_content_dir.exists():
                for input_path in custom_content_dir.glob("**/*"):
                    if not input_path.is_file():
                        continue
                    try:
                        input_size = input_path.stat().st_size
                    except OSError:
                        input_size = 0
                    vpk_input_files += 1
                    vpk_input_bytes += input_size
                    timer.record_inventory(
                        "custom-vpk",
                        input_path.relative_to(custom_content_dir).as_posix(),
                        input_size,
                    )
            timer.checkpoint(
                "inventory_custom_vpk",
                files=vpk_input_files,
                bytes=vpk_input_bytes,
            )

            for split_file in custom_dir.glob(f"{CUSTOM_VPK_SPLIT_PATTERN}*.vpk"):
                split_file.unlink()
                cache_file = custom_dir / (split_file.name + ".sound.cache")
                if cache_file.exists():
                    cache_file.unlink()

            if custom_content_dir.exists() and any(custom_content_dir.iterdir()):
                split_size = 2 ** 31
                vpk_base_path = custom_dir / CUSTOM_VPK_NAME.replace('.vpk', '')

                custom_content_dir.mkdir(parents=True, exist_ok=True)
                if not create_profiled_vpk(
                    custom_content_dir,
                    vpk_base_path,
                    split_size,
                    timer,
                ):
                    raise Exception("Failed to create custom VPK")
            timer.checkpoint("build_custom_vpk")

            self._check_cancelled()

            if is_tf2:
                precache_model_count = 0
                precache_reused = False
                precache = QuickPrecache(
                    str(Path(tf_path).parents[0]),
                    debug=False,
                    progress_callback=on_progress,
                )
                quick_precache_path = custom_dir / "_QuickPrecache.vpk"
                old_quick_precache_path = custom_dir / "QuickPrecache.vpk"

                if skip_quickprecache:
                    log.info("Skipping QuickPrecache scan/build (skip_quickprecache=True)")
                    precache_models_for_state = set()
                else:
                    progress(85, "Scanning for models to precache...")

                    precache_prop_set = make_precache_list(
                        str(Path(tf_path).parents[0]),
                        profiler=timer,
                    )
                    precache_models_for_state = precache_prop_set
                    precache_model_count = len(precache_prop_set)
                    if request_header is not None:
                        precache_reused = state_store.can_reuse_precache(
                            tf_path,
                            request_header,
                            precache_prop_set,
                            profiler=timer,
                        )

                if not precache_reused:
                    # Clear stale output when the desired model list changed or
                    # QuickPrecache was explicitly disabled.
                    precache.flush_files()
                    if quick_precache_path.exists():
                        quick_precache_path.unlink()
                    if old_quick_precache_path.exists():
                        old_quick_precache_path.unlink()

                    if not skip_quickprecache and precache_models_for_state:
                        precache.run(
                            model_list=precache_models_for_state,
                            flush_existing=False,
                        )
                        copy(folder_setup.install_dir / 'core/quickprecache/_QuickPrecache.vpk', custom_dir / '_QuickPrecache.vpk')
                else:
                    log.info("Reusing QuickPrecache outputs models=%d", precache_model_count)

                self._check_cancelled()
                timer.checkpoint(
                    "quickprecache",
                    models=precache_model_count,
                    reused=precache_reused,
                )

                progress(95, "Configuring...")

                has_mastercomfig = False
                for item in custom_dir.iterdir():
                    if item.is_file() and item.suffix == '.vpk' and item.name.startswith('mastercomfig'):
                        has_mastercomfig = True
                        break

                needs_quickprecache = (custom_dir / "_QuickPrecache.vpk").exists()

                config_content = generate_config(has_mastercomfig, needs_quickprecache, show_console_on_startup)

                custom_vpk_path = custom_dir / CUSTOM_VPK_NAME.replace('.vpk', '_dir.vpk')
                if custom_vpk_path.exists():
                    vpk_handler = FileHandler(str(custom_vpk_path))
                    vpk_handler.process_file('cfg/w/config.cfg', config_content.encode('utf-8'))
            timer.checkpoint("configure")

            progress(97, "Finalizing...")

            get_from_custom_dir(
                custom_dir,
                skip_paths=reusable_external_custom_paths,
                profiler=timer,
            )
            timer.checkpoint(
                "finalize_custom_content",
                reused=len(reusable_external_custom_paths),
            )

            if request_header is not None:
                save_inputs = captured_inputs
                if is_tf2 and not direct_game_files_reused:
                    # Particle preflight can add verified runtime backups. Refresh
                    # only the small non-addon inputs while reusing the addon tree.
                    save_inputs = capture_install_inputs(
                        selected_addons,
                        particle_selections,
                        disable_paint_colors,
                        include_direct_game=True,
                        profiler=timer,
                        addon_inventory=addon_inventory,
                    )
                state_store.save_current(
                    tf_path,
                    request_header,
                    selected_addons,
                    particle_selections,
                    precache_models=precache_models_for_state,
                    profiler=timer,
                    captured_inputs=save_inputs,
                )
                timer.checkpoint("save_install_state")

            progress(100, "Installation complete")
            return True

        finally:
            try:
                prepare_working_copy()
            finally:
                timer.checkpoint("reset_working_copy")
                timer.finish()

    def uninstall(self, tf_path: str, on_progress: Optional[ProgressCallback] = None, game_target: str = "Team Fortress 2"):
        # resets everything
        InstallStateStore(folder_setup.install_state_file).clear(tf_path)
        def progress(pct: int, msg: str):
            if on_progress:
                on_progress(pct, msg)

        try:
            prepare_working_copy()
            custom_dir = Path(tf_path) / 'custom'
            custom_dir.mkdir(exist_ok=True)

            tf_path_obj = Path(tf_path)
            is_tf2 = game_target == "Team Fortress 2"

            game_type(Path(tf_path) / 'gameinfo.txt', uninstall=True)

            if is_tf2:
                self.cleanup_huds(custom_dir)
                restore_skybox_files(tf_path)
                restore_particle_files(tf_path)
                enable_paints(tf_path)

                QuickPrecache(str(Path(tf_path).parents[0]), debug=False).run(flush=True)
                quick_precache_path = custom_dir / "_QuickPrecache.vpk"
                if quick_precache_path.exists():
                    quick_precache_path.unlink()

                quick_precache_cache = custom_dir / "_quickprecache.vpk.sound.cache"
                if quick_precache_cache.exists():
                    quick_precache_cache.unlink()

                old_quick_precache_path = custom_dir / "QuickPrecache.vpk"
                if old_quick_precache_path.exists():
                    old_quick_precache_path.unlink()

                backup_mainmenu_folder = custom_dir / BACKUP_MAINMENU_FOLDER
                delete(backup_mainmenu_folder, not_exist_ok=True)

            for custom_vpk in CUSTOM_VPK_NAMES:
                vpk_path = custom_dir / custom_vpk
                cache_path = custom_dir / (custom_vpk + ".sound.cache")
                if vpk_path.exists():
                    vpk_path.unlink()
                if cache_path.exists():
                    cache_path.unlink()

            for split_file in custom_dir.glob(f"{CUSTOM_VPK_SPLIT_PATTERN}*.vpk"):
                split_file.unlink()
                cache_file = custom_dir / (split_file.name + ".sound.cache")
                if cache_file.exists():
                    cache_file.unlink()

        finally:
            prepare_working_copy()
