"""Table-driven tests for the filename parser. Pure python: no mutagen, no
fixtures, runnable anywhere with `python tests/test_filename.py` or pytest."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tagfill.stages.filename import (
    clean_stem,
    crate_grouping,
    folder_is_track_numbered,
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
    # Track-numbered album files. Passed numbered_folder=True below,
    # because without sibling context a leading number is ambiguous with a
    # numeric artist name ("50 Cent") and is deliberately sent to review.
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
        # A leading digit run is only a track number when the siblings say
        # so; these cases model an album, so say so.
        numbered = stem[:1].isdigit() and not stem[:2].isalpha()
        p = parse_stem(stem, numbered_folder=True if numbered else None)
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


def test_a_numeric_artist_name_is_not_eaten_as_a_track_number():
    """External review: "50 Cent - In Da Club" parsed to artist "Cent" at
    0.80 -- above the 0.70 default, so written unreviewed, on exactly the
    untagged files this stage targets. Nothing in the filename separates
    it from "05 Moby - Porcelain"; only the siblings do."""
    singles = ["2 Chainz - Birthday Song", "50 Cent - In Da Club",
               "3 Doors Down - Kryptonite", "98 Degrees - Because of You"]
    for stem in singles:
        p = parse_stem(stem, numbered_folder=False)
        assert p.artist == stem.split(" - ")[0], f"{stem} -> {p.artist}"
        assert p.confidence >= 0.70

    for stem, artist in [("05 Moby - Porcelain", "Moby"),
                         ("01 - Portishead - Glory Box", "Portishead")]:
        p = parse_stem(stem, numbered_folder=True)
        assert p.artist == artist
        assert p.confidence >= 0.70


def test_an_ambiguous_leading_number_goes_to_review_not_into_the_file():
    """With no folder context the two shapes are indistinguishable, so the
    honest answer is a human, not a guess."""
    for stem in ("50 Cent - In Da Club", "05 Moby - Porcelain"):
        assert parse_stem(stem).confidence < 0.70


def test_folder_is_track_numbered_needs_a_real_sequence():
    assert folder_is_track_numbered(
        ["01 One", "02 Two", "03 Three"])
    assert not folder_is_track_numbered(
        ["50 Cent - In Da Club"])                      # a lone single
    assert not folder_is_track_numbered(
        ["2 Chainz - A", "2 Chainz - B"])              # same number, not a run
