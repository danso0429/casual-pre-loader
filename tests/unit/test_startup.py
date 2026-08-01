import sys
from types import ModuleType
from unittest.mock import Mock, patch

import main as app_main
from scripts import build


def test_main_shows_window_without_splash(monkeypatch):
    def module(name, **attrs):
        result = ModuleType(name)
        for attr_name, value in attrs.items():
            setattr(result, attr_name, value)
        return result

    app = Mock()
    application = Mock(return_value=app)
    splash = Mock(side_effect=AssertionError("splash must not be created"))
    window = Mock()
    settings = Mock()
    settings.is_first_time_setup.return_value = False
    settings.return_value.should_show_update_dialog.return_value = False

    imported_modules = {
        "PyQt6.QtGui": module("PyQt6.QtGui", QIcon=Mock()),
        "PyQt6.QtWidgets": module(
            "PyQt6.QtWidgets",
            QApplication=application,
            QMessageBox=Mock(),
            QSplashScreen=splash,
        ),
        "core.auto_updater": module("core.auto_updater", check_for_updates=Mock(return_value=())),
        "core.backup_manager": module(
            "core.backup_manager", prepare_runtime_environment=Mock(return_value=None)
        ),
        "core.settings": module("core.settings", SettingsManager=settings),
        "gui.first_time_setup": module(
            "gui.first_time_setup", run_first_time_setup=Mock(return_value=None)
        ),
        "gui.main_window": module("gui.main_window", ParticleManagerGUI=Mock(return_value=window)),
        "gui.theme": module("gui.theme", GLOBAL_STYLESHEET=""),
        "gui.update_dialog": module("gui.update_dialog", show_update_dialog=Mock()),
    }

    with (
        patch.dict(sys.modules, imported_modules),
        patch.object(app_main, "delete"),
    ):
        app_main.main()

    app.exec.assert_called_once_with()
    splash.assert_not_called()
    window.show.assert_called_once_with()


def test_build_copies_visible_and_hidden_windows_launchers(tmp_path, monkeypatch):
    target_dir = tmp_path / "casual-preloader"
    monkeypatch.setattr(sys, "argv", ["build.py", "--target_dir", str(target_dir)])
    monkeypatch.setattr("builtins.input", lambda _prompt: "y")

    build.main()

    for launcher_name in ("RUNME.bat", "RUNME.vbs"):
        built_launcher = tmp_path / launcher_name
        source_launcher = build.Path(build.__file__).resolve().parent / launcher_name
        assert built_launcher.read_bytes() == source_launcher.read_bytes()
