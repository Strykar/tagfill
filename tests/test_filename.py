"""Table-driven tests for the filename parser. Pure python: no mutagen, no
fixtures, runnable anywhere with `python tests/test_filename.py` or pytest."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tagfill.stages.filename import (
    clean_stem,
    crate_grouping,
    parse_stem,
)

CASES = [
    # stem, expect_artist, expect_title, expect_label, min_confidence
    ("Dysmorph - Quietus Korero", "Dysmorph", "Quietus Korero", None, 0.7),
    ("Dysmorph - Quietus Korero [Fixture Recs]",
     "Dysmorph", "Quietus Korero", "Fixture Recs", 0.7),
    ("Artist - Title (Extended Mix) [Label]",
     "Artist", "Title (Extended Mix)", "Label", 0.7),
    # The Camelot trap: must NOT yield artist "2A".
    ("2A - 128 - 03 Kliment Serial Spider", None, "Kliment Serial Spider",
     None, 0.0),
    ("12B - Some Artist - Some Title", "Some Artist", "Some Title", None, 0.7),
    ("8m - 124 - Open Key Artist - Track", "Open Key Artist", "Track",
     None, 0.7),
    ("01 Artist - Title", "Artist", "Title", None, 0.7),
    ("01. Artist - Title", "Artist", "Title", None, 0.7),
    # 112 is a real artist; a bare number with a separator is a BPM prefix
    # only when 60-200, but "112 - Cupid" is inside that range: the parse is
    # wrong by design and must therefore land BELOW the apply threshold or
    # keep the artist. Either way it must not silently apply "Cupid" alone
    # with high confidence. We assert it stays reviewable.
    ("112 - Cupid", None, "Cupid", None, 0.0),
    # No separator at all: title-only, low confidence.
    ("justafilename", None, "justafilename", None, 0.0),
    # Underscores normalize to spaces.
    ("Artist_-_Title", "Artist", "Title", None, 0.7),
]


def test_parse_table():
    for stem, artist, title, label, min_conf in CASES:
        p = parse_stem(stem)
        assert p.artist == artist, f"{stem!r}: artist {p.artist!r} != {artist!r}"
        assert p.title == title, f"{stem!r}: title {p.title!r} != {title!r}"
        assert p.label == label, f"{stem!r}: label {p.label!r} != {label!r}"
        assert p.confidence >= min_conf, \
            f"{stem!r}: confidence {p.confidence} < {min_conf}"


def test_low_confidence_cases_stay_below_default_threshold():
    """Anything that lost its artist to the stripper, or never had a
    separator, must fall below the default 0.70 apply threshold so it lands
    in the review queue instead of being silently applied."""
    for stem in ("2A - 128 - 03 Kliment Serial Spider", "justafilename",
                 "112 - Cupid"):
        assert parse_stem(stem).confidence < 0.70, stem


def test_stripper_never_consumes_everything():
    p = parse_stem("7A")
    assert p.title == "7A"  # nothing left to strip into


def test_clean_stem():
    assert clean_stem("2A - 128 - 03 Kliment Serial Spider") == \
        "Kliment Serial Spider"
    assert clean_stem("Plain - Name") == "Plain - Name"


def test_crate_grouping():
    globs = ["DJ Pool/*"]
    assert crate_grouping(Path("DJ Pool/Some Crate/x.flac"), globs) \
        == "Some Crate"
    assert crate_grouping(Path("Albums/Artist/x.flac"), globs) is None
    assert crate_grouping(Path("DJ Pool/Some Crate/x.flac"), []) is None


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"ok {name}")
