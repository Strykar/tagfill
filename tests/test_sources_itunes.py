"""Tests for sources/itunes.py's search() — the duration-vector fallback
mb.py tries when MusicBrainz has no catalogued match. Not to be confused
with stages/itunes.py, a different stage doing art-only text matching.

Found live: two Ragas compilation volumes ("Evening Ragas, Vol. 1" and
"Vol. 4") are absent from MusicBrainz's top search results but present in
iTunes' catalogue, with every track's duration matching the local files to
the millisecond (1803.760s iTunes vs 1803.76s local). Rather than a new,
fuzzier title-matching path, this reuses gate() — the exact same
duration-vector verification every source in sources/ shares — just
sourced from iTunes' `lookup?entity=song` endpoint instead of MusicBrainz's
recording data.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

requests_mock = pytest.importorskip("requests_mock")

from tagfill.sources import itunes
from tagfill.util import RateLimiter

# The real values from Evening Ragas, Vol. 1.
LOCAL = [1803.76, 927.69, 877.95]
SEARCH_RESPONSE = {"results": [{
    "collectionId": 151318835,
    "collectionName": "Evening Ragas, Vol. 1",
    "artistName": "Pandit Jasraj, Shruti Sadolikar & Ustad Shahid Parvez Khan",
    "trackCount": 3,
    "releaseDate": "1992-01-01T12:00:00Z",
    "artworkUrl100": "https://example/100x100bb.jpg",
    "primaryGenreName": "Worldwide",
}]}
LOOKUP_RESPONSE = {"results": [
    {"wrapperType": "collection"},
    {"wrapperType": "track", "trackNumber": 1,
     "trackName": "Raga Marwa", "trackTimeMillis": 1803760},
    {"wrapperType": "track", "trackNumber": 2,
     "trackName": "Raga Shree", "trackTimeMillis": 927693},
    {"wrapperType": "track", "trackNumber": 3,
     "trackName": "Raga Hamsadhwani", "trackTimeMillis": 877947},
]}


def _search(album, **overrides):
    kwargs = {"album": album, "local": LOCAL, "tolerance": 4.0, "pass_fraction": 0.90,
                  "limiter": RateLimiter(0.0)}
    kwargs.update(overrides)
    return itunes.search(**kwargs)


def test_matches_the_real_evening_ragas_case():
    with requests_mock.Mocker() as m:
        m.get("https://itunes.apple.com/search", json=SEARCH_RESPONSE)
        m.get("https://itunes.apple.com/lookup", json=LOOKUP_RESPONSE)
        match, rejections = _search("Evening Ragas, Vol 1")

    assert match is not None
    assert match.id == "itunes:151318835"
    assert match.title == "Evening Ragas, Vol. 1"
    assert match.evidence["source"] == "itunes"
    assert match.evidence["within_tolerance"] == 3
    assert rejections == []


def test_match_shape_carries_everything_the_caller_writes():
    with requests_mock.Mocker() as m:
        m.get("https://itunes.apple.com/search", json=SEARCH_RESPONSE)
        m.get("https://itunes.apple.com/lookup", json=LOOKUP_RESPONSE)
        match, _ = _search("Evening Ragas, Vol 1")

    assert match.title
    assert match.date
    assert match.albumartist == \
        "Pandit Jasraj, Shruti Sadolikar & Ustad Shahid Parvez Khan"
    assert match.genre == "Worldwide"


def test_fetch_art_is_lazy_until_called():
    """fetch_art must be a callable, not already-fetched bytes — a source
    that isn't used should never pay for the art request."""
    with requests_mock.Mocker() as m:
        m.get("https://itunes.apple.com/search", json=SEARCH_RESPONSE)
        m.get("https://itunes.apple.com/lookup", json=LOOKUP_RESPONSE)
        match, _ = _search("Evening Ragas, Vol 1")
        art_calls_before = len([r for r in m.request_history
                               if "example" in r.url])
        assert art_calls_before == 0

        m.get("https://example/1200x1200bb.jpg", content=b"fakeart")
        art = match.fetch_art()
    assert art == b"fakeart"


def test_wrong_track_count_is_skipped_before_any_lookup_call():
    """A candidate whose trackCount doesn't match the local folder must not
    even trigger a lookup call — cheap pre-filter."""
    resp = {"results": [{**SEARCH_RESPONSE["results"][0], "trackCount": 12}]}
    with requests_mock.Mocker() as m:
        m.get("https://itunes.apple.com/search", json=resp)
        lookup = m.get("https://itunes.apple.com/lookup", json=LOOKUP_RESPONSE)
        match, _ = _search("Evening Ragas, Vol 1")

    assert match is None
    assert lookup.call_count == 0


def test_no_search_results_returns_none():
    with requests_mock.Mocker() as m:
        m.get("https://itunes.apple.com/search", json={"results": []})
        match, _ = _search("Some Nonexistent Album")
    assert match is None


def test_wrong_durations_are_rejected_despite_matching_track_count():
    """A same-track-count album with unrelated durations must fail the
    gate, not be accepted just because the count lines up."""
    wrong_lookup = {"results": [
        {"wrapperType": "collection"},
        {"wrapperType": "track", "trackNumber": 1, "trackTimeMillis": 50000},
        {"wrapperType": "track", "trackNumber": 2, "trackTimeMillis": 60000},
        {"wrapperType": "track", "trackNumber": 3, "trackTimeMillis": 70000},
    ]}
    with requests_mock.Mocker() as m:
        m.get("https://itunes.apple.com/search", json=SEARCH_RESPONSE)
        m.get("https://itunes.apple.com/lookup", json=wrong_lookup)
        match, _ = _search("Evening Ragas, Vol 1")
    assert match is None


def test_search_request_failure_returns_none_not_an_exception():
    with requests_mock.Mocker() as m:
        m.get("https://itunes.apple.com/search", exc=ConnectionError("down"))
        match, _ = _search("Evening Ragas, Vol 1")
    assert match is None


def test_mb_orchestrator_lists_itunes_after_musicbrainz():
    src = Path(__file__).resolve().parents[1] / "tagfill" / "stages" / "mb.py"
    text = src.read_text()
    idx_mb = text.index("musicbrainz.search,")
    idx_itunes = text.index("itunes.search,", idx_mb)
    assert idx_itunes > idx_mb, (
        "sources list must try musicbrainz before itunes, not replace it "
        "or run it first")
