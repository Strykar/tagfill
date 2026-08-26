"""Stage 4: metadata sources, tried in order.

Orchestrates tagfill/sources/ — one module per source (musicbrainz,
itunes, discogs), each returning the same SourceMatch shape regardless of
where it came from. This stage's only job is: build the local duration
vector and candidate album name for a folder, hand both to each source in
turn until one verifies, then apply the result. All of the actual
matching logic — the duration-vector gate, the compilation/plurality-vote
album naming, the unordered-match fallback, why iTunes and Discogs are
ordered where they are — lives in sources/, not here. See
sources/musicbrainz.py for the primary story (it's the source most of the
interesting reasoning belongs to) and sources/discogs.py for why that one
is deliberately last.

On acceptance: source-supplied art (Cover Art Archive for MusicBrainz,
each source's own ladder/URL otherwise) fills embedded art, and
album/date/albumartist fill where empty. Genre and track number fill too,
gated behind config's [collection] extra_tags — track number only when
the match's gate() evidence proves positional order, since an unordered
match confirms the album but not which local file is which track. Never
touches a track's own artist — a compilation's genuinely different
per-track artists are untouched by design.

Requires a contact address in the config for the MusicBrainz source —
MusicBrainz requires an identifying user agent. The iTunes and Discogs
sources need no configuration at all.
"""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path

from .. import probe
from ..journal import Record
from ..sources import SourceMatch, discogs, itunes, musicbrainz
from ..util import RateLimiter
from . import Context, StagePrecondition, guarded_write


def run(ctx: Context) -> None:
    if not ctx.cfg.mb_contact:
        raise StagePrecondition(
            "set [musicbrainz].contact in the config first (MusicBrainz "
            "requires an identifying user agent)")
    try:
        import requests  # noqa: F401 -- every source needs it; fail fast here
    except ImportError:
        raise StagePrecondition("pip install musicbrainzngs requests") from None
    from .. import __version__
    mb_limiter = RateLimiter(ctx.cfg.mb_rate_s)
    itunes_limiter = RateLimiter(0.5)
    discogs_limiter = RateLimiter(2.5)  # 25 req/min unauthenticated, own budget
    cache = ctx.workdir / "cache" / "http"
    from . import census
    from .art_local import row_needs_art

    tol = ctx.cfg.mb_duration_tolerance_s
    pass_fraction = ctx.cfg.mb_vector_pass_fraction

    # Ordered list of sources to try. Add or remove a source here — no
    # other change needed anywhere in this file. Each entry's kwargs are
    # merged with the common ones (artist, album, compilation, local,
    # tolerance, pass_fraction) at call time; a source that doesn't use a
    # given kwarg just ignores it via **_ignored in its own signature.
    sources = [
        (musicbrainz.search, {"limiter": mb_limiter, "cache_dir": cache,
                              "mb_app": ctx.cfg.mb_app,
                              "mb_contact": ctx.cfg.mb_contact,
                              "tool_version": __version__}),
        (itunes.search, {"limiter": itunes_limiter,
                         "art_sizes": ctx.cfg.itunes_art_sizes}),
        (discogs.search, {"limiter": discogs_limiter}),
    ]

    folders: dict[str, list[dict]] = defaultdict(list)
    for r in census.load(ctx):
        if not r["issue"]:
            folders[str(Path(r["path"]).parent)].append(r)

    handled = 0
    for folder, rows in sorted(folders.items()):
        if ctx.limit and handled >= ctx.limit:
            break
        artists = {r["artist"] for r in rows}
        albums = {r["album"] for r in rows}
        compilation = False
        if (len(artists) == 1 and len(albums) == 1
                and "" not in artists and "" not in albums):
            artist, album = artists.pop(), albums.pop()
        else:
            # A folder with more than one distinct artist is a candidate
            # compilation (VA release, film soundtrack), not a bad rip.
            # See sources/musicbrainz.py for why, and why the search key
            # is a plurality vote over album values rather than a hard
            # match.
            non_empty_albums = [r["album"] for r in rows if r["album"]]
            if len(artists) < 2 or not non_empty_albums:
                continue
            album = max(set(non_empty_albums), key=non_empty_albums.count)
            artist = None
            compilation = True
        # extra_tags counts as "needed" too, or enabling a new field does
        # nothing for music already swept.
        extra = [f for f in ("genre", "tracknumber") if f in ctx.cfg.extra_tags]
        need = [r for r in rows
                if row_needs_art(r, ctx.cfg.art_min_px)
                or not r["date"] or not r["albumartist"]
                or any(not r.get(f) for f in extra)]
        if not need:
            continue
        paths = [ctx.root / r["path"] for r in rows]
        if not all(ctx.within_scope(p) for p in paths):
            continue
        # The guard means "already wrote what we could to these files",
        # which is right until the set of fields we write grows. Hence
        # --recheck.
        if not ctx.recheck and all(
                ctx.journal.already_done("mb", ctx.root, p) for p in paths):
            continue
        local = [float(r["duration"] or 0) for r in rows]

        match: SourceMatch | None = None
        all_rejections = []
        later = []
        for i, (search_fn, extra_kwargs) in enumerate(sources):
            match, rejections = search_fn(
                artist=artist, album=album, compilation=compilation,
                local=local, tolerance=tol, pass_fraction=pass_fraction,
                **extra_kwargs)
            all_rejections.extend(rejections)
            if match:
                later = sources[i + 1:]
                break

        if not match:
            ctx.journal.append(Record(
                stage="mb", path=folder, action="reject", field="release",
                evidence={"artist": artist or "Various Artists (compilation)",
                          "album": album,
                          "reason": "no candidate passed the duration-vector "
                                    "gate" if all_rejections else
                                    "no candidates found",
                          "rejected": all_rejections[:5],
                          "local_tracks": len(local)}))
            handled += 1
            continue

        # Fetch only once something actually needs art. This used to run
        # before the apply check, so a dry run over a large collection paid
        # a Cover Art Archive request pair per matched folder -- the exact
        # cost SourceMatch.fetch_art is a callable to avoid.
        need_art = any(row_needs_art(r, ctx.cfg.art_min_px) for r in rows)
        art = match.fetch_art() if (ctx.apply and need_art) else None

        # The winner owns identity; later sources only fill its blanks.
        # MusicBrainz often matches an album but carries no genre, and
        # iTunes usually has one. Chase a gap only when filling it would
        # change a file, or every match pays for a Discogs query at 2.5s.
        writable = ["date", "albumartist"] + [
            f for f in ("genre",) if f in ctx.cfg.extra_tags]
        fillable = [f for f in writable
                    if not getattr(match, f, "")
                    and any(not r.get(f) for r in rows)]
        want_art = art is None and any(row_needs_art(r, ctx.cfg.art_min_px)
                                       for r in rows)
        for search_fn, extra_kwargs in later:
            if not fillable and not want_art:
                break
            other, _ = search_fn(
                artist=artist, album=album, compilation=compilation,
                local=local, tolerance=tol, pass_fraction=pass_fraction,
                **extra_kwargs)
            if not other:
                continue
            for f in list(fillable):
                value = getattr(other, f, "")
                if value:
                    setattr(match, f, value)
                    fillable.remove(f)
                    match.evidence[f"{f}_from"] = other.evidence.get(
                        "source", other.id.split(":")[0])
            if want_art:
                art = other.fetch_art()
                if art:
                    want_art = False
                    match.evidence["art_from"] = other.evidence.get(
                        "source", other.id.split(":")[0])
        # idx+1 is only the real track number when gate() matched this
        # folder positionally. An unordered match proves the album but not
        # which file is which track; a concatenated multi-medium one is
        # positional across the whole release, numbering disc 2 from 13.
        positional = (match.evidence.get("order") == "positional"
                      and not match.evidence.get("multi_medium"))
        # Once per folder, not once per track: decoding a 1200x1200 JPEG
        # sixteen times to embed the same bytes sixteen times is pure
        # waste. min_px=0 so an image that decodes but is too small stays
        # distinguishable from one that is not an image at all.
        checked = probe.validate_art(art, 0) if art else None
        no_pillow = art is not None and not probe.pillow_available()
        usable = checked if checked and checked[2] >= ctx.cfg.art_min_px \
            else None
        if art and not usable:
            # Found live: a source had real art for this release but under
            # the size floor -- the decline is correct, but silence here
            # looked identical to "no art fetch attempted" from outside.
            for row in rows:
                ctx.journal.append(Record(
                    stage="mb", path=row["path"], action="reject",
                    field="art",
                    evidence={"reason": (
                        "pillow not installed, so no image can be checked"
                        if no_pillow else
                        "undersized" if checked else "not a usable image"),
                        "px": checked[2] if checked else None}))
        for idx, row in enumerate(rows):
            path = ctx.root / row["path"]
            values = {"date": match.date, "albumartist": match.albumartist,
                      "album": match.title}
            if "genre" in ctx.cfg.extra_tags:
                values["genre"] = match.genre
            if positional and "tracknumber" in ctx.cfg.extra_tags:
                values["tracknumber"] = str(idx + 1)
            if not ctx.apply:
                ctx.journal.append(Record(stage="mb", path=row["path"],
                                          action="propose", field="release",
                                          new=match.id, evidence=match.evidence))
                continue
            # Fields and art in one save. probe.write's own read decides
            # whether this file still needs art, so the separate needs_art
            # parse is gone too.
            ok, changed = guarded_write(
                ctx, "mb", row["path"], probe.write, path, values,
                overwrite=ctx.overwrite,
                art=(usable[0], usable[1]) if usable else None,
                art_min_px=ctx.cfg.art_min_px)
            if not ok:
                continue
            for f, old, new in changed:
                ctx.journal.record_write("mb", ctx.root, path, f, old, new,
                                         evidence=match.evidence)
        handled += 1
    ctx.say(f"mb: {handled} folders handled "
            f"({'applied' if ctx.apply else 'dry run'})")
