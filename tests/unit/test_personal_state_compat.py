from core.services.install_state import capture_direct_game_inputs
from core.services.personal_state_compat import (
    LEGACY_BUNDLE_IDENTITIES,
    bundled_content_digest,
    bundled_shape_digest,
)


def test_personal6_manifest_matches_current_bundled_inputs():
    entries = capture_direct_game_inputs([], {}, False)
    identity = LEGACY_BUNDLE_IDENTITIES["2.2.4+personal.6"]

    assert bundled_shape_digest(entries) == identity.shape_sha256
    assert bundled_content_digest(entries) == identity.content_sha256
