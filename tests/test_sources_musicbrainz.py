"""Regression test: sources/musicbrainz.py must actually request
artist-credits from MusicBrainz, not just read a field that's never
populated.

Found live, only because the compilation-support work made the gap visible:
`get_release_by_id(rid, includes=["recordings", "release-groups"])` never
included "artist-credits", so `rel.get("artist-credit", [])` was always
empty and the write always fell through to the `or artist or "Various
Artists"` fallback chain. For a single-artist folder this was invisible —
the fallback is `artist`, the same name used to search, so the wrong-for-
the-wrong-reason value looked correct. For a compilation folder `artist`
is None, so every one of them silently got the literal string "Various
Artists" instead of the real credited names MusicBrainz actually has.
Confirmed live: adding "artist-credits" to the same call for the same
release (55dee47b, "Afternoon Ragas (Vol 2)") changes the response from
`artist-credit` absent to `[{"artist": {"name": "Hariprasad Chaurasia"}},
...]`. 29 real files on the collection had already been written with the
wrong fallback value before this was caught; corrected by hand afterward,
re-deriving the same value this fixed code now produces on its own.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tagfill.sources.musicbrainz import _top_genre


def test_top_genre_picks_highest_vote_count():
    release = {"release-group": {"tag-list": [
        {"count": "1", "name": "big fat"},
        {"count": "13", "name": "progressive rock"},
        {"count": "8", "name": "art rock"},
    ]}}
    assert _top_genre(release) == "progressive rock"


def test_top_genre_falls_back_to_release_tags_when_group_has_none():
    release = {"tag-list": [{"count": "1", "name": "rock"}]}
    assert _top_genre(release) == "rock"


def test_top_genre_empty_when_no_tags_anywhere():
    assert _top_genre({}) == ""


def test_get_release_by_id_requests_artist_credits():
    src = (Path(__file__).resolve().parents[1] / "tagfill" / "sources"
          / "musicbrainz.py")
    text = src.read_text()
    call = text[text.index("get_release_by_id("):]
    includes_block = call[:call.index(")")]
    assert "artist-credits" in includes_block, (
        "without this, rel.get('artist-credit') is always empty and every "
        "compilation folder's albumartist silently falls back to the "
        "literal string 'Various Artists', discarding MusicBrainz's real "
        "credited names")
