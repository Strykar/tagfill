"""Regression test: iTunes art match must reject when the runner-up
candidate is a near-tie, not just accept whatever scores highest.

Found live: local tag "Random Access Memories (Édition Studio Masters)"
scored 0.69 against iTunes' "Random Access Memories (Drumless Edition)" — a
different, instrumental-only product — while the correct plain "Random
Access Memories" sat in the same result set at 0.66, edged out by 0.03 of
pure text-overlap noise. Confirmed the two candidates' cover art actually
differ (0.67 normalized pixel difference on a 64x64 downsample). The
similarity-only gate (best_sim >= 0.60) would have accepted the wrong one.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tagfill.util import similarity


def _score_and_gate(artist, album, results, min_sim=0.60, min_margin=0.15):
    """Mirrors the scoring block in itunes.py's run()."""
    scored = sorted(
        ((min(similarity(artist, r.get("artistName", "")),
             similarity(album, r.get("collectionName", ""))), r)
         for r in results),
        key=lambda t: t[0], reverse=True)
    best_sim, best = scored[0] if scored else (0.0, None)
    second_sim = scored[1][0] if len(scored) > 1 else 0.0
    if not best or best_sim < min_sim:
        return None, "below min_sim"
    if best_sim - second_sim < min_margin:
        return None, "ambiguous"
    return best, "accepted"


REAL_ITUNES_RESULTS = [
    {"artistName": "Daft Punk", "collectionName":
        "Random Access Memories (10th Anniversary Edition)"},
    {"artistName": "Daft Punk", "collectionName":
        "Random Access Memories (Drumless Edition)"},
    {"artistName": "Daft Punk", "collectionName": "Random Access Memories"},
]


def test_ambiguous_near_tie_is_rejected():
    best, reason = _score_and_gate(
        "Daft Punk", "Random Access Memories (Édition Studio Masters)",
        REAL_ITUNES_RESULTS)
    assert best is None
    assert reason == "ambiguous"


def test_clear_winner_is_accepted():
    results = [
        {"artistName": "David Gray", "collectionName": "White Ladder"},
        {"artistName": "David Gray", "collectionName":
            "White Ladder (2020 Remaster)"},
    ]
    best, reason = _score_and_gate("David Gray", "White Ladder", results)
    assert reason == "accepted"
    assert best["collectionName"] == "White Ladder"


def test_similarity_only_gate_would_have_accepted_the_wrong_one():
    """Proves the margin check is load-bearing: without it, the old
    best-sim-only logic picks the wrong candidate for this exact input."""
    scored = sorted(
        ((min(similarity("Daft Punk", r.get("artistName", "")),
             similarity("Random Access Memories (Édition Studio Masters)",
                       r.get("collectionName", ""))), r)
         for r in REAL_ITUNES_RESULTS),
        key=lambda t: t[0], reverse=True)
    old_best = scored[0][1]
    assert old_best["collectionName"] == "Random Access Memories (Drumless Edition)"
