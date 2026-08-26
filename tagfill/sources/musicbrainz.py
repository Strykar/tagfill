"""MusicBrainz + Cover Art Archive. The primary source — tried first,
searched by name, keyless, and free to use at MusicBrainz's own asked-for
1 req/s.

Text match alone is not sufficient — wrong-edition and compilation false
positives are the dominant failure mode of artist+album search — so every
candidate faces gate() (see sources/__init__.py), the same verification
every source in this package shares.

A folder with more than one distinct per-track artist is treated as a
candidate compilation (VA release, film soundtrack) instead of being
skipped outright — found live: neither "The Art of Flight OST" nor a
5-disc "VA - Rhythm Is A Dancer" club-classics set has a single consistent
artist, since every track is genuinely by someone different. The search
key is the most common non-empty album value across the folder (a
plurality vote, not a hard match) rather than requiring every track's
album tag to agree, because real compilations often carry a few tracks
still tagged with their *original* unrelated album from wherever they
were first ripped. Verification never looks at per-track artist, and the
write path (mb.py's run()) only ever touches date/albumartist/album,
never a track's own artist, so a compilation's genuinely-different
per-track artists are untouched.

gate()'s unordered fallback exists because of this source specifically:
found live on Pink Floyd's "The Division Bell" — the album folder's .ogg
files carry no track-number prefix, so they sort alphabetically ("A Great
Day For Freedom" before "Cluster One") rather than by track order, and
positional comparison can never pass even against the correct release.
Every real MusicBrainz candidate had exactly 11 tracks, matching
local_tracks exactly, yet all were rejected on ordering alone.

Getting the unordered threshold right took two tries. The first instinct
was to demand every track match (11/11), reasoning that giving up
position ought to cost something. Tested against the real Division Bell
candidate and that was wrong: even the correct release has one track
(High Hopes, 511s local vs. 478s on this pressing) outside tolerance —
the same real mismatch already visible in the ordered pass for the
sibling FLAC folder (10/11, accepted at pass_fraction=0.90). Demanding
11/11 unordered would reject the exact case the fallback exists for. It
uses the same pass_fraction as the positional tier instead.

Requires `musicbrainzngs` and `requests`, and a contact address in the
config — MusicBrainz requires an identifying user agent, and mb.py
refuses to run this source at all without one.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from ..util import RateLimiter, is_mb_placeholder, similarity
from . import SourceMatch, gate


def _http_cached(url: str, cache_dir: Path, limiter: RateLimiter,
                 headers: dict | None = None) -> bytes | None:
    import requests
    cache_dir.mkdir(parents=True, exist_ok=True)
    key = cache_dir / hashlib.sha1(url.encode()).hexdigest()
    if key.exists():
        return key.read_bytes() or None
    limiter.wait()
    try:
        r = requests.get(url, headers=headers or {}, timeout=30)
    except Exception:
        return None
    if r.status_code != 200:
        key.write_bytes(b"")
        return None
    key.write_bytes(r.content)
    return r.content


def _top_genre(release: dict) -> str:
    """MusicBrainz has no dedicated curated-genre include on this API
    version (only community folksonomy "tags", checked live: 'genres' is
    rejected as an invalid include) -- the release-group's tags are the
    less noisy of the two (a release's own tags are sparser and more
    likely a single one-off vote), so prefer those. Highest vote count
    wins; a tag with a count of 1 among twenty is not a genre, it's someone
    being cute."""
    tags = (release.get("release-group", {}).get("tag-list")
           or release.get("tag-list") or [])
    if not tags:
        return ""
    return max(tags, key=lambda t: int(t.get("count", 0))).get("name", "")


def _duration_vectors(release: dict) -> list[list[float]]:
    """Per-medium duration vectors in seconds, plus the whole-release
    concatenation when there is more than one medium."""
    media = release.get("medium-list", [])
    vectors = []
    for medium in media:
        v = []
        for track in medium.get("track-list", []):
            length = (track.get("length")
                      or track.get("recording", {}).get("length"))
            v.append(float(length) / 1000.0 if length else -1.0)
        if v:
            vectors.append(v)
    if len(vectors) > 1:
        vectors.append([d for v in vectors for d in v])
    return vectors


def search(*, artist: str | None, album: str, compilation: bool,
          local: list[float], tolerance: float, pass_fraction: float,
          limiter: RateLimiter, cache_dir: Path,
          mb_app: str, mb_contact: str, tool_version: str,
          **_ignored) -> tuple[SourceMatch | None, list[dict]]:
    """Search MusicBrainz, verify every same-length candidate against
    gate(), and return the first that passes plus the rejection evidence
    for every candidate that didn't (so the caller can journal *why* on a
    total miss). `compilation` selects a release-title-only search
    (no artist constraint) instead of artist+release.

    MusicBrainz requires an identifying user agent on every request;
    mb_app/mb_contact/tool_version set it fresh on every call (cheap,
    idempotent) so this module never needs the caller to have configured
    musicbrainzngs's global state beforehand."""
    try:
        import musicbrainzngs
    except ImportError:
        return None, []
    if not mb_contact:
        return None, []
    musicbrainzngs.set_useragent(mb_app, tool_version, mb_contact)

    limiter.wait()
    try:
        if compilation:
            result = musicbrainzngs.search_releases(release=album, limit=8)
        else:
            result = musicbrainzngs.search_releases(artist=artist,
                                                    release=album, limit=8)
    except Exception:
        return None, []

    rejections = []
    for cand in result.get("release-list", []):
        rid = cand["id"]
        limiter.wait()
        try:
            rel = musicbrainzngs.get_release_by_id(
                rid, includes=["recordings", "release-groups",
                              "artist-credits", "tags"])["release"]
        except Exception:
            continue
        vectors = _duration_vectors(rel)
        evidence = gate(local, vectors, tolerance, pass_fraction)
        if not evidence:
            rejections.append({"mbid": rid,
                               "counts": [len(v) for v in vectors]})
            continue

        # _duration_vectors appends a whole-release concatenation after the
        # per-medium vectors. If that is what matched, position within the
        # folder is position across the *whole release*, so track numbers
        # derived from it would be wrong from disc 2 onward.
        if len(vectors) > 1 and evidence.get("vector") == len(vectors) - 1:
            evidence["multi_medium"] = True
        evidence.update(mbid=rid,
                        title_sim=round(similarity(album,
                                                  rel.get("title", "")), 2))
        date = rel.get("date", "")
        albumartist = "; ".join(
            c.get("artist", {}).get("name", "")
            for c in rel.get("artist-credit", []) if isinstance(c, dict)
            and not is_mb_placeholder(c.get("artist", {}).get("name"))
        ) or artist or "Various Artists"
        title = rel.get("title", "")
        if is_mb_placeholder(title):
            title = ""

        def fetch_art(rid=rid):
            for url in (f"https://coverartarchive.org/release/{rid}/front-500",
                        f"https://coverartarchive.org/release/{rid}/front"):
                art = _http_cached(url, cache_dir, limiter)
                if art:
                    return art
            return None

        return SourceMatch(id=f"mb:{rid}", title=title, date=date,
                          albumartist=albumartist, evidence=evidence,
                          fetch_art=fetch_art,
                          genre=_top_genre(rel)), rejections
    return None, rejections
