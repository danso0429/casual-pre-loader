import logging
from pathlib import Path

log = logging.getLogger()


def check_root_lod(game_path: str) -> bool:
    config_file = Path(game_path) / "tf" / "cfg" / "config.cfg"

    if not config_file.exists():
        log.warning(f"Config file not found: {config_file}")
        return False

    # Treat config.cfg as bytes. Source config files often contain legacy or
    # mixed encodings, while the setting we edit is strictly ASCII.
    config_data = config_file.read_bytes()

    # find r_rootlod setting
    root_lod_index = config_data.find(b"r_rootlod")

    if root_lod_index > -1:
        # find the line with r_rootlod
        end_index = config_data.find(b"\n", root_lod_index)
        if end_index == -1:
            end_index = len(config_data)
        old_line = config_data[root_lod_index:end_index]

        # replace with r_rootlod "0"
        config_data = config_data.replace(old_line, b'r_rootlod "0"', 1)
        log.info(f"Updated r_rootlod setting to 0 in {config_file}")
    else:
        # r_rootlod not found, add it to the end of the file
        newline = b"\r\n" if b"\r\n" in config_data else b"\n"
        if not config_data.endswith((b"\n", b"\r")):
            config_data += newline
        config_data += b'r_rootlod "0"' + newline
        log.info(f"Added r_rootlod setting to {config_file}")

    # write the updated config
    config_file.write_bytes(config_data)
    return True
