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
import shutil

from .. import probe
from ..util import iter_audio
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
    for path in iter_audio(ctx.root, ctx.cfg.exclude, ctx.workdir):
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
        if tags.has_art:
            art = probe.read_art(path)
            if art:
                px = probe.image_min_px(art[0])
                row["art_min_px"] = px if px else ""
        sidecar = probe.find_sidecar_art(path.parent)
        row["sidecar"] = sidecar.name if sidecar else ""
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
    with open(out, "w", newline="") as f:
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
    implicitly when it has not been run yet."""
    out = ctx.workdir / "census.csv"
    if not out.exists():
        run(ctx)
    with open(out, newline="") as f:
        return list(csv.DictReader(f))
