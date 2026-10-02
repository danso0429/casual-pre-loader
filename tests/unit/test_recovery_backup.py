import json
import os
from types import SimpleNamespace

import pytest

from core.services import install as install_service
from core.services import recovery_backup, setup


def _userdata_config(root):
    data = root / "data"
    config_dir = root / "config"
    return SimpleNamespace(
        mods_dir=data / "mods",
        game_backups_dir=data / "game_backups",
        modsinfo_file=data / "modsinfo.json",
        app_settings_file=config_dir / "app_settings.json",
        addon_metadata_file=config_dir / "addon_metadata.json",
        install_state_file=config_dir / "install_state.json",
    )


@pytest.fixture
def userdata(tmp_path, monkeypatch):
    source = _userdata_config(tmp_path / "userdata")
    monkeypatch.setattr(recovery_backup, "config", source)

    addon = source.mods_dir / "addons" / "hud_mod"
    addon.mkdir(parents=True)
    (addon / "mod.json").write_text('{"type": "hud"}', encoding="utf-8")
    particle = source.mods_dir / "particles" / "explosions" / "actual_particles"
    particle.mkdir(parents=True)
    (particle / "explosion.pcf").write_bytes(b"pcf")
    (source.game_backups_dir / "target" / "particles").mkdir(parents=True)
    (source.game_backups_dir / "target" / "particles" / "safe.pcf").write_bytes(b"safe")
    source.app_settings_file.parent.mkdir(parents=True)
    source.app_settings_file.write_text(
        json.dumps({"profiles": {"TF2": {"addon_selections": ["hud_mod", "b", "a"]}}}),
        encoding="utf-8",
    )
    source.addon_metadata_file.write_text("{}", encoding="utf-8")
    source.install_state_file.write_text('{"targets": {}}', encoding="utf-8")
    return source


def test_sync_mirrors_userdata_and_marks_it_complete(tmp_path, userdata):
    game = tmp_path / "tf"
    game.mkdir()

    assert recovery_backup.sync_recovery_backup(game)

    backup = game / "casual_preloader_backup"
    assert recovery_backup.find_recovery_backup(game) == backup
    assert (
        backup / "data" / "mods" / "particles" / "explosions" / "actual_particles" / "explosion.pcf"
    ).read_bytes() == b"pcf"
    assert (backup / "data" / "game_backups" / "target" / "particles" / "safe.pcf").read_bytes() == b"safe"
    assert (backup / "config" / "app_settings.json").read_text(encoding="utf-8") == (
        userdata.app_settings_file.read_text(encoding="utf-8")
    )
    manifest = json.loads((backup / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["schema"] == 1


def test_sync_removes_deleted_mods_and_updates_changed_files(tmp_path, userdata):
    game = tmp_path / "tf"
    game.mkdir()
    recovery_backup.sync_recovery_backup(game)
    backup_mods = game / "casual_preloader_backup" / "data" / "mods"

    (userdata.mods_dir / "particles" / "explosions" / "actual_particles" / "explosion.pcf").unlink()
    (userdata.mods_dir / "particles" / "explosions" / "actual_particles").rmdir()
    (userdata.mods_dir / "particles" / "explosions").rmdir()
    settings_text = '{"profiles": {"TF2": {"addon_selections": ["a", "b", "hud_mod"]}}}'
    userdata.app_settings_file.write_text(settings_text, encoding="utf-8")
    userdata.install_state_file.unlink()

    assert recovery_backup.sync_recovery_backup(game)

    assert not (backup_mods / "particles" / "explosions").exists()
    assert (backup_mods / "addons" / "hud_mod" / "mod.json").is_file()
    backup_config = game / "casual_preloader_backup" / "config"
    assert (backup_config / "app_settings.json").read_text(encoding="utf-8") == settings_text
    assert not (backup_config / "install_state.json").exists()


def test_sync_recopies_a_file_whose_timestamp_changed(tmp_path, userdata):
    game = tmp_path / "tf"
    game.mkdir()
    recovery_backup.sync_recovery_backup(game)
    source = userdata.mods_dir / "addons" / "hud_mod" / "mod.json"
    backup_file = game / "casual_preloader_backup" / "data" / "mods" / "addons" / "hud_mod" / "mod.json"

    # same size, different content and timestamp
    source.write_text('{"type": "HUD"}', encoding="utf-8")
    stat = source.stat()
    os.utime(source, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))

    recovery_backup.sync_recovery_backup(game)

    assert backup_file.read_text(encoding="utf-8") == '{"type": "HUD"}'


def test_empty_userdata_never_overwrites_an_existing_recovery_copy(tmp_path, userdata, monkeypatch):
    game = tmp_path / "tf"
    game.mkdir()
    recovery_backup.sync_recovery_backup(game)
    backup = game / "casual_preloader_backup"
    before = sorted(path.relative_to(backup) for path in backup.rglob("*"))

    fresh = _userdata_config(tmp_path / "fresh")
    fresh.app_settings_file.parent.mkdir(parents=True)
    fresh.app_settings_file.write_text("{}", encoding="utf-8")
    fresh.mods_dir.mkdir(parents=True)
    (fresh.mods_dir / "addons").mkdir()
    monkeypatch.setattr(recovery_backup, "config", fresh)

    assert not recovery_backup.sync_recovery_backup(game)
    assert sorted(path.relative_to(backup) for path in backup.rglob("*")) == before
    assert recovery_backup.find_recovery_backup(game) == backup


def test_interrupted_sync_is_not_offered_for_restore(tmp_path, userdata, monkeypatch):
    game = tmp_path / "tf"
    game.mkdir()
    recovery_backup.sync_recovery_backup(game)

    def fail(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(recovery_backup.shutil, "copy2", fail)
    (userdata.mods_dir / "addons" / "hud_mod" / "new.txt").write_text("new", encoding="utf-8")

    with pytest.raises(OSError):
        recovery_backup.sync_recovery_backup(game)
    assert recovery_backup.find_recovery_backup(game) is None


def test_missing_or_invalid_recovery_folder_is_ignored(tmp_path):
    game = tmp_path / "tf"
    game.mkdir()
    assert recovery_backup.find_recovery_backup(game) is None

    (game / "casual_preloader_backup").mkdir()
    (game / "casual_preloader_backup" / "manifest.json").write_text("{}", encoding="utf-8")
    assert recovery_backup.find_recovery_backup(game) is None


def test_recovery_copy_restores_through_userdata_import(tmp_path, userdata, monkeypatch):
    game = tmp_path / "tf"
    game.mkdir()
    recovery_backup.sync_recovery_backup(game)

    fresh = _userdata_config(tmp_path / "reinstalled" / "userdata")
    monkeypatch.setattr(setup, "config", fresh)

    success, warnings = setup.import_userdata(recovery_backup.find_recovery_backup(game))

    assert success
    assert warnings == ["Not present in source: modsinfo.json"]
    assert fresh.app_settings_file.read_text(encoding="utf-8") == (
        userdata.app_settings_file.read_text(encoding="utf-8")
    )
    assert (fresh.mods_dir / "addons" / "hud_mod" / "mod.json").is_file()
    assert (
        fresh.mods_dir / "particles" / "explosions" / "actual_particles" / "explosion.pcf"
    ).read_bytes() == b"pcf"
    assert (fresh.game_backups_dir / "target" / "particles" / "safe.pcf").read_bytes() == b"safe"
    assert fresh.install_state_file.read_text(encoding="utf-8") == '{"targets": {}}'


def test_install_keeps_going_when_the_recovery_copy_fails(tmp_path, monkeypatch, caplog):
    def fail(_game_path):
        raise OSError("read-only")

    monkeypatch.setattr(install_service, "sync_recovery_backup", fail)

    install_service.InstallService._save_recovery_backup(tmp_path)

    assert "Failed to save recovery backup" in caplog.text
