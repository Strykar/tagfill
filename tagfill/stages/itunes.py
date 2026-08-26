"""Stage 6: iTunes Search art fallback. Keyless.

Grouping is by (folder, artist, album), not folder alone. Found live: a
"flat dump" folder (auto-downloaded YouTube rips, one file per unrelated
song, no per-album subfolders) mixes many genuinely different artists and
albums in one directory — Daft Punk, Deadmau5, John Mayer, Melissa
Etheridge all sat side by side. Grouping by folder alone used only the
first eligible row's artist/album as the search query for the *entire*
folder, then embedded whatever art it found onto every file in that folder
still needing art — regardless of whether they were the same song. Checked
against the real journal: this had already run three times and been
rejected every time by the similarity gate (score 0.2), by luck rather than
correctness — a well-known album as the first row would have produced a
high-confidence match and embedded its cover onto every unrelated track in
the folder. mb.py already required `len(artists) == 1 and len(albums) == 1`
before trusting a folder; itunes.py never did. Grouping on the full
(folder, artist, album) tuple gives every distinct song its own group, so a
real album folder still groups its tracks together exactly as before, and a
flat dump correctly splits into one group per song.

For folders whose art Cover Art Archive could not supply. Artwork comes from
rewriting `artworkUrl100`: the `100x100bb` component accepts other sizes, so
the configured ladder (default 1200 then 600) is tried in order. There is a
ceiling — absurd sizes 404 — which is why it is a ladder and not a single
optimistic request.

Both artist and album name must clear a similarity gate against the returned
`artistName`/`collectionName`; a keyless text search with no duration data is
exactly where a false positive would slip through otherwise.

That gate alone isn't enough. Found live: "Random Access Memories (Édition
Studio Masters)" scored 0.69 against iTunes' "Random Access Memories
(Drumless Edition)" — a different, instrumental-only product, confirmed by a
0.67 normalized pixel difference against the real album's cover — while the
correct plain "Random Access Memories" sat right there in the same result
set at 0.66, edged out by pure text-overlap noise (an unusual local
qualifier happening to share more characters with "Drumless Edition" than
with the plain title). The top-scoring candidate must beat the runner-up by
`itunes_min_margin` (default 0.15) or the folder is rejected as ambiguous
rather than gambling on a coin-flip margin. Checked against every accept in
a real run: 7/9 had margins of 0.21 or higher; only the Drumless case (0.03)
and a genuine tie between two punctuation variants of one title (0.00, "X:
Y" vs "X - Y") fall under the bar.

That second kind of tie turned out to be too conservative. Checked live: a
margin=0.0 tie on "Wander This World" (Jonny Lang, 1998) was two iTunes
storefront listings of the *same* release — different `collectionId`s,
different asset paths, but their cover art is pixel-identical (0.0000
normalized difference on a 64x64 downsample). Rejecting that outright cost
a real, correct match for no reason: unlike Drumless/Édition (a genuinely
different product), a same-name near-tie is common when iTunes catalogues
one release once per territory. So an ambiguous margin no longer means an
automatic reject: `_same_image()` fetches both candidates' art and compares
pixels first. Identical (or near-identical, allowing for recompression
noise) art means the disagreement doesn't matter, so the top-scored
candidate is used. Only a real content difference is rejected.

A high miss rate on non-mainstream catalogues is expected and acceptable: the
report stage's unresolved list is what decides whether a further source is
worth adding.
"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from pathlib import Path

from .. import probe
from ..journal import Record
from ..util import RateLimiter, similarity
from . import Context, StagePrecondition, guarded_write

_SAME_IMAGE_TOLERANCE = 0.03  # normalized RGB distance on a 32x32 downsample


def _same_image(data_a: bytes, data_b: bytes,
                tolerance: float = _SAME_IMAGE_TOLERANCE) -> bool:
    """Cheap perceptual check: are these two images the same picture, give
    or take recompression noise? Downsamples both to 32x32 and compares mean
    per-channel distance. Used to recover an ambiguous-margin tie when the
    two candidates turn out to be the same release listed twice (different
    storefronts), rather than genuinely different products."""
    try:
        import io

        from PIL import Image
        a = Image.open(io.BytesIO(data_a)).convert("RGB").resize((32, 32))
        b = Image.open(io.BytesIO(data_b)).convert("RGB").resize((32, 32))
    except Exception:
        return False
    pa, pb = list(a.getdata()), list(b.getdata())
    diff = sum(abs(x[0] - y[0]) + abs(x[1] - y[1]) + abs(x[2] - y[2])
              for x, y in zip(pa, pb, strict=True)) / (32 * 32 * 3 * 255)
    return diff <= tolerance


def _fetch(url: str, limiter: RateLimiter, timeout: int = 30) -> bytes | None:
    import requests
    limiter.wait()
    try:
        r = requests.get(url, timeout=timeout)
    except Exception:
        return None
    return r.content if r.status_code == 200 and r.content else None


def run(ctx: Context) -> None:
    try:
        import requests
    except ImportError:
        raise StagePrecondition("pip install requests") from None
    from . import census
    from .art_local import needs_art, row_needs_art
    limiter = RateLimiter(0.5)
    cache = ctx.workdir / "cache" / "http"
    cache.mkdir(parents=True, exist_ok=True)

    groups: dict[tuple[str, str, str], list[dict]] = defaultdict(list)
    for r in census.load(ctx):
        if (not r["issue"] and row_needs_art(r, ctx.cfg.art_min_px)
                and r["artist"] and r["album"]):
            key = (str(Path(r["path"]).parent), r["artist"], r["album"])
            groups[key].append(r)

    handled = 0
    for (folder, artist, album), rows in sorted(groups.items()):
        if ctx.limit and handled >= ctx.limit:
            break
        paths = [ctx.root / r["path"] for r in rows]
        if not all(ctx.within_scope(p) for p in paths):
            continue

        url = ("https://itunes.apple.com/search?entity=album&limit=5&term="
               + requests.utils.quote(f"{artist} {album}"))
        key = cache / hashlib.sha1(url.encode()).hexdigest()
        if key.exists():
            body = key.read_bytes()
        else:
            limiter.wait()
            try:
                body = requests.get(url, timeout=30).content
            except Exception:
                continue
            key.write_bytes(body)
        try:
            results = json.loads(body).get("results", [])
        except json.JSONDecodeError:
            continue

        scored = sorted(
            ((min(similarity(artist, res.get("artistName", "")),
                 similarity(album, res.get("collectionName", ""))), res)
             for res in results),
            key=lambda t: t[0], reverse=True)
        best_sim, best = scored[0] if scored else (0.0, None)
        second_sim = scored[1][0] if len(scored) > 1 else 0.0
        margin = best_sim - second_sim
        tie_recovered = False
        if not best or best_sim < 0.60 or not best.get("artworkUrl100"):
            ctx.journal.append(Record(stage="itunes", path=folder,
                                      action="reject", field="art",
                                      evidence={"reason": "no match above 0.60",
                                                "query": f"{artist} - {album}",
                                                "best_sim": round(best_sim, 2)}))
            handled += 1
            continue
        if margin < ctx.cfg.itunes_min_margin:
            second = scored[1][1]
            thumb_a = _fetch(best["artworkUrl100"], limiter)
            thumb_b = _fetch(second.get("artworkUrl100", ""), limiter) \
                if second.get("artworkUrl100") else None
            same = (thumb_a and thumb_b and _same_image(thumb_a, thumb_b))
            if not same:
                ctx.journal.append(Record(
                    stage="itunes", path=folder, action="reject", field="art",
                    evidence={"reason": "ambiguous: runner-up too close",
                              "query": f"{artist} - {album}",
                              "best_sim": round(best_sim, 2),
                              "best_name": best.get("collectionName"),
                              "second_sim": round(second_sim, 2),
                              "second_name": second.get("collectionName"),
                              "margin": round(margin, 2)}))
                handled += 1
                continue
            # Same picture either way, so the margin doesn't matter here —
            # proceed with the top-scored candidate.
            tie_recovered = True

        art = None
        for size in ctx.cfg.itunes_art_sizes:
            candidate = best["artworkUrl100"].replace(
                "100x100bb", f"{size}x{size}bb")
            art = _fetch(candidate, limiter)
            if art:
                break
        if not art:
            ctx.journal.append(Record(
                stage="itunes", path=folder, action="reject", field="art",
                evidence={"reason": "no artwork URL resolved",
                          "query": f"{artist} - {album}"}))
            handled += 1
            continue
        px = probe.image_min_px(art)
        if not px or px < ctx.cfg.art_min_px:
            # Same silent gap found live in mb.py: art was fetched, just
            # under the size floor. Without this record it's
            # indistinguishable from "no art was ever found" from the
            # outside.
            ctx.journal.append(Record(
                stage="itunes", path=folder, action="reject", field="art",
                evidence={"reason": "undersized", "px": px,
                          "query": f"{artist} - {album}"}))
            handled += 1
            continue
        evidence = {"source": "itunes", "sim": round(best_sim, 2),
                    "collection": best.get("collectionName"),
                    "margin": round(margin, 2),
                    "tie_recovered_same_image": tie_recovered}
        for row, path in zip(rows, paths, strict=True):
            if not needs_art(path, ctx.cfg.art_min_px):
                continue
            if not ctx.apply:
                ctx.journal.append(Record(stage="itunes", path=row["path"],
                                          action="propose", field="art",
                                          new=f"{px}px", evidence=evidence))
            else:
                checked = probe.validate_art(art, 0)
                if not checked:
                    ctx.journal.append(Record(
                        stage="itunes", path=row["path"], action="reject",
                        field="art",
                        evidence={"reason": "not a usable image", "px": px}))
                    continue
                data, mime, px = checked
                ok, _ = guarded_write(ctx, "itunes", row["path"],
                                      probe.embed_art, path, data, mime)
                if ok:
                    ctx.journal.record_write("itunes", ctx.root, path, "art",
                                             None, f"{px}px",
                                             evidence=evidence)
        handled += 1
    ctx.say(f"itunes: {handled} folders handled "
            f"({'applied' if ctx.apply else 'dry run'})")
