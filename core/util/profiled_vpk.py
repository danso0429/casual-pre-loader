import logging
import os
from pathlib import Path

from valve_parsers import VPKFile

from core.util.perf import StageTimer

log = logging.getLogger()


def create_profiled_vpk(
    source_dir: Path,
    output_base_path: Path,
    split_size: int | None,
    profiler: StageTimer,
) -> bool:
    """Create a VPK while exposing the library's three expensive phases."""
    try:
        output_base_path.parent.mkdir(parents=True, exist_ok=True)
        source_str = str(source_dir.absolute())
        if not source_str.endswith(os.sep):
            source_str += os.sep
        source_len = len(source_str)

        files = []
        with profiler.measure("vpk_enumerate_inputs", "custom VPK"):
            for root, _dirs, filenames in os.walk(source_str):
                for filename in filenames:
                    full_path = os.path.join(root, filename)
                    files.append((full_path, full_path[source_len:]))

        if not files:
            log.error("No files found in custom VPK input directory")
            return False

        with profiler.measure(
            "vpk_read_inputs",
            f"custom VPK files={len(files)}",
        ):
            vpk_structure = VPKFile._build_vpk_structure(files)

        with profiler.measure(
            "vpk_crc_and_write",
            f"custom VPK files={len(files)}",
        ):
            if split_size is None:
                output_path = (
                    output_base_path
                    if output_base_path.suffix == ".vpk"
                    else output_base_path.with_suffix(".vpk")
                )
                return VPKFile._create_single_vpk(vpk_structure, output_path)
            return VPKFile._create_multi_vpk(
                vpk_structure,
                output_base_path,
                split_size,
            )
    except Exception:
        log.exception("Failed to create profiled custom VPK")
        return False
