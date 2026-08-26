"""Stage 3: tags-from-filename. No network.

Parses `Artist - Title (Mix) [Label]` from the stem. Before splitting, a
prefix stripper removes, in order: Camelot key prefixes (`1A`..`12B`, and
`1m`/`12d` Open Key forms), BPM prefixes (a bare 2-3 digit number, 60-200),
and leading track numbers. So a DJ-pool filename like
`2A - 128 - 03 Kliment Serial Spider` strips to `Kliment Serial Spider`
rather than yielding the artist "2A".

Rules:
- Never overwrites a non-empty field.
- `[Label]` goes to the label field, never to album.
- A parse carries a confidence score. Above threshold it is proposed; below,
  it goes to the review queue CSV for a human decision, and an edited queue
  feeds back in via `--from-review`.

Crate policy: folders matching `crates.globs` in the config are playlists,
not releases. Writing `album=<folder>` would manufacture fake multi-artist
albums in every album-browsing player and poison later MusicBrainz matching
through album-field mismatch penalties — and it gains nothing, because the
path already encodes crate membership and tagfill never moves files. So
for crate files the folder name goes to `grouping` and album stays empty.

The parser half of this module is pure and table-tested; only `run()` touches
files.
"""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from . import guarded_write

# -- pure parser -------------------------------------------------------------

_CAMELOT = re.compile(r"^(?:[1-9]|1[0-2])(?:[ABab]|[md])$")
_BPM = re.compile(r"^\d{2,3}$")
_TRACKNO = re.compile(r"^\d{1,3}[.)]?$")
_SEP = re.compile(r"\s+[-\u2013\u2014]\s+")
_LABEL = re.compile(r"\[([^\[\]]+)\]\s*$")


@dataclass
class Parse:
    artist: str | None
    title: str | None
    label: str | None
    confidence: float
    stripped: list[str]


def _strip_prefixes(stem: str) -> tuple[str, list[str]]:
    """Iteratively remove Camelot / BPM / track-number lead tokens, each
    optionally followed by a separator. Bounded, and never strips the whole
    stem: something must remain to be a title."""
    stripped: list[str] = []
    s = stem.strip()
    for _ in range(4):
        m = re.match(r"^(\S+)([\s._-]+)(?=\S)", s)
        if not m:
            break
        tok = m.group(1)
        is_prefix = (_CAMELOT.match(tok)
                     or (_BPM.match(tok) and 60 <= int(tok) <= 200)
                     or _TRACKNO.match(tok))
        if not is_prefix:
            break
        rest = s[m.end():]
        if len(rest.strip()) < 3:
            break
        stripped.append(tok)
        s = rest
    return s.strip(), stripped


def clean_stem(stem: str) -> str:
    """The prefix-stripped stem, for use as a search query elsewhere."""
    s, _ = _strip_prefixes(stem.replace("_", " "))
    return s


def folder_is_track_numbered(stems: list[str]) -> bool:
    """Do these filenames look like a numbered tracklist?

    The signal that tells "05 Moby - Porcelain" (track five) apart from
    "50 Cent - In Da Club" (a name that opens with a number). Nothing in
    either filename distinguishes them -- only the company they keep does.
    """
    nums = []
    for stem in stems:
        m = re.match(r"^(\d{1,3})[.)]?[\s._-]", stem.strip())
        if m:
            nums.append(int(m.group(1)))
    return len(nums) >= 2 and len(set(nums)) >= 2 and max(nums) <= len(stems) + 5


def parse_stem(stem: str, *, numbered_folder: bool | None = None) -> Parse:
    """`numbered_folder` says whether the siblings look like a tracklist.
    None means nobody checked, which is treated as unknown rather than as
    no -- an unknown leading number lands in the review queue instead of
    being written as an artist name."""
    s, stripped = _strip_prefixes(stem.replace("_", " "))
    label = None
    m = _LABEL.search(s)
    if m:
        label = m.group(1).strip()
        s = s[:m.start()].strip()

    # A leading number is only safely a track number when the siblings
    # agree. Without that, "50 Cent - In Da Club" was being written as
    # artist "Cent" at 0.80 -- above the default threshold, so unreviewed,
    # on exactly the untagged files this stage targets.
    bare_number = bool(stripped) and _TRACKNO.match(stripped[0]) is not None
    if bare_number and numbered_folder is False:
        s, stripped = stem.replace("_", " ").strip(), []
        m = _LABEL.search(s)
        if m:
            s = s[:m.start()].strip()

    parts = _SEP.split(s)
    conf = 0.85
    if stripped:
        conf -= 0.05  # a stripped prefix means the layout was noisy
    if bare_number and numbered_folder is None and len(_SEP.split(s)) >= 2:
        conf -= 0.20  # ambiguous leading number: let a human look
    if len(parts) >= 2:
        artist = parts[0].strip()
        title = " - ".join(p.strip() for p in parts[1:]).strip()
        if len(parts) > 2:
            conf -= 0.15  # ambiguous split: which dash divides artist/title?
        for side in (artist, title):
            if len(side) < 2 or side.isdigit():
                conf -= 0.30
        if label:
            conf += 0.05  # Beatport-style [Label] corroborates the shape
        return Parse(artist or None, title or None, label,
                     round(max(0.0, min(1.0, conf)), 2), stripped)
    # No separator left: title-only, low confidence, review queue.
    return Parse(None, s or None, label, 0.30, stripped)


def crate_grouping(rel_path: Path, crate_globs: list[str]) -> str | None:
    """If the file sits inside a crate folder, return the crate name for the
    grouping field.

    Matched in POSIX form so the result does not depend on the host.
    (fnmatch would in fact cope on Windows -- it normcases both sides,
    mapping / to a backslash -- but relying on that side effect is worse
    than saying which form is being matched.)"""
    rel = rel_path.as_posix()
    parent = rel_path.parent.as_posix()
    for g in crate_globs:
        if fnmatch.fnmatch(rel, g) or fnmatch.fnmatch(parent, g):
            return rel_path.parent.name
    return None


# -- stage runner ------------------------------------------------------------

def run(ctx) -> None:
    from .. import probe
    from ..journal import Record
    from . import census

    if ctx.from_review:
        _apply_review(ctx)
        return

    all_rows = [r for r in census.load(ctx) if not r["issue"]]
    numbered: dict[str, bool] = {}
    for folder in {str(PurePosixPath(r["path"]).parent) for r in all_rows}:
        stems = [PurePosixPath(r["path"]).stem for r in all_rows
                 if str(PurePosixPath(r["path"]).parent) == folder]
        numbered[folder] = folder_is_track_numbered(stems)

    rows = [r for r in all_rows if not r["artist"] or not r["title"]]
    threshold = ctx.cfg.filename_confidence
    proposed = queued = 0
    for row in rows:
        if ctx.limit and proposed >= ctx.limit:
            break
        path = ctx.root / row["path"]
        if not path.exists() or not ctx.within_scope(path):
            continue
        p = parse_stem(path.stem, numbered_folder=numbered.get(
            str(PurePosixPath(row["path"]).parent)))
        values = {}
        if not row["artist"] and p.artist:
            values["artist"] = p.artist
        if not row["title"] and p.title:
            values["title"] = p.title
        # Some taggers (Traktor, seen on 23 files in the first real collection)
        # dump the whole filename into title and leave artist empty. The title
        # is non-empty, so never-overwrite-non-empty skips it and the file ends
        # up half-fixed: artist correct, title still "Artist - Title". Not a
        # silent overwrite and not a silent skip; it is exactly what the review
        # queue is for.
        polluted_title = bool(
            row["title"] and p.artist and p.title
            and row["title"].strip().lower().startswith(
                p.artist.strip().lower() + " -"))
        if not row["label"] and p.label:
            values["label"] = p.label
        grouping = crate_grouping(Path(row["path"]), ctx.cfg.crate_globs)
        if grouping and not row["grouping"]:
            values["grouping"] = grouping
        if not values:
            continue
        evidence = {"stem": path.stem, "confidence": p.confidence,
                    "stripped": p.stripped}
        low = p.confidence < threshold and ("artist" in values
                                            or "title" in values)
        if low or polluted_title:
            ctx.review.add({"path": row["path"], "stage": "filename",
                            "proposed_artist": p.artist or "",
                            "proposed_title": p.title or "",
                            "proposed_label": p.label or "",
                            "confidence": p.confidence,
                            "reason": "below threshold" if low
                                      else f"title carries artist prefix: "
                                           f"{row['title']!r}"})
            ctx.journal.append(Record(stage="filename", path=row["path"],
                                      action="skip", evidence={
                                          **evidence,
                                          "reason": "queued for review"}))
            queued += 1
            continue
        if not ctx.apply:
            for f, v in values.items():
                ctx.journal.append(Record(stage="filename", path=row["path"],
                                          action="propose", field=f,
                                          old=row[f] or None, new=v,
                                          evidence=evidence))
        else:
            _ok, changed = guarded_write(ctx, "filename", row["path"],
                                        probe.write, path, values,
                                        overwrite=ctx.overwrite)
            for f, old, new in (changed or []):
                ctx.journal.record_write("filename", ctx.root, path, f,
                                         old, new, evidence=evidence)
        proposed += 1
    ctx.say(f"filename: {proposed} "
            f"({'applied' if ctx.apply else 'proposed'}), {queued} to review "
            f"queue -> {ctx.review.path}")


def _apply_review(ctx) -> None:
    from .. import probe
    from ..journal import ReviewQueue
    accepted = ReviewQueue.load_accepted(ctx.from_review)
    n = 0
    for row in accepted:
        path = ctx.root / row["path"]
        if not path.exists():
            continue
        values = {k: row.get(f"proposed_{k}") for k in ("artist", "title",
                                                        "label")}
        values = {k: v for k, v in values.items() if v}
        if not ctx.apply:
            for f, v in values.items():
                from ..journal import Record
                ctx.journal.append(Record(stage="filename", path=row["path"],
                                          action="propose", field=f, new=v,
                                          evidence={"source": "review"}))
        else:
            # A row only reaches here because a human typed `y` against it.
            # That accept IS the authorization, so it overwrites regardless of
            # --overwrite. Deferring to ctx.overwrite here would make the
            # polluted-title rows silently no-op after the user approved them,
            # which is worse than never queueing them.
            _ok, changed = guarded_write(ctx, "filename", row["path"],
                                        probe.write, path, values,
                                        overwrite=True)
            for f, old, new in (changed or []):
                ctx.journal.record_write("filename", ctx.root, path, f, old,
                                         new, evidence={"source": "review"})
        n += 1
    ctx.say(f"filename: {n} review decisions "
            f"({'applied' if ctx.apply else 'proposed'})")
