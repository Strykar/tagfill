"""Duration-vector metadata sources. One module per source, one shared
contract, so adding or removing a source is "add/delete a file and one
line in mb.py's SOURCES list" — not "find and edit the right lines inside
a 500-line stage file".

This is a refactor of what mb.py grew into after three sources (iTunes,
Discogs) got bolted on as functions inside the stage file that owns
MusicBrainz. That worked, but "remove Discogs" meant deleting specific
lines out of mb.py rather than deleting a file, and "add a fourth source"
meant editing mb.py's run() again. Every source here now returns the same
SourceMatch shape regardless of where it came from, and the orchestrator
(mb.py's run()) just iterates a list.

gate() and _unordered_match() live here, not in any one source module,
because they are the actual safety mechanism every source shares —
duration-vector verification against local track lengths, position-first
then order-independent as a fallback. A source module's only job is:
turn "album name (+ optional artist)" into candidate duration vectors,
hand them to gate(), and if it passes, build a SourceMatch. See
musicbrainz.py's docstring for why the unordered fallback exists and why
its threshold is what it is — that reasoning doesn't belong to any single
source, but the story is told once, there, rather than repeated per file.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field


@dataclass
class SourceMatch:
    """A verified match from one metadata source. `fetch_art` is a
    callable, not already-fetched bytes — a source that never gets used
    (a different source matched first, or the folder didn't need art)
    should never pay for an art HTTP request it didn't need. Each source
    module closes over whatever URL-construction and rate-limiting its own
    art fetch needs; callers just call fetch_art() and get bytes or None.
    """
    id: str                 # "mb:<mbid>", "itunes:<collectionId>", "discogs:<id>"
    title: str
    date: str
    albumartist: str
    evidence: dict
    fetch_art: Callable[[], bytes | None] = field(default=lambda: None)
    genre: str = ""


def _unordered_match(local: list[float], v: list[float],
                     tolerance: float) -> int:
    """Greedy nearest-neighbor bipartite match, order-independent: for each
    local duration, consume the closest still-unused candidate duration
    within tolerance. Good enough for the vector sizes involved (a handful
    to a few dozen tracks); an optimal assignment isn't worth the
    complexity here."""
    remaining = list(v)
    matched = 0
    for a in local:
        best_i, best_d = None, None
        for i, b in enumerate(remaining):
            d = abs(a - b)
            if d <= tolerance and (best_d is None or d < best_d):
                best_i, best_d = i, d
        if best_i is not None:
            remaining.pop(best_i)
            matched += 1
    return matched


def gate(local: list[float], vectors: list[list[float]],
         tolerance: float, pass_fraction: float) -> dict | None:
    """Return evidence dict when some candidate duration vector passes,
    else None. Two tiers: positional first (each local track's duration
    must match its same-position candidate track within tolerance, for at
    least pass_fraction of tracks), then — only if no same-length
    candidate passes positionally — an unordered fallback at the same
    pass_fraction. See musicbrainz.py for why the unordered tier exists
    and why it uses the same threshold as the positional tier rather than
    a stricter one."""
    for i, v in enumerate(vectors):
        if len(v) != len(local):
            continue
        ok = sum(1 for a, b in zip(local, v, strict=False)
                 if b >= 0 and abs(a - b) <= tolerance)
        if ok / len(local) >= pass_fraction:
            return {"tracks": len(local), "within_tolerance": ok,
                    "order": "positional", "vector": i}
    for i, v in enumerate(vectors):
        if len(v) != len(local):
            continue
        positive = [b for b in v if b >= 0]
        ok = _unordered_match(local, positive, tolerance)
        if ok / len(local) >= pass_fraction:
            return {"tracks": len(local), "within_tolerance": ok,
                    "order": "unordered", "vector": i}
    return None
