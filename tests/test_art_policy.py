"""Art policy: "missing" must mean absent OR below the minimum size,
consistently, at every point a stage decides what to work on.

`art_local.needs_art()` had the rule right, and mb/itunes both imported
it. But each of them selected its targets on the raw `has_art`
boolean first, so a file carrying a 200px thumbnail was filtered out before
`needs_art()` was ever consulted. The guard was correct and unreachable.

Effect: one undersized cover.jpg embedded at stage 2 permanently blocks every
network stage from supplying real art, which is a fail-closed in the wrong
direction.
"""

import re
from pathlib import Path

STAGES = Path(__file__).resolve().parents[1] / "tagfill" / "stages"

ROW_NO_ART = {"path": "a.mp3", "issue": "", "has_art": "", "art_min_px": "",
              "artist": "A", "album": "B", "title": "T", "date": "",
              "albumartist": ""}
ROW_SMALL_ART = {**ROW_NO_ART, "path": "b.mp3", "has_art": "1",
                 "art_min_px": "200"}
ROW_GOOD_ART = {**ROW_NO_ART, "path": "c.mp3", "has_art": "1",
                "art_min_px": "1000"}


def test_row_needs_art_treats_undersized_as_missing():
    from tagfill.stages.art_local import row_needs_art
    assert row_needs_art(ROW_NO_ART, 300) is True
    assert row_needs_art(ROW_SMALL_ART, 300) is True, \
        "200px art must count as missing at a 300px floor"
    assert row_needs_art(ROW_GOOD_ART, 300) is False


def test_row_needs_art_distinguishable_from_raw_has_art():
    """The whole point: the two predicates must disagree on the small-art row,
    otherwise the fix is cosmetic."""
    from tagfill.stages.art_local import row_needs_art
    raw = not ROW_SMALL_ART["has_art"]
    assert raw is False
    assert row_needs_art(ROW_SMALL_ART, 300) is True


def test_no_stage_selects_targets_on_raw_has_art():
    """Regression guard against re-introduction.

    A stage may still *read* has_art, but it must not gate what it works on
    with a bare `not r["has_art"]`. That is the exact shape that made the
    needs_art() guard unreachable.
    """
    bad = re.compile(r'not\s+r\[["\']has_art["\']\]')
    offenders = []
    for p in sorted(STAGES.glob("*.py")):
        for n, line in enumerate(p.read_text().splitlines(), 1):
            if bad.search(line):
                offenders.append(f"{p.name}:{n}: {line.strip()}")
    assert not offenders, (
        "these gate on raw has_art instead of row_needs_art():\n  "
        + "\n  ".join(offenders))


def test_every_stage_that_embeds_art_uses_the_shared_predicate():
    for name in ("mb.py", "itunes.py", "art_local.py"):
        src = (STAGES / name).read_text()
        assert "row_needs_art" in src, f"{name} does not use row_needs_art"


# --- polluted title (Traktor dumps the whole filename into title) -----------

def test_review_apply_overwrites_because_accept_is_authorization():
    """An accepted review row must actually write, even over a non-empty
    field. Deferring to --overwrite made accepted rows silently no-op."""
    src = (STAGES / "filename.py").read_text()
    body = src.split("def _apply_review")[1]
    assert "overwrite=True" in body, \
        "_apply_review must overwrite; the human accept is the authorization"
    assert "overwrite=ctx.overwrite" not in body


def test_polluted_title_is_queued_not_silently_skipped():
    src = (STAGES / "filename.py").read_text()
    assert "polluted_title" in src
    assert "startswith" in src
