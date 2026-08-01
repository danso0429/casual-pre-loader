import logging
from contextlib import contextmanager
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from time import perf_counter
from typing import Iterator


@dataclass(frozen=True)
class StageTiming:
    name: str
    duration: float
    elapsed: float
    counters: dict[str, int]


@dataclass(frozen=True)
class OperationTiming:
    category: str
    label: str
    duration: float
    size_bytes: int


@dataclass(frozen=True)
class InventoryEntry:
    category: str
    label: str
    size_bytes: int


class StageTimer:
    """Record stage timings and a bounded list of slow file operations."""

    def __init__(
        self,
        logger: logging.Logger,
        operation: str,
        clock: Callable[[], float] = perf_counter,
        report_path: Path | None = None,
        slow_threshold: float = 0.010,
        detail_limit: int = 20,
    ):
        self.logger = logger
        self.operation = operation
        self.clock = clock
        self.report_path = report_path
        self.slow_threshold = slow_threshold
        self.detail_limit = detail_limit
        self.started_at = self.clock()
        self.last_checkpoint = self.started_at
        self.timings: list[StageTiming] = []
        self.slow_operations: list[OperationTiming] = []
        self.inventory: dict[str, list[InventoryEntry]] = {}

    def start_operation(self) -> float:
        return self.clock()

    def end_operation(
        self,
        category: str,
        label: str,
        started_at: float,
        *,
        size_bytes: int = 0,
    ) -> OperationTiming:
        timing = OperationTiming(
            category=category,
            label=label,
            duration=self.clock() - started_at,
            size_bytes=size_bytes,
        )
        self._keep_slow_operation(timing)
        return timing

    @contextmanager
    def measure(
        self,
        category: str,
        label: str,
        *,
        size_bytes: int = 0,
    ) -> Iterator[None]:
        started_at = self.start_operation()
        try:
            yield
        finally:
            self.end_operation(
                category,
                label,
                started_at,
                size_bytes=size_bytes,
            )

    def _keep_slow_operation(self, timing: OperationTiming) -> None:
        if timing.duration < self.slow_threshold or self.detail_limit <= 0:
            return

        self.slow_operations.append(timing)
        self.slow_operations.sort(key=lambda item: item.duration, reverse=True)
        del self.slow_operations[self.detail_limit:]

    def record_inventory(
        self,
        category: str,
        label: str,
        size_bytes: int,
    ) -> None:
        if self.detail_limit <= 0:
            return

        entries = self.inventory.setdefault(category, [])
        entries.append(InventoryEntry(category, label, size_bytes))
        entries.sort(key=lambda item: item.size_bytes, reverse=True)
        del entries[self.detail_limit:]

    def checkpoint(self, name: str, **counters: int) -> StageTiming:
        now = self.clock()
        timing = StageTiming(
            name=name,
            duration=now - self.last_checkpoint,
            elapsed=now - self.started_at,
            counters=counters,
        )
        self.timings.append(timing)
        self.last_checkpoint = now

        counter_text = " ".join(f"{key}={value}" for key, value in sorted(counters.items()))
        self.logger.info(
            "Performance operation=%s stage=%s duration=%.3fs elapsed=%.3fs%s",
            self.operation,
            name,
            timing.duration,
            timing.elapsed,
            f" {counter_text}" if counter_text else "",
        )
        return timing

    def finish(self) -> float:
        elapsed = self.clock() - self.started_at
        self.logger.info(
            "Performance operation=%s complete elapsed=%.3fs stages=%d",
            self.operation,
            elapsed,
            len(self.timings),
        )
        lines = self._report_lines(elapsed)
        for line in lines[4:]:
            self.logger.info("Performance %s", line)

        if self.report_path is not None:
            try:
                self.report_path.parent.mkdir(parents=True, exist_ok=True)
                self.report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
                self.logger.info("Performance report=%s", self.report_path)
            except OSError:
                self.logger.exception("Could not write performance report")
        return elapsed

    def _report_lines(self, elapsed: float) -> list[str]:
        lines = [
            "The Casual File Shuffler 9000 performance report",
            f"operation={self.operation}",
            f"total={elapsed:.3f}s stages={len(self.timings)}",
            "",
            "STAGES (slowest first)",
        ]

        for rank, timing in enumerate(
            sorted(self.timings, key=lambda item: item.duration, reverse=True),
            start=1,
        ):
            counters = " ".join(
                f"{key}={value}" for key, value in sorted(timing.counters.items())
            )
            lines.append(
                f"stage-rank={rank} stage={timing.name} "
                f"duration={timing.duration:.3f}s"
                f"{f' {counters}' if counters else ''}"
            )

        lines.extend(("", f"SLOW OPERATIONS (top {self.detail_limit}, >= {self.slow_threshold:.3f}s)"))
        if self.slow_operations:
            for rank, timing in enumerate(self.slow_operations, start=1):
                lines.append(
                    f"slow-rank={rank} category={timing.category} "
                    f"duration={timing.duration:.3f}s bytes={timing.size_bytes} "
                    f"file={timing.label}"
                )
        else:
            lines.append("none")

        for category, entries in sorted(self.inventory.items()):
            lines.extend(("", f"LARGEST INPUTS category={category} top={self.detail_limit}"))
            for rank, entry in enumerate(entries, start=1):
                lines.append(
                    f"size-rank={rank} bytes={entry.size_bytes} file={entry.label}"
                )

        return lines
