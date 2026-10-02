"""
Recovery copy of userdata kept inside the game folder.

Deleting a portable installation also deletes its userdata/ folder, so after
each install a copy of the mods, settings and install state is mirrored to
`<game folder>/casual_preloader_backup/`. The copy uses the same data/ and
config/ layout as userdata/, so a new installation can restore it with
`import_userdata()`.
"""

import json
import logging
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path

from core.config import config
from core.constants import RECOVERY_BACKUP_FOLDER
from core.services.setup import is_valid_userdata_folder
from core.version import VERSION

log = logging.getLogger()

RECOVERY_MANIFEST_NAME = 'manifest.json'
RECOVERY_MANIFEST_SCHEMA = 1


def get_recovery_backup_dir(game_path: Path) -> Path:
    return game_path / RECOVERY_BACKUP_FOLDER


def _recovery_items() -> list[tuple[Path, Path]]:
    """(userdata source, path relative to the recovery folder) for each mirrored item."""
    return [
        (config.mods_dir, Path('data') / config.mods_dir.name),
        (config.game_backups_dir, Path('data') / config.game_backups_dir.name),
        (config.modsinfo_file, Path('data') / config.modsinfo_file.name),
        (config.app_settings_file, Path('config') / config.app_settings_file.name),
        (config.addon_metadata_file, Path('config') / config.addon_metadata_file.name),
        (config.install_state_file, Path('config') / config.install_state_file.name),
    ]


def _has_files(directory: Path) -> bool:
    return directory.is_dir() and any(path.is_file() for path in directory.rglob('*'))


def _same_file(source: os.stat_result, destination: Path) -> bool:
    try:
        stat = destination.stat()
    except OSError:
        return False
    return stat.st_size == source.st_size and stat.st_mtime_ns == source.st_mtime_ns


def _mirror_file(source: Path, destination: Path) -> int:
    """Copy source over destination unless both already match. Returns files copied."""
    if destination.is_dir():
        shutil.rmtree(destination)
    if _same_file(source.stat(), destination):
        return 0
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)
    return 1


def _mirror_tree(source: Path, destination: Path) -> tuple[int, int]:
    """Make destination an exact copy of source. Returns (files copied, paths removed)."""
    if destination.is_file():
        destination.unlink()
    destination.mkdir(parents=True, exist_ok=True)

    copied = 0
    for root, _directories, filenames in os.walk(source):
        relative_root = Path(root).relative_to(source)
        (destination / relative_root).mkdir(parents=True, exist_ok=True)
        for filename in filenames:
            copied += _mirror_file(Path(root) / filename, destination / relative_root / filename)

    removed = 0
    # walk bottom-up so emptied directories can be removed after their files
    for root, directories, filenames in os.walk(destination, topdown=False):
        relative_root = Path(root).relative_to(destination)
        for name in filenames + directories:
            if not (source / relative_root / name).exists():
                stale = Path(root) / name
                if stale.is_dir():
                    shutil.rmtree(stale)
                else:
                    stale.unlink()
                removed += 1
    return copied, removed


def _remove(path: Path) -> int:
    if path.is_dir():
        shutil.rmtree(path)
        return 1
    if path.exists():
        path.unlink()
        return 1
    return 0


def sync_recovery_backup(game_path: Path) -> bool:
    """
    Mirror the current userdata to the recovery folder of `game_path`.

    Returns False without touching the recovery folder when there are no mods or
    no settings to save, so an empty fresh installation never overwrites a copy
    that is still waiting to be restored.
    """
    if not _has_files(config.mods_dir) or not config.app_settings_file.is_file():
        log.info('Skipping recovery backup: no mods or settings to save')
        return False

    backup_dir = get_recovery_backup_dir(game_path)
    manifest_path = backup_dir / RECOVERY_MANIFEST_NAME
    backup_dir.mkdir(parents=True, exist_ok=True)

    # an existing manifest marks a complete copy; drop it until this sync finishes
    manifest_path.unlink(missing_ok=True)

    copied = 0
    removed = 0
    for source, relative in _recovery_items():
        destination = backup_dir / relative
        if source.is_dir():
            item_copied, item_removed = _mirror_tree(source, destination)
            copied += item_copied
            removed += item_removed
        elif source.is_file():
            copied += _mirror_file(source, destination)
        else:
            removed += _remove(destination)

    manifest = {
        'schema': RECOVERY_MANIFEST_SCHEMA,
        'app_version': VERSION,
        'saved_at': datetime.now(timezone.utc).isoformat(timespec='seconds'),
    }
    temp_path = manifest_path.with_name(f'.{manifest_path.name}.tmp')
    temp_path.write_text(json.dumps(manifest, indent=2), encoding='utf-8')
    temp_path.replace(manifest_path)

    log.info('Saved recovery backup to %s copied=%d removed=%d', backup_dir, copied, removed)
    return True


def find_recovery_backup(game_path: Path) -> Path | None:
    """Return the recovery folder of `game_path` if it holds a complete copy."""
    backup_dir = get_recovery_backup_dir(game_path)
    if (backup_dir / RECOVERY_MANIFEST_NAME).is_file() and is_valid_userdata_folder(backup_dir):
        return backup_dir
    return None
