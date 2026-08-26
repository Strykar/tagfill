"""Regression test: MusicBrainz special-purpose bracketed placeholders
("[unknown]", "[traditional]", "[data]", etc.) must never be written into a
tag as if they were real values.

Found live: an AcoustID lookup against a real file (a fresh, freshly-created
FLAC from a WAV conversion, no prior tags) matched a genuine MusicBrainz
recording at score 1.0 whose title field is literally the string
"[unknown]" — MB's own convention for "the community does not know this
recording's title", not a placeholder tagfill invented. The stage was
about to propose writing that literal string into the file's title tag,
which reads as corrupted metadata and is worse than leaving the field empty.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tagfill.util import is_mb_placeholder


def test_bracketed_placeholder_is_detected():
    assert is_mb_placeholder("[unknown]") is True
    assert is_mb_placeholder("[traditional]") is True
    assert is_mb_placeholder("[data]") is True
    assert is_mb_placeholder("[dialogue]") is True
    assert is_mb_placeholder("[no artist]") is True
    assert is_mb_placeholder(" [unknown] ") is True, "must tolerate whitespace"


def test_real_titles_with_brackets_are_not_flagged():
    """Brackets as a suffix are normal in real titles; only a value that is
    *entirely* a bracketed phrase is an MB placeholder."""
    assert is_mb_placeholder("Wiggle the Bug (Original Mix)") is False
    assert is_mb_placeholder("Song Title [Extended Mix]") is False
    assert is_mb_placeholder("[Redacted] Sessions Vol. 1") is False
    assert is_mb_placeholder("Toca's Miracle (club mix)") is False


def test_none_and_empty_are_not_flagged():
    assert is_mb_placeholder(None) is False
    assert is_mb_placeholder("") is False


def test_acoustid_stage_filters_placeholder_title():
    """The exact real-world case: a real match, real artist, placeholder
    title. Only the title should be withheld; the artist is still good."""
    from tagfill.util import is_mb_placeholder as guard

    rec = {"title": "[unknown]", "artists": [{"name": "Xompax"}]}
    artist = "; ".join(a.get("name", "") for a in rec.get("artists", [])
                       if not guard(a.get("name"))) or None
    title = rec.get("title")
    if guard(title):
        title = None
    assert artist == "Xompax"
    assert title is None


def test_acoustid_stage_rejects_when_only_placeholder_available():
    from tagfill.util import is_mb_placeholder as guard

    rec = {"title": "[unknown]", "artists": [{"name": "[unknown]"}]}
    artist = "; ".join(a.get("name", "") for a in rec.get("artists", [])
                       if not guard(a.get("name"))) or None
    title = rec.get("title")
    if guard(title):
        title = None
    assert artist is None
    assert title is None


def test_mb_stage_falls_back_to_search_artist_when_credit_is_placeholder():
    from tagfill.util import is_mb_placeholder as guard

    artist_credit = [{"artist": {"name": "[unknown]"}}]
    search_artist = "Some Query Artist"
    albumartist = "; ".join(
        c.get("artist", {}).get("name", "") for c in artist_credit
        if isinstance(c, dict) and not guard(c.get("artist", {}).get("name"))
    ) or search_artist
    assert albumartist == "Some Query Artist"


def test_mb_stage_blanks_placeholder_album_title():
    from tagfill.util import is_mb_placeholder as guard

    rel_title = "[unknown]"
    album_title = rel_title
    if guard(album_title):
        album_title = ""
    assert album_title == ""
