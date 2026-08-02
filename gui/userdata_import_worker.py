import logging
import time
from pathlib import Path

from PyQt6.QtCore import QThread, pyqtSignal

from core.services.setup import import_userdata

log = logging.getLogger()


class UserdataImportWorker(QThread):
    progress_updated = pyqtSignal(int, int, str)

    def __init__(self, userdata_path: Path):
        super().__init__()
        self.userdata_path = userdata_path
        self.result: tuple[bool, list[str]] = (False, ["Userdata import did not finish."])
        self._last_progress_at = 0.0
        self._last_label = ""

    def _report_progress(self, completed: int, total: int, label: str):
        now = time.monotonic()
        if (
            total == 0
            or completed >= total
            or label != self._last_label
            or now - self._last_progress_at >= 0.05
        ):
            self.progress_updated.emit(completed, total, label)
            self._last_progress_at = now
            self._last_label = label

    def run(self):
        try:
            self.result = import_userdata(
                self.userdata_path,
                progress_callback=self._report_progress,
            )
        except Exception as error:
            log.exception("Unexpected userdata import failure")
            self.result = (False, [f"Unexpected import error: {error}"])
