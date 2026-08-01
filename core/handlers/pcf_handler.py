import hashlib
import logging
import os
import zlib
from pathlib import Path
from typing import TYPE_CHECKING

from valve_parsers import PCFElement, PCFFile, VPKFile

from core.folder_setup import folder_setup
from core.util.vpk import get_vpk_name

log = logging.getLogger()

if TYPE_CHECKING:
    from core.util.perf import StageTimer


class ParticleBackupMismatchError(RuntimeError):
    pass


def get_game_particle_backup_dir(tf_path: Path | str) -> Path:
    normalized = os.path.normcase(os.path.abspath(str(tf_path)))
    target_key = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    return folder_setup.game_backups_dir / target_key / "particles"


def _matches_vpk_entry(data: bytes, file_info: dict) -> bool:
    return (
        file_info.get("preload_bytes", 0) == 0
        and len(data) == file_info["size"]
        and zlib.crc32(data) & 0xFFFFFFFF == file_info["crc"]
    )


def _verified_particle_backup(
    vpk: VPKFile,
    file_path: str,
    bundled_path: Path,
    dynamic_path: Path,
) -> bytes:
    file_info = vpk.get_file_info(file_path)
    if not file_info:
        raise ParticleBackupMismatchError(
            f"Could not find {file_path} in the current game VPK. "
            "Verify the game files through Steam, then try again."
        )

    for candidate in (dynamic_path, bundled_path):
        try:
            candidate_data = candidate.read_bytes()
        except OSError:
            continue
        if _matches_vpk_entry(candidate_data, file_info):
            return candidate_data

    dynamic_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = dynamic_path.with_name(f".{dynamic_path.name}.tmp")
    temp_path.unlink(missing_ok=True)
    try:
        if not vpk.extract_file(file_path, temp_path):
            raise ParticleBackupMismatchError(
                f"Could not read the current vanilla candidate for {file_path}."
            )
        current_data = temp_path.read_bytes()
        if not _matches_vpk_entry(current_data, file_info):
            raise ParticleBackupMismatchError(
                f"No compatible vanilla backup is available for {file_path}, and the "
                "current game file does not match its original VPK checksum. "
                "Use Steam > TF2 > Properties > Installed Files > Verify integrity of "
                "game files, then run the preloader again."
            )
        temp_path.replace(dynamic_path)
        log.info(
            "Captured verified game particle backup file=%s bytes=%d",
            file_path,
            len(current_data),
        )
        return current_data
    finally:
        temp_path.unlink(missing_ok=True)


def restore_particle_files(
    tf_path: Path | str,
    profiler: "StageTimer | None" = None,
) -> int:
    backup_particles_dir = folder_setup.backup_dir / "particles"
    if not backup_particles_dir.exists():
        log.error("missing backup dir/")
        return 0

    vpk_name = get_vpk_name(tf_path)
    vpk_path = Path(tf_path) / vpk_name
    if not vpk_path.exists():
        log.error(f"missing {vpk_name}, is the path correct?")
        return 0

    vpk = VPKFile(vpk_path)
    dynamic_backup_dir = get_game_particle_backup_dir(tf_path)
    patched_count = 0

    for pcf_file in sorted(
        backup_particles_dir.glob("*.pcf"),
        key=lambda path: path.name.casefold(),
    ):
        file_name = pcf_file.name
        file_path = f"particles/{file_name}"
        started_at = profiler.start_operation() if profiler is not None else None

        try:
            original_content = _verified_particle_backup(
                vpk,
                file_path,
                pcf_file,
                dynamic_backup_dir / file_name,
            )

            if vpk.patch_file(file_path, original_content, create_backup=False):
                patched_count += 1
            else:
                raise ParticleBackupMismatchError(
                    f"Failed to restore verified particle file {file_path}."
                )

        except ParticleBackupMismatchError:
            raise
        except Exception as error:
            log.exception(f"Error patching particle file {file_name}")
            raise ParticleBackupMismatchError(
                f"Failed to restore {file_path}: {error}"
            ) from error
        finally:
            if profiler is not None and started_at is not None:
                try:
                    backup_size = pcf_file.stat().st_size
                except OSError:
                    backup_size = 0
                profiler.end_operation(
                    "restore_particle_file",
                    file_name,
                    started_at,
                    size_bytes=backup_size,
                )

    return patched_count


def get_parent_elements(pcf: PCFFile) -> set[str]:
    # get all system definitions
    system_defs = pcf.get_elements_by_type('DmeParticleSystemDefinition')
    system_definitions = {elem.element_name.decode('ascii') for elem in system_defs}

    # get all child elements
    child_elems = pcf.get_elements_by_type('DmeParticleChild')
    child_elements = {elem.element_name.decode('ascii') for elem in child_elems}

    # parent elements are those that aren't also children
    parent_elements = system_definitions - child_elements
    return parent_elements


def check_parents(pcf: PCFFile, parents: set[str]) -> bool:
    # get all system definitions
    system_defs = pcf.get_elements_by_type('DmeParticleSystemDefinition')
    for element in system_defs:
        element_name = element.element_name.decode('ascii')
        if element_name in parents:
            return True
    return False


def update_materials(base: PCFFile, mod: PCFFile) -> PCFFile:
    # build map of element name to material
    mod_materials = {}
    for element in mod.elements:
        element_name = element.element_name.decode('ascii')
        if b'material' in element.attributes:
            mod_materials[element_name] = element.attributes[b'material']

    result = PCFFile(base.input_file)
    result.version = base.version
    result.string_dictionary = base.string_dictionary.copy()
    result.elements = []

    for element in base.elements:
        # create copy
        new_element = PCFElement(
            type_name_index=element.type_name_index,
            element_name=element.element_name,
            data_signature=element.data_signature,
            attributes=element.attributes.copy()
        )

        # update material
        element_name = element.element_name.decode('ascii')
        if element_name in mod_materials:
            new_element.attributes[b'material'] = mod_materials[element_name]

        result.elements.append(new_element)

    return result
