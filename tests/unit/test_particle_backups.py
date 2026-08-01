import zlib
from types import SimpleNamespace

import pytest

from core.handlers import pcf_handler


class FakeVPK:
    current_data = b""
    expected_data = b""
    patches = []
    extract_calls = 0

    def __init__(self, _path):
        pass

    def get_file_info(self, _file_path):
        return {
            "size": len(self.expected_data),
            "crc": zlib.crc32(self.expected_data) & 0xFFFFFFFF,
            "preload_bytes": 0,
        }

    def extract_file(self, _file_path, output_path):
        type(self).extract_calls += 1
        output_path.write_bytes(self.current_data)
        return True

    def patch_file(self, file_path, data, create_backup=False):
        type(self).patches.append((file_path, data, create_backup))
        return True


def _setup(tmp_path, monkeypatch, bundled_data):
    backup_dir = tmp_path / "backup"
    particles_dir = backup_dir / "particles"
    particles_dir.mkdir(parents=True)
    (particles_dir / "seasonal.pcf").write_bytes(bundled_data)
    game_backups_dir = tmp_path / "game_backups"
    tf_path = tmp_path / "tf"
    tf_path.mkdir()
    (tf_path / "tf2_misc_dir.vpk").touch()

    monkeypatch.setattr(
        pcf_handler,
        "folder_setup",
        SimpleNamespace(
            backup_dir=backup_dir,
            game_backups_dir=game_backups_dir,
        ),
    )
    monkeypatch.setattr(pcf_handler, "VPKFile", FakeVPK)
    FakeVPK.patches = []
    FakeVPK.extract_calls = 0
    return tf_path


def test_stale_bundled_particle_backup_is_refreshed_from_verified_game_file(
    tmp_path,
    monkeypatch,
):
    tf_path = _setup(tmp_path, monkeypatch, b"old bundled")
    FakeVPK.expected_data = b"current vanilla"
    FakeVPK.current_data = FakeVPK.expected_data

    assert pcf_handler.restore_particle_files(tf_path) == 1

    dynamic_backup = (
        pcf_handler.get_game_particle_backup_dir(tf_path) / "seasonal.pcf"
    )
    assert dynamic_backup.read_bytes() == FakeVPK.expected_data
    assert FakeVPK.extract_calls == 1
    assert FakeVPK.patches == [
        ("particles/seasonal.pcf", FakeVPK.expected_data, False)
    ]


def test_stale_backup_does_not_capture_an_already_modified_game_file(
    tmp_path,
    monkeypatch,
):
    tf_path = _setup(tmp_path, monkeypatch, b"old bundled")
    FakeVPK.expected_data = b"current vanilla"
    FakeVPK.current_data = b"modified file!!"

    with pytest.raises(pcf_handler.ParticleBackupMismatchError, match="Verify integrity"):
        pcf_handler.restore_particle_files(tf_path)

    assert FakeVPK.patches == []


def test_verified_dynamic_backup_restores_a_modified_game_file(tmp_path, monkeypatch):
    tf_path = _setup(tmp_path, monkeypatch, b"old bundled")
    FakeVPK.expected_data = b"current vanilla"
    FakeVPK.current_data = b"modified file!!"
    dynamic_backup = (
        pcf_handler.get_game_particle_backup_dir(tf_path) / "seasonal.pcf"
    )
    dynamic_backup.parent.mkdir(parents=True)
    dynamic_backup.write_bytes(FakeVPK.expected_data)

    assert pcf_handler.restore_particle_files(tf_path) == 1
    assert FakeVPK.extract_calls == 0
    assert FakeVPK.patches == [
        ("particles/seasonal.pcf", FakeVPK.expected_data, False)
    ]
