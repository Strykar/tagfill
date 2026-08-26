"""Discogs as a degraded last resort — tried only when neither
MusicBrainz nor iTunes finds a match. "Degraded" is deliberate: Discogs
durations are community-submitted strings ("2:44"), not derived from the
audio like iTunes', frequently missing outright, and the unauthenticated
API is capped at 25 requests/minute (confirmed live via the
x-discogs-ratelimit response header, far tighter than iTunes'). Pass a
RateLimiter dedicated to this source — its budget must never share
MusicBrainz's or iTunes'.

Reuses gate() unchanged, same as every source in this package — the only
thing that changes per source is where the candidate duration vector
comes from.
"""

from __future__ import annotations

import re

from ..util import RateLimiter
from . import SourceMatch, gate


def _parse_duration(s: str) -> float | None:
    """"M:SS" or "MM:SS" -> seconds. None means "skip this track", not
    zero — a release missing even one track's duration can't build a
    complete vector."""
    if not s or ":" not in s:
        return None
    parts = s.strip().split(":")
    try:
        parts = [int(p) for p in parts]
    except ValueError:
        return None
    seconds = 0
    for p in parts:
        seconds = seconds * 60 + p
    return float(seconds)


def search(*, album: str, local: list[float], tolerance: float,
          pass_fraction: float, limiter: RateLimiter,
          **_ignored) -> tuple[SourceMatch | None, list[dict]]:
    """Checked only the top 3 search results to respect the tight rate
    budget — this is meant to catch the rare thing neither MusicBrainz
    nor iTunes has, not to be a bulk search tier."""
    import requests

    from .. import __version__
    headers = {"User-Agent":
               f"tagfill/{__version__} (+https://github.com/Strykar/tagfill)"}
    try:
        limiter.wait()
        # An external review called this endpoint a release blocker:
        # "Discogs has required authentication on search since ~2014,
        # unauthenticated requests get 401". Checked live twice, both with
        # and without a user agent: HTTP 200, 50 results, and the
        # x-discogs-ratelimit: 25 header this module's rate limit is set
        # from comes back on this request, not on /releases/{id}. The
        # production journal agrees -- art_from=discogs on 12 files of a
        # real collection. Authentication raises the quota; it does not
        # gate the endpoint.
        r = requests.get("https://api.discogs.com/database/search",
                         params={"q": album, "type": "release"},
                         headers=headers, timeout=30)
        results = r.json().get("results", [])
    except Exception:
        return None, []

    for res in results[:3]:
        rid = res.get("id")
        if not rid:
            continue
        try:
            limiter.wait()
            rr = requests.get(f"https://api.discogs.com/releases/{rid}",
                              headers=headers, timeout=30)
            rel = rr.json()
        except Exception:
            continue
        tracks = [t for t in rel.get("tracklist", [])
                 if t.get("type_") == "track"]
        durations = [_parse_duration(t.get("duration")) for t in tracks]
        if len(durations) != len(local) or any(d is None for d in durations):
            continue
        evidence = gate(local, [durations], tolerance, pass_fraction)
        if not evidence:
            continue
        evidence["source"] = "discogs"

        images = rel.get("images") or []
        art_url = images[0].get("uri") if images else res.get("cover_image")

        def fetch_art(url=art_url):
            if not url:
                return None
            import requests as _requests
            limiter.wait()
            try:
                dr = _requests.get(url, headers=headers, timeout=30)
                if dr.status_code == 200 and dr.content:
                    return dr.content
            except Exception:
                pass
            return None

        # Discogs disambiguates same-named artists with a numeric suffix
        # ("Prince (2)"). That is a database artefact, not part of the
        # name, and writing it into a tag is wrong everywhere it lands.
        albumartist = "; ".join(
            re.sub(r"\s*\(\d+\)$", "", a.get("name", ""))
            for a in rel.get("artists", []))
        genres = rel.get("genres") or []
        return SourceMatch(
            id=f"discogs:{rid}", title=rel.get("title", album),
            date=str(rel.get("released") or rel.get("year") or ""),
            albumartist=albumartist, evidence=evidence,
            fetch_art=fetch_art, genre=genres[0] if genres else ""), []
    return None, []
