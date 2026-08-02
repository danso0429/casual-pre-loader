import hashlib
import json
from dataclasses import dataclass


@dataclass(frozen=True)
class LegacyBundleIdentity:
    shape_sha256: str
    content_sha256: str


LEGACY_BUNDLE_IDENTITIES = {
    # Verified against the published personal.6 portable archive. The shape
    # digest ignores extraction-specific timestamps; the content digest does not.
    "2.2.4+personal.6": LegacyBundleIdentity(
        shape_sha256=(
            "9826eeb4c84d0ad5a2992e73563073f0"
            "ffd3e4a9a4630d5d8201d89a33d242df"
        ),
        content_sha256=(
            "6a4f1e2f51a41eff56859072c1761bca"
            "571082651bfd0799b89d7b1cdd3dfcea"
        ),
    ),
}


def is_bundled_input_entry(entry: object) -> bool:
    if not isinstance(entry, list) or not entry or not isinstance(entry[0], str):
        return False
    label = entry[0]
    return (
        label.startswith("bundled_backup/particles")
        or label.startswith("bundled_backup/materials/skybox")
        or label == "particle_system_map.json"
    )


def bundled_input_entries(entries: object) -> list[list]:
    if not isinstance(entries, list):
        return []
    return [entry for entry in entries if is_bundled_input_entry(entry)]


def _canonical_digest(entries: list[list], *, shape_only: bool) -> str:
    normalized = []
    for entry in entries:
        if shape_only and len(entry) >= 2 and isinstance(entry[1], int):
            normalized.append([entry[0], entry[1]])
        else:
            normalized.append(entry)
    normalized.sort(key=lambda entry: entry[0].casefold())
    payload = json.dumps(
        normalized,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def bundled_shape_digest(entries: object) -> str:
    return _canonical_digest(bundled_input_entries(entries), shape_only=True)


def bundled_content_digest(entries: object) -> str:
    return _canonical_digest(bundled_input_entries(entries), shape_only=False)


def legacy_bundled_inputs_are_compatible(
    app_version: object,
    previous_entries: object,
    current_entries: object,
) -> bool:
    """Accept one audited personal release's legacy metadata for migration."""
    if not isinstance(app_version, str):
        return False
    identity = LEGACY_BUNDLE_IDENTITIES.get(app_version)
    if identity is None:
        return False
    return (
        bundled_shape_digest(previous_entries) == identity.shape_sha256
        and bundled_content_digest(current_entries) == identity.content_sha256
    )
