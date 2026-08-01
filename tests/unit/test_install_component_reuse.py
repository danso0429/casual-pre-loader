from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from core.handlers.pcf_handler import ParticleBackupMismatchError
from core.services import install as install_service


def test_staging_plan_keeps_the_last_selected_source_for_each_destination(tmp_path):
    first_addon = tmp_path / "addons" / "first"
    second_addon = tmp_path / "addons" / "second"
    first_file = first_addon / "materials" / "shared.vmt"
    second_file = second_addon / "materials" / "shared.vmt"
    unique_file = first_addon / "materials" / "unique.vmt"
    for path, content in (
        (first_file, b"first"),
        (second_file, b"second"),
        (unique_file, b"unique"),
    ):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)

    plan = install_service._build_staging_plan(
        [
            (first_file, first_addon, 0, len(b"first")),
            (unique_file, first_addon, 0, len(b"unique")),
            (second_file, second_addon, 1, len(b"second")),
        ],
        tmp_path / "patched",
        tmp_path / "vpk",
    )

    by_destination = {task[1]: task for task in plan}
    shared_destination = tmp_path / "vpk" / "materials" / "shared.vmt"
    assert len(plan) == 2
    assert by_destination[shared_destination][0] == second_file
    assert by_destination[shared_destination][2] == 1


def test_install_reuses_direct_game_patches_for_a_texture_only_change(tmp_path, monkeypatch):
    tf_path = tmp_path / "tf"
    custom_dir = tf_path / "custom"
    addons_dir = tmp_path / "addons"
    addon_dir = addons_dir / "texture_addon" / "materials"
    addon_dir.mkdir(parents=True)
    (addon_dir / "texture.vtf").write_bytes(b"texture")
    (addon_dir / "texture2.vtf").write_bytes(b"texture2")
    custom_dir.mkdir(parents=True)
    (tf_path / "tf2_misc_dir.vpk").write_bytes(b"directory")

    temp_dir = tmp_path / "temp"
    folder_setup = SimpleNamespace(
        install_state_file=tmp_path / "install_state.json",
        install_performance_file=tmp_path / "install-performance.log",
        addons_dir=addons_dir,
        temp_dir=temp_dir,
        temp_to_be_referenced_dir=temp_dir / "to_be_referenced",
        temp_to_be_patched_dir=temp_dir / "to_be_patched",
        temp_to_be_vpk_dir=temp_dir / "to_be_vpk",
        backup_dir=tmp_path / "backup",
        install_dir=tmp_path / "install",
    )
    folder_setup.temp_to_be_referenced_dir.mkdir(parents=True)

    state_store = Mock()
    state_store.evaluate.return_value = (False, "request_changed")
    state_store.reusable_external_custom_paths.return_value = set()
    state_store.can_reuse_direct_game_files.return_value = True
    state_store.can_reuse_precache.return_value = False
    monkeypatch.setattr(install_service, "folder_setup", folder_setup)
    monkeypatch.setattr(install_service, "InstallStateStore", lambda _path: state_store)
    monkeypatch.setattr(install_service, "check_writable", Mock(return_value=True))
    monkeypatch.setattr(install_service, "_io_worker_count", lambda _count: 2)
    monkeypatch.setattr(install_service, "COPY_BATCH_SIZE", 1)

    forbidden = [
        "initialize_pcf",
        "prepare_particle_restore",
        "apply_particle_restore",
        "restore_skybox_files",
        "restore_particle_files",
        "enable_paints",
        "handle_skybox_mods",
        "disable_paints",
    ]
    for name in forbidden:
        monkeypatch.setattr(
            install_service,
            name,
            Mock(side_effect=AssertionError(f"{name} must be reused")),
        )

    remove_skybox_vmts = Mock(return_value=0)
    monkeypatch.setattr(install_service, "remove_staged_skybox_vmts", remove_skybox_vmts)
    monkeypatch.setattr(install_service, "stage_particle_selections", Mock())
    monkeypatch.setattr(install_service, "game_type", Mock())
    monkeypatch.setattr(install_service, "copy_config_files", Mock())
    monkeypatch.setattr(install_service, "patch_mainmenuoverride", Mock())
    monkeypatch.setattr(install_service, "relocate_mdl_paths", Mock())
    monkeypatch.setattr(install_service, "generate_missing_vmt_files", Mock())
    monkeypatch.setattr(install_service, "create_profiled_vpk", Mock(return_value=True))
    monkeypatch.setattr(install_service, "make_precache_list", Mock(return_value=set()))
    monkeypatch.setattr(install_service, "get_from_custom_dir", Mock())
    reset_working_copy = Mock()
    monkeypatch.setattr(install_service, "prepare_working_copy", reset_working_copy)

    quickprecache = Mock()
    monkeypatch.setattr(install_service, "QuickPrecache", Mock(return_value=quickprecache))

    result = install_service.InstallService().install(
        tf_path,
        ["texture_addon"],
        particle_selections={},
    )

    assert result is True
    assert (
        folder_setup.temp_to_be_vpk_dir / "materials" / "texture.vtf"
    ).read_bytes() == b"texture"
    assert (
        folder_setup.temp_to_be_vpk_dir / "materials" / "texture2.vtf"
    ).read_bytes() == b"texture2"
    remove_skybox_vmts.assert_called_once_with(folder_setup.temp_to_be_vpk_dir)
    quickprecache.flush_files.assert_called_once_with()
    state_store.save_current.assert_called_once()
    reset_working_copy.assert_called_once_with()


def test_install_preflights_particle_backups_before_game_or_hud_mutation(
    tmp_path,
    monkeypatch,
):
    tf_path = tmp_path / "tf"
    tf_path.mkdir()
    (tf_path / "tf2_misc_dir.vpk").write_bytes(b"directory")

    temp_dir = tmp_path / "temp"
    folder_setup = SimpleNamespace(
        install_state_file=tmp_path / "install_state.json",
        install_performance_file=tmp_path / "install-performance.log",
        addons_dir=tmp_path / "addons",
        temp_dir=temp_dir,
        temp_to_be_referenced_dir=temp_dir / "to_be_referenced",
        temp_to_be_patched_dir=temp_dir / "to_be_patched",
        temp_to_be_vpk_dir=temp_dir / "to_be_vpk",
    )

    state_store = Mock()
    state_store.evaluate.return_value = (False, "no_previous_state")
    state_store.reusable_external_custom_paths.return_value = set()
    state_store.can_reuse_direct_game_files.return_value = False
    monkeypatch.setattr(install_service, "folder_setup", folder_setup)
    monkeypatch.setattr(install_service, "InstallStateStore", lambda _path: state_store)
    monkeypatch.setattr(install_service, "check_writable", Mock(return_value=True))
    monkeypatch.setattr(install_service, "initialize_pcf", Mock(return_value=(Mock(), set())))

    preflight = Mock(side_effect=ParticleBackupMismatchError("unsafe backup"))
    cleanup_huds = Mock()
    restore_skybox = Mock()
    apply_particles = Mock()
    reset_working_copy = Mock()
    monkeypatch.setattr(install_service, "prepare_particle_restore", preflight)
    monkeypatch.setattr(install_service.InstallService, "cleanup_huds", cleanup_huds)
    monkeypatch.setattr(install_service, "restore_skybox_files", restore_skybox)
    monkeypatch.setattr(install_service, "apply_particle_restore", apply_particles)
    monkeypatch.setattr(install_service, "prepare_working_copy", reset_working_copy)

    with pytest.raises(ParticleBackupMismatchError, match="unsafe backup"):
        install_service.InstallService().install(
            tf_path,
            [],
            particle_selections={},
        )

    preflight.assert_called_once()
    assert preflight.call_args.args == (tf_path,)
    assert preflight.call_args.kwargs["profiler"] is not None
    cleanup_huds.assert_not_called()
    restore_skybox.assert_not_called()
    apply_particles.assert_not_called()
    reset_working_copy.assert_called_once_with()
