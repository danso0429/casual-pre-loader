from types import SimpleNamespace

from core.services import setup


def test_userdata_import_preserves_install_state_and_verified_game_backups(
    tmp_path,
    monkeypatch,
):
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


def test_userdata_import_treats_new_state_files_as_optional(tmp_path, monkeypatch):
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
