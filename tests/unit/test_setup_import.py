import logging
from types import SimpleNamespace

from core.services import setup


def test_userdata_import_preserves_install_state_and_verified_game_backups(
    tmp_path,
    monkeypatch,
    caplog,
):
    caplog.set_level(logging.INFO)
    source_userdata = tmp_path / "old" / "userdata"
    source_data = source_userdata / "data"
    source_config = source_userdata / "config"
    source_data.mkdir(parents=True)
    source_config.mkdir(parents=True)
    (source_data / "mods").mkdir()
    (source_data / "game_backups" / "target" / "particles").mkdir(parents=True)
    (source_data / "game_backups" / "target" / "particles" / "safe.pcf").write_bytes(
        b"verified"
    )
    (source_config / "install_state.json").write_text("{}", encoding="utf-8")

    target_data = tmp_path / "new" / "userdata" / "data"
    target_config = tmp_path / "new" / "userdata" / "config"
    monkeypatch.setattr(
        setup,
        "folder_setup",
        SimpleNamespace(
            mods_dir=target_data / "mods",
            game_backups_dir=target_data / "game_backups",
            modsinfo_file=target_data / "modsinfo.json",
            app_settings_file=target_config / "app_settings.json",
            addon_metadata_file=target_config / "addon_metadata.json",
            install_state_file=target_config / "install_state.json",
        ),
    )

    success, warnings = setup.import_userdata(source_userdata)

    assert success
    assert (target_config / "install_state.json").read_text(encoding="utf-8") == "{}"
    assert (
        target_data / "game_backups" / "target" / "particles" / "safe.pcf"
    ).read_bytes() == b"verified"
    assert warnings == [
        "Not present in source: modsinfo.json",
        "Not present in source: app_settings.json",
        "Not present in source: addon_metadata.json",
    ]
    assert "Imported userdata item=install_state.json" in caplog.text
    assert "Imported userdata item=game_backups" in caplog.text


def test_userdata_import_treats_new_state_files_as_optional(
    tmp_path,
    monkeypatch,
    caplog,
):
    caplog.set_level(logging.INFO)
    source_userdata = tmp_path / "old" / "userdata"
    source_data = source_userdata / "data"
    source_config = source_userdata / "config"
    (source_data / "mods").mkdir(parents=True)
    source_config.mkdir(parents=True)

    target_data = tmp_path / "new" / "userdata" / "data"
    target_config = tmp_path / "new" / "userdata" / "config"
    monkeypatch.setattr(
        setup,
        "folder_setup",
        SimpleNamespace(
            mods_dir=target_data / "mods",
            game_backups_dir=target_data / "game_backups",
            modsinfo_file=target_data / "modsinfo.json",
            app_settings_file=target_config / "app_settings.json",
            addon_metadata_file=target_config / "addon_metadata.json",
            install_state_file=target_config / "install_state.json",
        ),
    )

    success, warnings = setup.import_userdata(source_userdata)

    assert success
    assert "Not present in source: game_backups" not in warnings
    assert "Not present in source: install_state.json" not in warnings
    assert "Optional userdata item not present item=game_backups" in caplog.text
    assert "Optional userdata item not present item=install_state.json" in caplog.text


def test_userdata_import_reports_file_progress_and_preserves_empty_directories(
    tmp_path,
    monkeypatch,
):
    source_userdata = tmp_path / "old" / "userdata"
    source_data = source_userdata / "data"
    source_config = source_userdata / "config"
    (source_data / "mods" / "nested" / "empty").mkdir(parents=True)
    (source_data / "mods" / "first.vpk").write_bytes(b"first")
    (source_data / "mods" / "nested" / "second.txt").write_bytes(b"second")
    source_config.mkdir(parents=True)
    (source_config / "app_settings.json").write_text("{}", encoding="utf-8")

    target_data = tmp_path / "new" / "userdata" / "data"
    target_config = tmp_path / "new" / "userdata" / "config"
    monkeypatch.setattr(
        setup,
        "folder_setup",
        SimpleNamespace(
            mods_dir=target_data / "mods",
            game_backups_dir=target_data / "game_backups",
            modsinfo_file=target_data / "modsinfo.json",
            app_settings_file=target_config / "app_settings.json",
            addon_metadata_file=target_config / "addon_metadata.json",
            install_state_file=target_config / "install_state.json",
        ),
    )
    progress_events = []

    success, warnings = setup.import_userdata(
        source_userdata,
        progress_callback=lambda completed, total, label: progress_events.append(
            (completed, total, label)
        ),
    )

    assert success
    assert warnings == [
        "Not present in source: modsinfo.json",
        "Not present in source: addon_metadata.json",
    ]
    assert progress_events[0] == (0, 0, "Scanning previous userdata...")
    assert progress_events[-1] == (3, 3, "Userdata import complete")
    assert any(label == "Importing mods..." for _done, _total, label in progress_events)
    assert (target_data / "mods" / "first.vpk").read_bytes() == b"first"
    assert (target_data / "mods" / "nested" / "second.txt").read_bytes() == b"second"
    assert (target_data / "mods" / "nested" / "empty").is_dir()
