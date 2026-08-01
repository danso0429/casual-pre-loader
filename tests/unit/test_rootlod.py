from core.quickprecache.r_rootlod import check_root_lod


def test_root_lod_update_preserves_non_text_bytes(tmp_path):
    config_path = tmp_path / "tf" / "cfg" / "config.cfg"
    config_path.parent.mkdir(parents=True)
    config_path.write_bytes(b"bind x test\n// \xaf\xfe\nr_rootlod 2\n")

    assert check_root_lod(str(tmp_path))
    assert config_path.read_bytes() == b'bind x test\n// \xaf\xfe\nr_rootlod "0"\n'


def test_root_lod_append_preserves_crlf_and_non_text_bytes(tmp_path):
    config_path = tmp_path / "tf" / "cfg" / "config.cfg"
    config_path.parent.mkdir(parents=True)
    config_path.write_bytes(b"bind x test\r\n// \xaf\xfe")

    assert check_root_lod(str(tmp_path))
    assert config_path.read_bytes() == b'bind x test\r\n// \xaf\xfe\r\nr_rootlod "0"\r\n'
