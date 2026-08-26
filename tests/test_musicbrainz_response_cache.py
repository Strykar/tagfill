"""MusicBrainz search and release responses are cached the way art is.

External review: only art went through _http_cached, so a crash partway
through mb -- or a re-run -- re-spent the whole MusicBrainz budget from
zero. At the 1 req/s their policy asks for, 5,000 unmatched folders times
a search plus up to eight release fetches is a ten-hour ceiling, which
makes retry economics the difference between "resume" and "start again
tomorrow".
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from tagfill.sources import musicbrainz
from tagfill.util import RateLimiter


def test_a_second_call_with_the_same_key_does_not_refetch(tmp_path):
    calls = []

    def fetch():
        calls.append(1)
        return {"release-list": [{"id": "abc"}]}

    args = (tmp_path, ("search", "Dummy", "Portishead"), RateLimiter(0.0))
    first = musicbrainz._json_cached(*args, fetch)
    second = musicbrainz._json_cached(*args, fetch)

    assert first == second == {"release-list": [{"id": "abc"}]}
    assert len(calls) == 1


def test_a_different_key_is_a_different_entry(tmp_path):
    calls = []

    def fetch():
        calls.append(1)
        return {"n": len(calls)}

    musicbrainz._json_cached(tmp_path, ("search", "A"), RateLimiter(0.0), fetch)
    musicbrainz._json_cached(tmp_path, ("search", "B"), RateLimiter(0.0), fetch)
    assert len(calls) == 2


def test_a_failure_is_not_cached(tmp_path):
    """A timeout now must not become a permanent "no such release"."""
    def boom():
        raise ConnectionError("down")

    with pytest.raises(ConnectionError):
        musicbrainz._json_cached(tmp_path, ("search", "A"), RateLimiter(0.0),
                                 boom)
    assert not list(tmp_path.glob("*.json"))


def test_a_half_written_cache_file_is_refetched_not_fatal(tmp_path):
    calls = []

    def fetch():
        calls.append(1)
        return {"ok": True}

    musicbrainz._json_cached(tmp_path, ("k",), RateLimiter(0.0), fetch)
    cached = next(iter(tmp_path.glob("*.json")))
    cached.write_text('{"ok": tr', encoding="utf-8")

    assert musicbrainz._json_cached(tmp_path, ("k",), RateLimiter(0.0),
                                    fetch) == {"ok": True}
    assert len(calls) == 2


def test_the_rate_limiter_is_only_waited_on_a_miss(tmp_path):
    """A cached run should not be paced as if it were talking to the API."""
    waits = []

    class Counting(RateLimiter):
        def wait(self):
            waits.append(1)

    musicbrainz._json_cached(tmp_path, ("k",), Counting(0.0), lambda: {"a": 1})
    musicbrainz._json_cached(tmp_path, ("k",), Counting(0.0), lambda: {"a": 1})
    assert len(waits) == 1


def test_the_search_and_release_calls_both_go_through_it():
    src = Path(musicbrainz.__file__).read_text(encoding="utf-8")
    assert src.count("_json_cached(") >= 3
    assert "musicbrainzngs.search_releases(" in src
    body = src[src.index("def search("):]
    assert "limiter.wait()" not in body, (
        "the cache owns the pacing now; a bare wait would double-pace")


def test_what_lands_on_disk_is_json(tmp_path):
    musicbrainz._json_cached(tmp_path, ("k",), RateLimiter(0.0),
                             lambda: {"release-list": []})
    cached = next(iter(tmp_path.glob("*.json")))
    assert json.loads(cached.read_text(encoding="utf-8")) == {
        "release-list": []}
