"""Stage 0: census. Read-only.

Walks the tree once and records, for every audio file: identity, container,
every managed tag field, art presence and size, sidecar presence, duration,
issues. Every later stage reads this CSV rather than re-walking, and the
first census of a run is kept as `census-baseline.csv` so the report stage
can diff against it.

The file universe is decided here, once: AppleDouble `._*` stubs, hidden
directories, the workdir and configured excludes are filtered at this
chokepoint so no later stage can disagree about what exists. (The original
collection had twelve 4096-byte `._*.wav` resource-fork stubs that made a raw
`find` overcount the WAVs by a third.)
"""

from __future__ import annotations

import csv
import json
import shutil

from .. import probe
from ..util import iter_audio, relpath
from . import Context

# Spliced from probe.FIELDS rather than restated: DictWriter raises on an
# unknown key, so a field added there and forgotten here killed census, and
# census.load() calls run(), so every stage went with it.
COLUMNS = ["path", "container", "size", "mtime", "duration", "bitrate",
           *probe.FIELDS,
           "has_art", "art_min_px", "sidecar", "issue"]


def collect(ctx: Context) -> list[dict]:
    """Always a full, unscoped walk. `census.csv` is the shared ground truth
    every other stage reads via `load()`; applying `ctx.subpath` here would
    silently truncate that shared file to whatever one command happened to be
    scoped to, corrupting state for every stage that runs afterward without
    ever raising an error. `--path` restricts what a *consuming* stage acts
    on — see each stage's own `within_scope` check — never what census
    records exists.
    """
    rows = []
    sidecar_cache: dict = {}
    links: list = []
    for path in iter_audio(ctx.root, ctx.cfg.exclude, ctx.workdir,
                           skipped_links=links):
        st = path.stat()
        row = dict.fromkeys(COLUMNS, "")
        row.update(path=path.relative_to(ctx.root).as_posix(),
                   container=path.suffix.lower().lstrip("."),
                   size=st.st_size, mtime=f"{st.st_mtime:.6f}")
        if st.st_size == 0:
            row["issue"] = "zero-byte"
            rows.append(row)
            continue
        try:
            tags = probe.read(path)
        except probe.ProbeError as e:
            row["issue"] = f"unreadable: {e}"
            rows.append(row)
            continue
        for f in probe.FIELDS:
            row[f] = tags.get(f) or ""
        row["duration"] = f"{tags.duration:.2f}" if tags.duration else ""
        row["bitrate"] = tags.bitrate or ""
        row["has_art"] = "1" if tags.has_art else ""
        row["art_min_px"] = tags.art_min_px or ""
        if tags.art_error:
            row["issue"] = f"unreadable art: {tags.art_error}"
        # Per folder, not per file: a 16-track album used to list its own
        # directory sixteen times, plus any Cover/ subdir. Free on Linux
        # once the dentry cache is warm, and network round trips against a
        # NAS or an SMB share, where this was the dominant census cost.
        parent = path.parent
        if parent not in sidecar_cache:
            sidecar_cache[parent] = probe.find_sidecar_art(parent)
        sidecar = sidecar_cache[parent]
        row["sidecar"] = sidecar.name if sidecar else ""
        rows.append(row)
    # Recorded, not silently dropped: a link farm would otherwise look like
    # a collection where nothing needs doing.
    for link in links:
        row = dict.fromkeys(COLUMNS, "")
        row["path"] = relpath(link, ctx.root)
        row["container"] = link.suffix.lower().lstrip(".")
        row["issue"] = ("symlink: skipped, because writing here would "
                        "replace the link with a regular file")
        rows.append(row)
    return rows


MISSING_FIELDS = ("artist", "title", "album", "art")


def missing_paths(rows: list[dict], art_min_px: int) -> dict[str, set[str]]:
    """The single definition of "missing" over census rows: which paths
    lack each field.

    It was stated three times -- census's own summary, the report's
    progress table, and the report's unresolved list -- with the same
    `not r[f]` plus row_needs_art() rules copied into each. That is the
    drift art_local.row_needs_art()'s docstring already warns about, one
    level up: change what "missing art" means in one place and the census
    summary silently stops agreeing with the report built from it.

    Rows carrying an `issue` are excluded everywhere: a zero-byte file is
    not missing a genre, it is unreadable, and it belongs on the reacquire
    list rather than in a fix rate.
    """
    from .art_local import row_needs_art
    missing: dict[str, set[str]] = {f: set() for f in MISSING_FIELDS}
    for r in rows:
        if r["issue"]:
            continue
        for f in ("artist", "title", "album"):
            if not r.get(f):
                missing[f].add(r["path"])
        if row_needs_art(r, art_min_px):
            missing["art"].add(r["path"])
    return missing


def run(ctx: Context) -> None:
    if ctx.subpath is not None:
        ctx.say(f"census: --path is ignored here; census always scans the "
                f"full collection root ({ctx.root}). --path restricts what "
                f"other stages act on, not what census records.")
    rows = collect(ctx)
    ctx.workdir.mkdir(parents=True, exist_ok=True)
    out = ctx.workdir / "census.csv"
    with open(out, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS)
        w.writeheader()
        w.writerows(rows)
    baseline = ctx.workdir / "census-baseline.csv"
    if not baseline.exists():
        shutil.copy2(out, baseline)

    n = len(rows)
    issues = [r for r in rows if r["issue"]]
    missing = missing_paths(rows, ctx.cfg.art_min_px)
    with_sidecar = sum(1 for r in rows
                       if r["path"] in missing["art"] and r["sidecar"])
    ctx.say(f"census: {n} files -> {out}")
    ctx.say(f"  missing art     {len(missing['art'])}"
            f"  (of which sidecar present: {with_sidecar})")
    ctx.say(f"  missing artist  {len(missing['artist'])}")
    ctx.say(f"  missing title   {len(missing['title'])}")
    ctx.say(f"  missing album   {len(missing['album'])}")
    ctx.say(f"  issues          {len(issues)}")


def load(ctx: Context) -> list[dict]:
    """Later stages read the census instead of re-walking. Runs stage 0
    implicitly when it has not been run yet, and folds in anything written
    since that scan."""
    out = ctx.workdir / "census.csv"
    if not out.exists():
        run(ctx)
    with open(out, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    return _fold_in_later_writes(ctx, rows, out.stat().st_mtime)


def _fold_in_later_writes(ctx: Context, rows: list[dict],
                          census_mtime: float) -> list[dict]:
    """A census is a snapshot, and writing tags makes it stale immediately.

    Without this, a second `tagfill mb --recheck` sees the pre-write census,
    concludes every folder still has no date/genre/art, and re-queries
    MusicBrainz for the lot -- the resume guard normally absorbs that, but
    --recheck is exactly the flag that turns the guard off, and --recheck is
    what you are told to use after enabling a new field in extra_tags.
    Replaying the journal is a file read; re-walking the tree is not.
    """
    later = _applied_since(ctx, census_mtime)
    if not later:
        return rows
    for row in rows:
        for field, value in later.get(row["path"], {}).items():
            if field == "art":
                row["has_art"] = "1"
                row["art_min_px"] = value.removesuffix("px")
            elif field in row:
                row[field] = value
    return rows


def _applied_since(ctx: Context, census_mtime: float) -> dict[str, dict]:
    """{relative path: {field: value}} for writes newer than the census.

    Keyed on the record's post-write file mtime rather than its timestamp:
    a file edited by something other than tagfill after the census was
    taken must keep the census's reading, not an older journal entry's.
    """
    out: dict[str, dict] = {}
    if not ctx.journal.path.exists():
        return out
    with open(ctx.journal.path, encoding="utf-8") as f:
        for line in f:
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            if (d.get("action") == "apply" and d.get("field")
                    and (d.get("mtime") or 0) > census_mtime):
                out.setdefault(d["path"], {})[d["field"]] = str(
                    d.get("new") or "")
    return out
