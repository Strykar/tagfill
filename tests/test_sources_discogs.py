"""Tests for sources/discogs.py's search() — the degraded last-resort
duration-vector source, tried only when both MusicBrainz and iTunes find
nothing. "Degraded" is deliberate: Discogs durations are community-
submitted strings ("2:44"), not derived from the audio like iTunes', and
are frequently missing outright; the unauthenticated API is capped at
25 requests/minute (confirmed live via the x-discogs-ratelimit response
header, far tighter than iTunes' effectively unthrottled pace).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

requests_mock = pytest.importorskip("requests_mock")

from tagfill.sources import discogs
from tagfill.util import RateLimiter

LOCAL = [164.0, 163.0, 176.0]
SEARCH_RESPONSE = {"results": [
    {"id": 4110478, "title": "Some Album", "cover_image": "https://example/thumb.jpg"},
]}
RELEASE_RESPONSE = {
    "title": "Some Album",
    "year": 1990,
    "released": "1990-05-01",
    "artists": [{"name": "Some Artist"}],
    "images": [{"uri": "https://example/full.jpg"}],
    "genres": ["Rock"],
    "tracklist": [
        {"type_": "heading", "title": "Side A"},
        {"type_": "track", "position": "1", "title": "Track One", "duration": "2:44"},
        {"type_": "track", "position": "2", "title": "Track Two", "duration": "2:43"},
        {"type_": "track", "position": "3", "title": "Track Three", "duration": "2:56"},
    ],
}


def _search(album="Some Album", **overrides):
    kwargs = {"album": album, "local": LOCAL, "tolerance": 4.0, "pass_fraction": 0.90,
                  "limiter": RateLimiter(0.0)}
    kwargs.update(overrides)
    return discogs.search(**kwargs)


def test_duration_string_parsing():
    assert discogs._parse_duration("2:44") == 164.0
    assert discogs._parse_duration("1:02:33") == 3753.0
    assert discogs._parse_duration("") is None
    assert discogs._parse_duration(None) is None
    assert discogs._parse_duration("garbage") is None


def test_matches_when_all_durations_present():
    with requests_mock.Mocker() as m:
        m.get("https://api.discogs.com/database/search", json=SEARCH_RESPONSE)
        m.get("https://api.discogs.com/releases/4110478", json=RELEASE_RESPONSE)
        match, _ = _search()

    assert match is not None
    assert match.id == "discogs:4110478"
    assert match.evidence["source"] == "discogs"
    assert match.genre == "Rock"


def test_fetch_art_is_lazy_until_called():
    with requests_mock.Mocker() as m:
        m.get("https://api.discogs.com/database/search", json=SEARCH_RESPONSE)
        m.get("https://api.discogs.com/releases/4110478", json=RELEASE_RESPONSE)
        match, _ = _search()
        assert not any("full.jpg" in r.url for r in m.request_history)

        m.get("https://example/full.jpg", content=b"fakeart")
        art = match.fetch_art()
    assert art == b"fakeart"


def test_heading_entries_are_excluded_from_the_track_count():
    """The release has 4 tracklist entries but only 3 are type_=="track" —
    the heading must not be counted as a track with no duration."""
    with requests_mock.Mocker() as m:
        m.get("https://api.discogs.com/database/search", json=SEARCH_RESPONSE)
        m.get("https://api.discogs.com/releases/4110478", json=RELEASE_RESPONSE)
        match, _ = _search()
    assert match is not None


def test_any_missing_duration_skips_the_whole_candidate():
    """A release missing even one track's duration can't build a complete
    vector — must be skipped, not padded or guessed."""
    incomplete = {**RELEASE_RESPONSE, "tracklist": [
        {"type_": "track", "position": "1", "duration": "2:44"},
        {"type_": "track", "position": "2", "duration": ""},  # missing
        {"type_": "track", "position": "3", "duration": "2:56"},
    ]}
    with requests_mock.Mocker() as m:
        m.get("https://api.discogs.com/database/search", json=SEARCH_RESPONSE)
        m.get("https://api.discogs.com/releases/4110478", json=incomplete)
        match, _ = _search()
    assert match is None


def test_only_top_3_search_results_are_checked():
    many_results = {"results": [
        {"id": i, "title": f"Album {i}"} for i in range(10)
    ]}
    with requests_mock.Mocker() as m:
        m.get("https://api.discogs.com/database/search", json=many_results)
        release_calls = m.get(requests_mock.ANY,
                              additional_matcher=lambda r: "/releases/" in r.url,
                              json={"tracklist": []})
        _search()
    assert release_calls.call_count <= 3


def test_search_failure_returns_none_not_an_exception():
    with requests_mock.Mocker() as m:
        m.get("https://api.discogs.com/database/search",
             exc=ConnectionError("down"))
        match, _ = _search()
    assert match is None


def test_wrong_durations_are_rejected():
    wrong = {**RELEASE_RESPONSE, "tracklist": [
        {"type_": "track", "duration": "0:50"},
        {"type_": "track", "duration": "1:00"},
        {"type_": "track", "duration": "1:10"},
    ]}
    with requests_mock.Mocker() as m:
        m.get("https://api.discogs.com/database/search", json=SEARCH_RESPONSE)
        m.get("https://api.discogs.com/releases/4110478", json=wrong)
        match, _ = _search()
    assert match is None


def test_mb_orchestrator_lists_discogs_after_itunes():
    src = Path(__file__).resolve().parents[1] / "tagfill" / "stages" / "mb.py"
    text = src.read_text()
    idx_itunes = text.index("itunes.search,")
    idx_discogs = text.index("discogs.search,", idx_itunes)
    assert idx_discogs > idx_itunes, (
        "discogs must be tried only after itunes, not before or instead of it")


def test_discogs_uses_its_own_rate_limiter_not_musicbrainzs():
    src = Path(__file__).resolve().parents[1] / "tagfill" / "stages" / "mb.py"
    text = src.read_text()
    assert "discogs_limiter = RateLimiter(2.5)" in text
    assert '"limiter": discogs_limiter' in text
