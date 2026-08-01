import logging
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from valve_parsers import VPKFile

from core.util.perf import StageTimer

log = logging.getLogger()
MAX_VPK_READ_WORKERS = 8


def _parse_vpk_path(filepath: str) -> tuple[str, str, str]:
    filepath = filepath.replace("\\", "/").lower()
    last_slash = filepath.rfind("/")
    if last_slash >= 0:
        directory = filepath[:last_slash]
        filename_ext = filepath[last_slash + 1:]
    else:
        directory = " "
        filename_ext = filepath

    last_dot = filename_ext.rfind(".")
    if last_dot > 0:
        filename = filename_ext[:last_dot]
        extension = filename_ext[last_dot + 1:]
    else:
        filename = filename_ext
        extension = " "
    return extension, directory, filename


def _read_input_batch(batch):
    result = []
    for index, (file_path, relative_path) in batch:
        with open(file_path, "rb") as file:
            content = file.read()
        result.append((index, file_path, relative_path, content))
    return result


def _build_vpk_structure(files, read_workers: int):
    indexed_files = list(enumerate(files))
    batches = [indexed_files[index::read_workers] for index in range(read_workers)]
    if read_workers == 1:
        batch_results = [_read_input_batch(batches[0])]
    else:
        with ThreadPoolExecutor(
            max_workers=read_workers,
            thread_name_prefix="preloader-vpk-read",
        ) as executor:
            batch_results = list(executor.map(_read_input_batch, batches))

    loaded_files = [item for batch in batch_results for item in batch]
    loaded_files.sort(key=lambda item: item[0])

    vpk_structure = {}
    for _index, file_path, relative_path, content in loaded_files:
        extension, path, filename = _parse_vpk_path(relative_path)
        vpk_structure.setdefault(extension, {}).setdefault(path, {})[filename] = {
            "content": content,
            "size": len(content),
            "path": file_path,
        }
    return vpk_structure


def create_profiled_vpk(
    source_dir: Path,
    output_base_path: Path,
    split_size: int | None,
    profiler: StageTimer,
    read_workers: int | None = None,
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

        default_workers = MAX_VPK_READ_WORKERS if os.name == "nt" else 1
        requested_workers = (
            default_workers if read_workers is None else max(1, read_workers)
        )
        workers = min(requested_workers, len(files))

        with profiler.measure(
            "vpk_read_inputs",
            f"custom VPK files={len(files)} workers={workers}",
        ):
            vpk_structure = _build_vpk_structure(files, workers)

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
