"""iTunes as a duration-vector source — not to be confused with
stages/itunes.py, which is a *different* stage doing art-only text-
similarity matching. This module exists specifically for mb.py's
fallback chain: when MusicBrainz's search has no catalogued match at
all, iTunes' own per-track lookup can supply an independent duration
vector to verify against, using the exact same gate() every source
shares.

Found live: iTunes has "Evening Ragas, Vol. 1" and "Vol. 4", both absent
from MusicBrainz's top search results, with every track's duration
matching the local files to the millisecond (1803.760s iTunes vs
1803.76s local). Keyless, and effectively unthrottled in practice — no
documented rate limit, unlike Discogs.
"""

from __future__ import annotations

from ..util import RateLimiter
from . import SourceMatch, gate


def search(*, album: str, local: list[float], tolerance: float,
          pass_fraction: float, limiter: RateLimiter,
          art_sizes: list[int] = (1200, 600),
          **_ignored) -> tuple[SourceMatch | None, list[dict]]:
    """iTunes has no artist-only or album-only release search distinct
    from a plain text query — `album` alone is the whole query, same as
    the Discogs source. Returns (match, []) since this source has no
    reject-evidence trail like MusicBrainz's candidate loop; a miss here
    is just "nothing matched", not "these N candidates were considered
    and failed"."""
    import requests
    try:
        limiter.wait()
        r = requests.get("https://itunes.apple.com/search",
                         params={"entity": "album", "limit": 5, "term": album},
                         timeout=30)
        results = r.json().get("results", [])
    except Exception:
        return None, []

    for res in results:
        if res.get("trackCount") != len(local):
            continue
        cid = res.get("collectionId")
        if not cid:
            continue
        try:
            limiter.wait()
            tr = requests.get("https://itunes.apple.com/lookup",
                              params={"id": cid, "entity": "song"}, timeout=30)
            tracks = sorted(
                (t for t in tr.json().get("results", [])
                 if t.get("wrapperType") == "track"),
                key=lambda t: t.get("trackNumber") or 0)
        except Exception:
            continue
        vector = [(t.get("trackTimeMillis") or 0) / 1000.0 for t in tracks]
        if len(vector) != len(local):
            continue
        evidence = gate(local, [vector], tolerance, pass_fraction)
        if not evidence:
            continue
        evidence["source"] = "itunes"

        base = res.get("artworkUrl100")

        def fetch_art(base=base):
            if not base:
                return None
            import requests as _requests
            for size in art_sizes:
                candidate = base.replace("100x100bb", f"{size}x{size}bb")
                limiter.wait()
                try:
                    ir = _requests.get(candidate, timeout=30)
                except Exception:
                    continue
                if ir.status_code == 200 and ir.content:
                    return ir.content
            return None

        return SourceMatch(
            id=f"itunes:{cid}", title=res.get("collectionName", album),
            date=(res.get("releaseDate") or "")[:10],
            albumartist=res.get("artistName", ""), evidence=evidence,
            fetch_art=fetch_art,
            genre=res.get("primaryGenreName", "")), []
    return None, []
