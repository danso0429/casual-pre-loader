import json
import logging
import os
import shutil
from collections.abc import Callable
from pathlib import Path

from core.folder_setup import folder_setup
from core.util.file import copy, delete

log = logging.getLogger()

ImportProgressCallback = Callable[[int, int, str], None]


def is_valid_userdata_folder(userdata_path: Path) -> bool:
    """Return True if path looks like a userdata/ folder (has data/ and config/ subfolders)."""
    if not userdata_path.is_dir():
        return False
    return (userdata_path / 'data').is_dir() and (userdata_path / 'config').is_dir()


def _copy_unit_count(source: Path) -> int:
    """Count file-copy operations, treating an empty directory as one unit."""
    if source.is_file():
        return 1

    file_count = 0
    try:
        for _root, _directories, filenames in os.walk(source, followlinks=True):
            file_count += len(filenames)
    except OSError:
        log.warning("Could not fully count userdata item=%s", source.name, exc_info=True)

    return max(file_count, 1)


def _copy_userdata_item(
    source: Path,
    destination: Path,
    item_units: int,
    completed: int,
    total: int,
    progress_callback: ImportProgressCallback | None,
) -> int:
    """Copy one userdata item and report completed file-copy units."""
    item_label = f"Importing {source.name}..."

    if progress_callback:
        progress_callback(completed, total, item_label)

    delete(destination, not_exist_ok=True)

    if source.is_file():
        copy(source, destination)
        completed += 1
        if progress_callback:
            progress_callback(completed, total, item_label)
        return completed

    copied_files = 0

    def copy_file(source_file: str, destination_file: str) -> str:
        nonlocal completed, copied_files
        result = shutil.copy2(source_file, destination_file)
        copied_files += 1
        completed += 1
        if progress_callback:
            progress_callback(completed, total, item_label)
        return result

    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(
        source,
        destination,
        copy_function=copy_file,
        dirs_exist_ok=True,
    )

    if copied_files == 0:
        completed += item_units
        if progress_callback:
            progress_callback(completed, total, item_label)

    return completed


def import_userdata(
    userdata_path: Path,
    progress_callback: ImportProgressCallback | None = None,
) -> tuple[bool, list[str]]:
    """
    Import a userdata folder from a previous installation.

    Copies mods/, verified game backups, app settings, addon metadata, and
    incremental install state into the locations defined by folder_setup.

    Args:
        userdata_path: Path to the source userdata folder (containing data/ and config/)
        progress_callback: Optional callback receiving completed units, total units,
            and the current operation label. A total of zero indicates an
            indeterminate scan phase.

    Returns:
        Tuple of (success, list of warning messages for files that were missing or failed)
    """

    if not is_valid_userdata_folder(userdata_path):
        return False, [
            f"Source is not a valid userdata folder (missing data/ or config/): {userdata_path}"
        ]

    src_data = userdata_path / 'data'
    src_config = userdata_path / 'config'

    items: list[tuple[Path, Path]] = [
        (src_data / folder_setup.mods_dir.name, folder_setup.mods_dir),
        (src_data / folder_setup.game_backups_dir.name, folder_setup.game_backups_dir),
        (src_data / folder_setup.modsinfo_file.name, folder_setup.modsinfo_file),
        (src_config / folder_setup.app_settings_file.name, folder_setup.app_settings_file),
        (src_config / folder_setup.addon_metadata_file.name, folder_setup.addon_metadata_file),
        (src_config / folder_setup.install_state_file.name, folder_setup.install_state_file),
    ]
    optional_sources = {
        src_data / folder_setup.game_backups_dir.name,
        src_config / folder_setup.install_state_file.name,
    }

    warnings: list[str] = []
    present_items: list[tuple[Path, Path]] = []

    if progress_callback:
        progress_callback(0, 0, "Scanning previous userdata...")

    for src, dst in items:
        if src.resolve() == dst.resolve():
            continue
        if not src.exists():
            if src not in optional_sources:
                warnings.append(f"Not present in source: {src.name}")
            else:
                log.info(
                    "Optional userdata item not present item=%s",
                    src.name,
                )
            continue
        present_items.append((src, dst))

    prepared_items = [
        (src, dst, _copy_unit_count(src))
        for src, dst in present_items
    ]
    total_units = sum(item_units for _src, _dst, item_units in prepared_items)
    if total_units == 0:
        if progress_callback:
            progress_callback(1, 1, "No importable userdata found")
        return True, warnings

    completed = 0
    for src, dst, item_units in prepared_items:
        completed_before_item = completed
        try:
            completed = _copy_userdata_item(
                src,
                dst,
                item_units,
                completed,
                total_units,
                progress_callback,
            )
            log.info("Imported userdata item=%s", src.name)
        except Exception as e:
            log.exception(f"Failed to import {src}")
            warnings.append(f"Failed to import {src.name}: {e}")
            completed = completed_before_item + item_units
            if progress_callback:
                progress_callback(
                    completed,
                    total_units,
                    f"Could not import {src.name}; continuing...",
                )

    if progress_callback:
        progress_callback(total_units, total_units, "Userdata import complete")

    return True, warnings


def save_initial_settings(tf_directory: Path) -> tuple[bool, str]:
    """
    Create or update app_settings.json with initial setup values.

    If app_settings.json already exists (e.g. from a previous userdata import),
    its other keys are preserved and only tf_directory is overwritten.

    Args:
        tf_directory: The tf/ directory path to save

    Returns:
        Tuple of (success, error_message)
    """

    try:
        settings_data = {}
        if folder_setup.app_settings_file.exists():
            try:
                with open(folder_setup.app_settings_file, 'r') as f:
                    settings_data = json.load(f)
            except Exception as e:
                log.warning(f"Failed to read existing settings file: {e}")
                # continue with empty settings

        settings_data["tf_directory"] = str(tf_directory)

        folder_setup.settings_dir.mkdir(parents=True, exist_ok=True)
        with open(folder_setup.app_settings_file, 'w') as f:
            json.dump(settings_data, f, indent=2)

        return True, ""

    except Exception as e:
        log.exception("Failed to save settings")
        return False, str(e)
