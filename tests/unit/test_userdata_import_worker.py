from PyQt6.QtCore import QCoreApplication, QEventLoop

from gui import userdata_import_worker


def test_userdata_import_worker_reports_progress_from_background_thread(
    tmp_path,
    monkeypatch,
):
    def fake_import(_path, progress_callback):
        progress_callback(0, 0, "Scanning previous userdata...")
        progress_callback(0, 2, "Importing mods...")
        progress_callback(1, 2, "Importing mods...")
        progress_callback(2, 2, "Userdata import complete")
        return True, []

    monkeypatch.setattr(userdata_import_worker, "import_userdata", fake_import)
    app = QCoreApplication.instance() or QCoreApplication([])
    event_loop = QEventLoop(app)
    events = []
    worker = userdata_import_worker.UserdataImportWorker(tmp_path)
    worker.progress_updated.connect(
        lambda completed, total, label: events.append((completed, total, label))
    )
    worker.finished.connect(event_loop.quit)

    worker.start()
    event_loop.exec()
    assert worker.wait(1000)

    assert worker.result == (True, [])
    assert events == [
        (0, 0, "Scanning previous userdata..."),
        (0, 2, "Importing mods..."),
        (2, 2, "Userdata import complete"),
    ]
