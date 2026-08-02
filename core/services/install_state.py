import hashlib
import json
import logging
import os
import stat
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path
from time import perf_counter
from typing import TYPE_CHECKING, Callable

from core.constants import BACKUP_MAINMENU_FOLDER, CUSTOM_VPK_NAME
from core.folder_setup import folder_setup
from core.util.vpk import get_vpk_name
from core.version import VERSION

log = logging.getLogger()

if TYPE_CHECKING:
    from core.util.perf import StageTimer

INSTALL_STATE_SCHEMA = 1
INSTALL_RECIPE_VERSION = 2
DIRECT_GAME_COMPATIBLE_RECIPE_UPGRADES = {(1, 2)}
MAX_ADDON_SCAN_WORKERS = 8
CONTENT_HASH_CHUNK_SIZE = 1024 * 1024


@dataclass(frozen=True)
class AddonInventoryFile:
    path: Path
    relative: Path
    size: int
    mtime_ns: int
    ctime_ns: int


@dataclass(frozen=True)
class AddonInventoryEntry:
    index: int
    name: str
    directory: Path
    exists: bool
    files: tuple[AddonInventoryFile, ...]


@dataclass(frozen=True)
class AddonInventory:
    addons: tuple[AddonInventoryEntry, ...]
    workers: int = 1


@dataclass(frozen=True)
class CapturedInstallInputs:
    sources: list[list]
    direct_game_inputs: list[list] | None


def _measure(profiler, category: str, label: str):
    if profiler is None:
        return nullcontext()
    return profiler.measure(category, label)


def _request_identity(request: dict) -> dict:
    if not isinstance(request, dict):
        return {}
    identity = dict(request)
    identity.pop("app_version", None)
    return identity


def _stable_file_entries_match(previous: object, current: object) -> bool:
    """Compare portable file identity while ignoring copy-specific ctime."""
    if (
        not isinstance(previous, list)
        or not isinstance(current, list)
        or len(previous) != len(current)
    ):
        return False

    def stable(entry):
        if (
            isinstance(entry, list)
            and len(entry) == 4
            and isinstance(entry[0], str)
            and isinstance(entry[1], int)
            and isinstance(entry[2], int)
            and isinstance(entry[3], int)
        ):
            return entry[:3]
        return entry

    return [stable(entry) for entry in previous] == [
        stable(entry) for entry in current
    ]


def _direct_game_recipes_are_compatible(previous: dict, current: dict) -> bool:
    previous_recipe = previous.get("recipe")
    current_recipe = current.get("recipe")
    return previous_recipe == current_recipe or (
        previous_recipe,
        current_recipe,
    ) in DIRECT_GAME_COMPATIBLE_RECIPE_UPGRADES


def make_request_header(
    selected_addons: list[str],
    particle_selections: dict[str, str],
    *,
    disable_paint_colors: bool,
    show_console_on_startup: bool,
    fix_mdl_paths: bool,
    skip_quickprecache: bool,
    game_target: str,
) -> dict:
    return {
        "recipe": INSTALL_RECIPE_VERSION,
        "app_version": VERSION,
        "game_target": game_target,
        "selected_addons": list(selected_addons),
        "particle_selections": dict(sorted(particle_selections.items())),
        "options": {
            "disable_paint_colors": disable_paint_colors,
            "show_console_on_startup": show_console_on_startup,
            "fix_mdl_paths": fix_mdl_paths,
            "skip_quickprecache": skip_quickprecache,
        },
    }


def _file_entry(path: Path, label: str) -> list:
    try:
        stat = path.stat()
    except OSError:
        return [label, "missing"]
    return [label, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns]


def _content_file_entry(path: Path, label: str) -> list:
    """Capture portable file identity for application-bundled inputs."""
    digest = hashlib.sha256()
    size = 0
    try:
        with path.open("rb") as file:
            while chunk := file.read(CONTENT_HASH_CHUNK_SIZE):
                digest.update(chunk)
                size += len(chunk)
    except OSError:
        return [label, "missing"]
    return [label, size, digest.hexdigest()]


def _inventory_file_entry(file: AddonInventoryFile, label: str) -> list:
    return [label, file.size, file.mtime_ns, file.ctime_ns]


def capture_addon_inventory(
    selected_addons: list[str],
    *,
    addons_dir: Path | None = None,
    profiler: "StageTimer | None" = None,
    operation_category: str = "state_addon_inventory",
    scan_workers: int | None = None,
) -> AddonInventory:
    """Read each selected addon tree once and retain its file metadata."""
    if addons_dir is None:
        addons_dir = folder_setup.addons_dir
    default_workers = MAX_ADDON_SCAN_WORKERS if os.name == "nt" else 1
    requested_workers = default_workers if scan_workers is None else max(1, scan_workers)
    workers = min(requested_workers, max(1, len(selected_addons)))

    def capture_one(job):
        index, addon_name = job
        addon_dir = addons_dir / addon_name
        started_at = perf_counter()
        files = []
        total_bytes = 0

        if addon_dir.is_dir():
            for root, dir_names, file_names in os.walk(addon_dir):
                dir_names.sort(key=str.casefold)
                file_names.sort(key=str.casefold)
                root_path = Path(root)
                for file_name in file_names:
                    path = root_path / file_name
                    try:
                        file_stat = path.stat()
                    except OSError:
                        continue
                    if not stat.S_ISREG(file_stat.st_mode):
                        continue
                    relative = path.relative_to(addon_dir)
                    files.append(
                        AddonInventoryFile(
                            path=path,
                            relative=relative,
                            size=file_stat.st_size,
                            mtime_ns=file_stat.st_mtime_ns,
                            ctime_ns=file_stat.st_ctime_ns,
                        )
                    )
                    total_bytes += file_stat.st_size

        files.sort(key=lambda item: item.relative.as_posix().casefold())
        return (
            AddonInventoryEntry(
                index=index,
                name=addon_name,
                directory=addon_dir,
                exists=addon_dir.is_dir(),
                files=tuple(files),
            ),
            total_bytes,
            perf_counter() - started_at,
        )

    jobs = list(enumerate(selected_addons))
    if workers == 1:
        results = [capture_one(job) for job in jobs]
    else:
        with ThreadPoolExecutor(
            max_workers=workers,
            thread_name_prefix="preloader-addon-scan",
        ) as executor:
            results = list(executor.map(capture_one, jobs))

    addons = []
    for addon, total_bytes, duration in results:
        addons.append(addon)
        if profiler is not None:
            profiler.record_operation(
                operation_category,
                f"{addon.name} files={len(addon.files)}",
                duration,
                size_bytes=total_bytes,
            )

    return AddonInventory(addons=tuple(addons), workers=workers)


def _source_entries_from_inventory(inventory: AddonInventory) -> list[list]:
    entries = []
    for addon in inventory.addons:
        label = f"addons/{addon.index}/{addon.name}"
        if not addon.exists:
            entries.append([label, "missing"])
            continue
        entries.append([f"{label}/", "directory"])
        for file in addon.files:
            if file.path.name == "sound.cache":
                continue
            entries.append(
                _inventory_file_entry(
                    file,
                    f"{label}/{file.relative.as_posix()}",
                )
            )
    return entries


def _direct_entries_from_inventory(inventory: AddonInventory) -> list[list]:
    entries = []
    for addon in inventory.addons:
        label = f"direct_addons/{addon.index}/{addon.name}"
        for file in addon.files:
            relative = file.relative.as_posix()
            relative_lower = relative.casefold()
            if file.path.suffix.casefold() != ".pcf" and not (
                relative_lower.startswith("materials/skybox/")
                and file.path.suffix.casefold() == ".vmt"
            ):
                continue
            entries.append(
                _inventory_file_entry(
                    file,
                    f"{label}/{relative}",
                )
            )
    return entries


def _tree_entries(
    root: Path,
    label: str,
    include: Callable[[Path], bool] | None = None,
    file_entry: Callable[[Path, str], list] = _file_entry,
) -> list[list]:
    if not root.is_dir():
        return [[label, "missing"]]

    entries = [[f"{label}/", "directory"]]
    paths = sorted(root.rglob("*"), key=lambda path: path.as_posix().casefold())
    for path in paths:
        if not path.is_file() or (include is not None and not include(path)):
            continue
        relative = path.relative_to(root).as_posix()
        entries.append(file_entry(path, f"{label}/{relative}"))
    return entries


def capture_source_state(
    selected_addons: list[str],
    particle_selections: dict[str, str],
    profiler: "StageTimer | None" = None,
    addon_inventory: AddonInventory | None = None,
) -> list[list]:
    if addon_inventory is None:
        addon_inventory = capture_addon_inventory(
            selected_addons,
            profiler=profiler,
        )
    entries = _source_entries_from_inventory(addon_inventory)

    for mod_name in sorted(set(particle_selections.values())):
        particle_mod_dir = folder_setup.particles_dir / mod_name
        with _measure(profiler, "state_source_particle_mod", mod_name):
            entries.extend(
                _tree_entries(
                    particle_mod_dir,
                    f"particles/{mod_name}",
                    lambda path: path.name != "sound.cache",
                )
            )
    return entries


def _managed_hud_names(custom_dir: Path) -> set[str]:
    result = set()
    if not custom_dir.is_dir():
        return result

    for item in custom_dir.iterdir():
        mod_json = item / "mod.json"
        if not item.is_dir() or not mod_json.is_file():
            continue
        try:
            with mod_json.open("r", encoding="utf-8") as file:
                metadata = json.load(file)
            if metadata.get("type", "").lower() == "hud" and metadata.get("preloader_installed", False):
                result.add(item.name)
        except (OSError, json.JSONDecodeError):
            continue
    return result


def _is_managed_custom_path(path: Path, custom_dir: Path, managed_huds: set[str]) -> bool:
    relative = path.relative_to(custom_dir)
    top_name = relative.parts[0]
    top_name_lower = top_name.lower()
    preloader_prefix = CUSTOM_VPK_NAME.removesuffix(".vpk").lower()

    return (
        top_name in managed_huds
        or top_name == BACKUP_MAINMENU_FOLDER
        or top_name_lower.startswith(preloader_prefix)
        or top_name_lower in {"_quickprecache.vpk", "quickprecache.vpk"}
    )


def capture_external_custom_state(custom_dir: Path) -> list[list]:
    if not custom_dir.is_dir():
        return [["custom/", "missing"]]

    managed_huds = _managed_hud_names(custom_dir)

    def include(path: Path) -> bool:
        return (
            not path.name.lower().endswith(".sound.cache")
            and not _is_managed_custom_path(path, custom_dir, managed_huds)
        )

    return _tree_entries(custom_dir, "custom", include)


def capture_managed_outputs(tf_path: Path | str) -> list[list]:
    tf_path = Path(tf_path)
    custom_dir = tf_path / "custom"
    entries = [
        _file_entry(tf_path / "gameinfo.txt", "gameinfo.txt"),
        _file_entry(tf_path / get_vpk_name(tf_path), get_vpk_name(tf_path)),
    ]

    if custom_dir.is_dir():
        managed_huds = _managed_hud_names(custom_dir)
        for path in sorted(custom_dir.rglob("*"), key=lambda item: item.as_posix().casefold()):
            if (
                path.is_file()
                and not path.name.lower().endswith(".sound.cache")
                and _is_managed_custom_path(path, custom_dir, managed_huds)
            ):
                entries.append(_file_entry(path, f"custom/{path.relative_to(custom_dir).as_posix()}"))

    models_dir = tf_path / "models"
    if models_dir.is_dir():
        model_paths = list(models_dir.glob("precache.mdl")) + list(models_dir.glob("precache_*.mdl"))
        for path in sorted(set(model_paths), key=lambda item: item.name.casefold()):
            entries.append(_file_entry(path, f"models/{path.name}"))

    return sorted(entries, key=lambda entry: entry[0].casefold())


def capture_precache_outputs(tf_path: Path | str) -> list[list]:
    tf_path = Path(tf_path)
    custom_dir = tf_path / "custom"
    entries = [
        _file_entry(custom_dir / "_QuickPrecache.vpk", "custom/_QuickPrecache.vpk"),
        _file_entry(custom_dir / "QuickPrecache.vpk", "custom/QuickPrecache.vpk"),
    ]

    models_dir = tf_path / "models"
    model_paths = list(models_dir.glob("precache.mdl")) + list(models_dir.glob("precache_*.mdl"))
    for path in sorted(set(model_paths), key=lambda item: item.name.casefold()):
        entries.append(_file_entry(path, f"models/{path.name}"))

    return sorted(entries, key=lambda entry: entry[0].casefold())


def capture_direct_game_inputs(
    selected_addons: list[str],
    particle_selections: dict[str, str],
    disable_paint_colors: bool,
    profiler: "StageTimer | None" = None,
    addon_inventory: AddonInventory | None = None,
) -> list[list]:
    entries = [
        ["disable_paint_colors", disable_paint_colors],
        ["particle_selections", [list(item) for item in sorted(particle_selections.items())]],
    ]
    if addon_inventory is None:
        addon_inventory = capture_addon_inventory(
            selected_addons,
            profiler=profiler,
        )
    entries.extend(_direct_entries_from_inventory(addon_inventory))

    for particle_name, mod_name in sorted(particle_selections.items()):
        source_path = (
            folder_setup.particles_dir
            / mod_name
            / "actual_particles"
            / f"{particle_name}.pcf"
        )
        entries.append(
            _file_entry(
                source_path,
                f"selected_particles/{particle_name}/{mod_name}.pcf",
            )
        )

    entries.extend(
        _tree_entries(
            folder_setup.install_dir / "backup" / "particles",
            "bundled_backup/particles",
            lambda path: path.suffix.casefold() == ".pcf",
            _content_file_entry,
        )
    )
    entries.extend(
        _tree_entries(
            folder_setup.install_dir / "backup" / "materials" / "skybox",
            "bundled_backup/materials/skybox",
            lambda path: path.suffix.casefold() == ".vmt",
            _content_file_entry,
        )
    )
    entries.extend(
        _tree_entries(
            folder_setup.backup_dir / "particles",
            "runtime_backup/particles",
            lambda path: path.suffix.casefold() == ".pcf",
        )
    )
    entries.append(
        _content_file_entry(
            folder_setup.particle_system_map_file,
            "particle_system_map.json",
        )
    )
    return entries


def capture_install_inputs(
    selected_addons: list[str],
    particle_selections: dict[str, str],
    disable_paint_colors: bool,
    *,
    include_direct_game: bool = True,
    profiler: "StageTimer | None" = None,
    addon_inventory: AddonInventory | None = None,
) -> CapturedInstallInputs:
    if addon_inventory is None:
        addon_inventory = capture_addon_inventory(
            selected_addons,
            profiler=profiler,
        )
    sources = capture_source_state(
        selected_addons,
        particle_selections,
        profiler=profiler,
        addon_inventory=addon_inventory,
    )
    direct_game_inputs = (
        capture_direct_game_inputs(
            selected_addons,
            particle_selections,
            disable_paint_colors,
            profiler=profiler,
            addon_inventory=addon_inventory,
        )
        if include_direct_game
        else None
    )
    return CapturedInstallInputs(
        sources=sources,
        direct_game_inputs=direct_game_inputs,
    )


def capture_direct_game_output(tf_path: Path | str) -> list[list]:
    tf_path = Path(tf_path)
    vpk_name = get_vpk_name(tf_path)
    vpk_prefix = vpk_name.removesuffix("_dir.vpk")
    vpk_paths = sorted(
        tf_path.glob(f"{vpk_prefix}_*.vpk"),
        key=lambda path: path.name.casefold(),
    )
    if not vpk_paths:
        return [_file_entry(tf_path / vpk_name, vpk_name)]
    return [_file_entry(path, path.name) for path in vpk_paths]


class InstallStateStore:
    def __init__(self, path: Path):
        self.path = path

    @staticmethod
    def _target_key(tf_path: Path | str) -> str:
        normalized = os.path.normcase(os.path.abspath(str(tf_path)))
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()

    @staticmethod
    def _empty_state() -> dict:
        return {"schema": INSTALL_STATE_SCHEMA, "targets": {}}

    def _load(self) -> dict:
        if not self.path.is_file():
            return self._empty_state()
        try:
            with self.path.open("r", encoding="utf-8") as file:
                state = json.load(file)
            if (
                isinstance(state, dict)
                and state.get("schema") == INSTALL_STATE_SCHEMA
                and isinstance(state.get("targets"), dict)
            ):
                return state
        except (OSError, json.JSONDecodeError):
            log.exception("Failed to load install state; a full install will be used")
        return self._empty_state()

    def _write(self, state: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = self.path.with_name(f".{self.path.name}.tmp")
        with temp_path.open("w", encoding="utf-8") as file:
            json.dump(state, file, indent=2, sort_keys=True)
            file.flush()
            os.fsync(file.fileno())
        temp_path.replace(self.path)

    def evaluate(
        self,
        tf_path: Path | str,
        request_header: dict,
        selected_addons: list[str],
        particle_selections: dict[str, str],
        profiler: "StageTimer | None" = None,
        captured_inputs: CapturedInstallInputs | None = None,
    ) -> tuple[bool, str]:
        with _measure(profiler, "state_load", "install_state.json"):
            target = self._load()["targets"].get(self._target_key(tf_path))
        if target is None:
            return False, "no_previous_state"
        if _request_identity(target.get("request", {})) != _request_identity(
            request_header
        ):
            return False, "request_changed"
        if captured_inputs is None:
            captured_inputs = capture_install_inputs(
                selected_addons,
                particle_selections,
                request_header["options"]["disable_paint_colors"],
                include_direct_game=(
                    request_header.get("game_target") == "Team Fortress 2"
                ),
                profiler=profiler,
            )
        if not _stable_file_entries_match(
            target.get("sources"),
            captured_inputs.sources,
        ):
            return False, "source_files_changed"

        custom_dir = Path(tf_path) / "custom"
        with _measure(profiler, "state_external_custom", "tf/custom"):
            external_custom = capture_external_custom_state(custom_dir)
        if target.get("external_custom") != external_custom:
            return False, "external_custom_changed"
        with _measure(profiler, "state_managed_outputs", "managed outputs"):
            managed_outputs = capture_managed_outputs(tf_path)
        if target.get("outputs") != managed_outputs:
            return False, "managed_outputs_changed"
        if request_header.get("game_target") == "Team Fortress 2":
            if not _stable_file_entries_match(
                target.get("direct_game_inputs"),
                captured_inputs.direct_game_inputs,
            ):
                return False, "direct_game_inputs_changed"
            with _measure(profiler, "state_direct_output", "game VPK"):
                direct_game_output = capture_direct_game_output(tf_path)
            if target.get("direct_game_output") != direct_game_output:
                return False, "direct_game_output_changed"
        return True, "up_to_date"

    def reusable_external_custom_paths(
        self,
        tf_path: Path | str,
        request_header: dict,
        profiler: "StageTimer | None" = None,
    ) -> set[str]:
        """Return external custom files already finalized by this recipe."""
        target = self._load()["targets"].get(self._target_key(tf_path))
        if target is None:
            return set()

        previous_request = target.get("request")
        if not isinstance(previous_request, dict):
            return set()
        compatibility_keys = ("recipe", "game_target")
        if any(previous_request.get(key) != request_header.get(key) for key in compatibility_keys):
            return set()

        saved_entries = {
            tuple(entry)
            for entry in target.get("external_custom", [])
            if isinstance(entry, list) and len(entry) == 4
        }
        with _measure(profiler, "state_reuse_external", "tf/custom"):
            current_entries = {
                tuple(entry)
                for entry in capture_external_custom_state(Path(tf_path) / "custom")
                if len(entry) == 4
            }

        reusable = set()
        for entry in saved_entries & current_entries:
            label = entry[0]
            if isinstance(label, str) and label.startswith("custom/"):
                reusable.add(label.removeprefix("custom/"))
        return reusable

    def can_reuse_precache(
        self,
        tf_path: Path | str,
        request_header: dict,
        model_list: set[str],
        profiler: "StageTimer | None" = None,
    ) -> bool:
        target = self._load()["targets"].get(self._target_key(tf_path))
        if target is None:
            return False

        previous_request = target.get("request")
        if not isinstance(previous_request, dict):
            return False
        compatibility_keys = ("recipe", "game_target")
        if any(
            previous_request.get(key) != request_header.get(key)
            for key in compatibility_keys
        ):
            return False

        with _measure(profiler, "state_precache_outputs", "QuickPrecache outputs"):
            precache_outputs = capture_precache_outputs(tf_path)
        return (
            target.get("precache_models") == sorted(model_list)
            and target.get("precache_outputs") == precache_outputs
        )

    def can_reuse_direct_game_files(
        self,
        tf_path: Path | str,
        request_header: dict,
        selected_addons: list[str],
        particle_selections: dict[str, str],
        disable_paint_colors: bool,
        profiler: "StageTimer | None" = None,
        captured_inputs: CapturedInstallInputs | None = None,
    ) -> bool:
        target = self._load()["targets"].get(self._target_key(tf_path))
        if target is None:
            return False

        previous_request = target.get("request")
        if not isinstance(previous_request, dict):
            return False
        if previous_request.get("game_target") != request_header.get("game_target"):
            return False
        if not _direct_game_recipes_are_compatible(previous_request, request_header):
            return False

        if captured_inputs is None:
            captured_inputs = capture_install_inputs(
                selected_addons,
                particle_selections,
                disable_paint_colors,
                profiler=profiler,
            )

        return (
            _stable_file_entries_match(
                target.get("direct_game_inputs"),
                captured_inputs.direct_game_inputs,
            )
            and target.get("direct_game_output") == capture_direct_game_output(tf_path)
        )

    def save_current(
        self,
        tf_path: Path | str,
        request_header: dict,
        selected_addons: list[str],
        particle_selections: dict[str, str],
        precache_models: set[str] | None = None,
        profiler: "StageTimer | None" = None,
        captured_inputs: CapturedInstallInputs | None = None,
    ) -> None:
        with _measure(profiler, "state_load", "install_state.json"):
            state = self._load()
        is_tf2 = request_header.get("game_target") == "Team Fortress 2"
        if captured_inputs is None:
            captured_inputs = capture_install_inputs(
                selected_addons,
                particle_selections,
                request_header["options"]["disable_paint_colors"],
                include_direct_game=is_tf2,
                profiler=profiler,
            )
        sources = captured_inputs.sources
        with _measure(profiler, "state_external_custom", "tf/custom"):
            external_custom = capture_external_custom_state(Path(tf_path) / "custom")
        with _measure(profiler, "state_managed_outputs", "managed outputs"):
            outputs = capture_managed_outputs(tf_path)
        with _measure(profiler, "state_precache_outputs", "QuickPrecache outputs"):
            precache_outputs = (
                capture_precache_outputs(tf_path)
                if precache_models is not None
                else None
            )
        direct_game_inputs = captured_inputs.direct_game_inputs if is_tf2 else None
        with _measure(profiler, "state_direct_output", "game VPK"):
            direct_game_output = capture_direct_game_output(tf_path) if is_tf2 else None

        state["targets"][self._target_key(tf_path)] = {
            "request": request_header,
            "sources": sources,
            "external_custom": external_custom,
            "outputs": outputs,
            "precache_models": sorted(precache_models) if precache_models is not None else None,
            "precache_outputs": precache_outputs,
            "direct_game_inputs": direct_game_inputs,
            "direct_game_output": direct_game_output,
        }
        with _measure(profiler, "state_write", "install_state.json fsync"):
            self._write(state)

    def clear(self, tf_path: Path | str) -> None:
        state = self._load()
        if state["targets"].pop(self._target_key(tf_path), None) is not None:
            self._write(state)
