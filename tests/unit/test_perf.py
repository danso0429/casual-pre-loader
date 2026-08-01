import logging

import pytest

from core.util.perf import StageTimer


def test_stage_timer_records_stage_and_total_durations(caplog):
    clock_values = iter((10.0, 10.25, 11.0, 11.5))
    timer = StageTimer(logging.getLogger("test.perf"), "install", lambda: next(clock_values))

    first = timer.checkpoint("scan", files=12, bytes=4096)
    second = timer.checkpoint("build", files=3)

    with caplog.at_level(logging.INFO, logger="test.perf"):
        elapsed = timer.finish()

    assert first.duration == pytest.approx(0.25)
    assert first.elapsed == pytest.approx(0.25)
    assert first.counters == {"files": 12, "bytes": 4096}
    assert second.duration == pytest.approx(0.75)
    assert second.elapsed == pytest.approx(1.0)
    assert elapsed == pytest.approx(1.5)
    assert "operation=install complete elapsed=1.500s stages=2" in caplog.text
    assert "stage-rank=1 stage=build duration=0.750s files=3" in caplog.text
    assert "stage-rank=2 stage=scan duration=0.250s bytes=4096 files=12" in caplog.text


def test_stage_timer_writes_bounded_slow_file_report(tmp_path):
    clock_values = iter((1.0, 1.0, 1.04, 1.04, 1.24, 1.5))
    report_path = tmp_path / "install-performance.log"
    timer = StageTimer(
        logging.getLogger("test.perf.report"),
        "install",
        lambda: next(clock_values),
        report_path=report_path,
        slow_threshold=0.010,
        detail_limit=1,
        category_limit=1,
    )

    with timer.measure("copy_addon_file", "small/file.vtf", size_bytes=10):
        pass
    with timer.measure("copy_addon_file", "slow/file.vtf", size_bytes=200):
        pass
    timer.record_inventory("custom-vpk", "small/file.vtf", 10)
    timer.record_inventory("custom-vpk", "large/file.vtf", 500)

    assert timer.finish() == pytest.approx(0.5)
    assert [timing.label for timing in timer.slow_operations] == ["slow/file.vtf"]
    assert [
        timing.label
        for timing in timer.slow_operations_by_category["copy_addon_file"]
    ] == ["slow/file.vtf"]
    assert [entry.label for entry in timer.inventory["custom-vpk"]] == ["large/file.vtf"]

    report = report_path.read_text(encoding="utf-8")
    assert "The Casual File Shuffler 9000 performance report" in report
    assert "slow-rank=1 category=copy_addon_file duration=0.200s bytes=200 file=slow/file.vtf" in report
    assert "SLOW BY CATEGORY category=copy_addon_file top=1" in report
    assert "size-rank=1 bytes=500 file=large/file.vtf" in report
    assert "small/file.vtf" not in report
