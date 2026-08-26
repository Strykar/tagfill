"""Tests for sources/__init__.py's gate(): the unordered duration-vector
fallback shared by every metadata source.

Found live on Pink Floyd's "The Division Bell": the album folder's .ogg
files carry no track-number prefix, so they sort alphabetically ("A Great
Day For Freedom" before "Cluster One") rather than by track order.
Positional comparison can never pass even against the correct release —
every real MusicBrainz candidate had exactly 11 tracks, matching
local_tracks exactly, yet all were rejected on ordering alone.

The unordered fallback's threshold was wrong on the first attempt: demanding
every track match (11/11) rejects the exact real case it exists for, because
even the correct release has one track (High Hopes) whose duration differs
by real seconds on this pressing — the same mismatch already visible and
already accepted in the ordered pass for the sibling FLAC folder (10/11 at
pass_fraction=0.90). Unordered matching uses the same pass_fraction as
ordered; only the ordering requirement is relaxed, not the tolerance.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tagfill.sources import _unordered_match, gate

# The real Division Bell case: local durations in the .ogg files' alphabetical
# (not track) order, and the real accepted MusicBrainz candidate's vector.
LOCAL_ALPHABETICAL = [261.5, 257.6, 355.5, 379.3, 511.0, 370.7,
                      314.7, 329.4, 423.7, 372.4, 408.9]
CANDIDATE_TRACK_ORDER = [358.866, 261.533, 424.6, 328.333, 258.493,
                         408.906, 372.293, 379.466, 371.333, 314.773, 478.0]


def test_positional_fails_on_the_real_misordered_case():
    result = gate(LOCAL_ALPHABETICAL, [CANDIDATE_TRACK_ORDER],
                  tolerance=4.0, pass_fraction=0.90)
    # Falls through to unordered, not positional.
    assert result is not None
    assert result["order"] == "unordered"


def test_unordered_recovers_the_real_case_at_10_of_11():
    """Even the correct release has one real duration mismatch (High Hopes,
    511s local vs 478s here) — must accept at pass_fraction, not demand 11/11."""
    result = gate(LOCAL_ALPHABETICAL, [CANDIDATE_TRACK_ORDER],
                  tolerance=4.0, pass_fraction=0.90)
    assert result["tracks"] == 11
    assert result["within_tolerance"] == 10


def test_full_100_percent_would_have_rejected_this_real_case():
    """Proves the threshold choice is load-bearing: the first (wrong)
    design demanded every track match, which fails here."""
    matched = _unordered_match(LOCAL_ALPHABETICAL, CANDIDATE_TRACK_ORDER,
                               tolerance=4.0)
    assert matched == 10
    assert matched != len(LOCAL_ALPHABETICAL), (
        "11/11 unordered would have rejected the exact case this fallback "
        "exists for")


def test_ordered_match_still_preferred_when_it_works():
    """A properly track-numbered folder must not fall through to the
    weaker unordered path when positional matching already succeeds."""
    local = [200.0, 210.0, 220.0]
    candidate = [200.0, 210.0, 220.0]
    result = gate(local, [candidate], tolerance=2.0, pass_fraction=0.90)
    assert result["order"] == "positional"


def test_track_count_mismatch_is_never_recovered_by_unordered():
    local = [200.0, 210.0, 220.0]
    candidate = [200.0, 210.0]  # different track count
    assert gate(local, [candidate], tolerance=2.0, pass_fraction=0.90) is None


def test_a_genuinely_different_release_is_rejected_by_unordered_too():
    """Unordered is weaker than positional, not toothless: a different
    album with the same track count and wildly different durations must
    still fail."""
    local = [200.0, 210.0, 220.0, 230.0]
    different_album = [45.0, 600.0, 12.0, 900.0]
    assert gate(local, [different_album], tolerance=4.0,
               pass_fraction=0.90) is None


def test_unordered_match_is_a_true_bijection_not_reusing_candidates():
    """Two local tracks both close to the same single candidate duration
    must not both count as matched — each candidate duration is consumed
    once."""
    local = [100.0, 100.1]
    candidate = [100.05]  # only one candidate duration
    matched = _unordered_match(local, candidate, tolerance=1.0)
    assert matched == 1, "one candidate duration can only match once"
