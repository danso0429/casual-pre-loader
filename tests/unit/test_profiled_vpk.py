import logging

from valve_parsers import VPKFile

from core.util import profiled_vpk
from core.util.perf import StageTimer
from core.util.profiled_vpk import create_profiled_vpk


def test_profiled_vpk_creates_archive_and_records_internal_phases(tmp_path):
    source_dir = tmp_path / "source"
    source_file = source_dir / "materials" / "test.vmt"
    source_file.parent.mkdir(parents=True)
    source_file.write_bytes(b"material")
    output_base = tmp_path / "custom" / "profiled"
    timer = StageTimer(
        logging.getLogger("test.profiled-vpk"),
        "install",
        slow_threshold=0,
    )

    assert create_profiled_vpk(
        source_dir,
        output_base,
        2 ** 31,
        timer,
        read_workers=2,
    )

    directory_vpk = output_base.with_name("profiled_dir.vpk")
    assert directory_vpk.exists()
    assert output_base.with_name("profiled_000.vpk").exists()
    assert VPKFile(directory_vpk).list_files() == ["materials/test.vmt"]
    assert {item.category for item in timer.slow_operations} == {
        "vpk_enumerate_inputs",
        "vpk_read_inputs",
        "vpk_crc_and_write",
    }


def test_parallel_vpk_input_builder_preserves_library_structure_and_last_winner(
    tmp_path,
):
    first = tmp_path / "first.vmt"
    second = tmp_path / "second.vmt"
    root_file = tmp_path / "README"
    first.write_bytes(b"first")
    second.write_bytes(b"second")
    root_file.write_bytes(b"root")
    files = [
        (str(first), "Materials/Shared.VMT"),
        (str(root_file), "README"),
        (str(second), "materials/shared.vmt"),
    ]

    expected = VPKFile._build_vpk_structure(files)
    actual = profiled_vpk._build_vpk_structure(files, read_workers=2)

    assert actual == expected
