"""Stage 8: report.

Re-runs the census and diffs it against the baseline captured on the first
run, then emits:

- per-stage counts of proposed / applied / rejected / skipped from the journal,
- `report/unresolved.csv`: every file still missing artist, title, album or
  art, with which stages tried it and why each declined,
- `report/reacquire.csv`: zero-byte and unreadable files — nothing to repair,
  a shopping list,
- a note of every AppleDouble stub seen (reported, never touched).

Also invokable with no other command via `tagfill --music-dir DIR
--report`, which needs no `tagfill.toml` — config.load() falls back to
Config()'s defaults, so a bare directory is enough to get a report.

The CSVs are the machine-readable form; `format_text()` below renders the
same data as a plain-text report for a terminal or a redirected file.
"""

from __future__ import annotations

import csv
import json
from collections import defaultdict
from pathlib import PurePosixPath

from ..util import is_appledouble
from . import Context


def run(ctx: Context) -> None:
    from . import census
    # Checked before census.run(), which creates the baseline if missing --
    # otherwise a first run compares against itself and reports 0% for
    # everything, which reads as failure rather than "nothing to compare".
    baseline_path = ctx.workdir / "census-baseline.csv"
    baseline_existed = baseline_path.exists()
    census.run(ctx)  # fresh post-run census
    rows = census.load(ctx)
    report_dir = ctx.workdir / "report"
    report_dir.mkdir(parents=True, exist_ok=True)

    progress = []
    if baseline_existed:
        with open(baseline_path, newline="") as f:
            baseline_rows = list(csv.DictReader(f))
        was = census.missing_paths(baseline_rows, ctx.cfg.art_min_px)
        now = census.missing_paths(rows, ctx.cfg.art_min_px)
        # Only files present in both censuses can be said to have improved
        # or not. Anything added since is new work, not a regression.
        tracked = ({r["path"] for r in baseline_rows}
                   & {r["path"] for r in rows})
        for field in census.MISSING_FIELDS:
            b = was[field] & tracked
            still = b & now[field]
            fixed = len(b) - len(still)
            pct = round(100 * fixed / len(b)) if b else 0
            progress.append({"field": field, "before": len(b),
                            "after": len(still), "fixed": fixed, "pct": pct})

    # Why-declined index from the journal.
    declined: dict[str, list[str]] = defaultdict(list)
    jpath = ctx.workdir / "journal.jsonl"
    stage_counts: dict[str, dict[str, int]] = defaultdict(
        lambda: defaultdict(int))
    if jpath.exists():
        with open(jpath) as f:
            for line in f:
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                stage_counts[d.get("stage", "?")][d.get("action", "?")] += 1
                if d.get("action") in ("reject", "skip"):
                    reason = (d.get("evidence") or {}).get("reason", "")
                    declined[d.get("path", "")].append(
                        f"{d.get('stage')}: {reason}" if reason
                        else str(d.get("stage")))

    unresolved = []
    reacquire = []
    current = census.missing_paths(rows, ctx.cfg.art_min_px)
    for r in rows:
        if r["issue"]:
            reacquire.append({"path": r["path"], "issue": r["issue"]})
            continue
        missing = [f for f in ("artist", "title", "album")
                   if r["path"] in current[f]]
        if r["path"] in current["art"]:
            # "art" alone reads as "no art at all" to a human, but
            # row_needs_art() also flags art that's present and merely
            # undersized (< art_min_px) — say which one this is. This is a
            # display label, not a selection gate — the row is already
            # selected by row_needs_art() above.
            has_art_already = bool(r["has_art"])
            missing.append("art(undersized)" if has_art_already else "art")
        if missing:
            unresolved.append({
                "path": r["path"], "missing": "+".join(missing),
                # mb and itunes reject a whole folder at once and journal
                # it under the folder path, so a per-file lookup alone
                # misses exactly the album-level decisions this column
                # exists to explain.
                "tried": " | ".join(dict.fromkeys(
                    declined.get(r["path"], [])
                    + declined.get(str(PurePosixPath(r["path"]).parent), [])
                )) or "(none)",
            })

    with open(report_dir / "unresolved.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["path", "missing", "tried"])
        w.writeheader()
        w.writerows(unresolved)
    with open(report_dir / "reacquire.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["path", "issue"])
        w.writeheader()
        w.writerows(reacquire)

    stubs = [p for p in ctx.root.rglob("._*") if is_appledouble(p)]
    rq = ctx.workdir / "report" / "review-queue.csv"

    ctx.say(format_text(ctx.root, len(rows), stage_counts, unresolved,
                        reacquire, stubs, rq if rq.exists() else None,
                        progress))


def _render_table(headers: list[str], rows: list[list[str]]) -> list[str]:
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))
    def fmt(cells):
        cols = zip(cells, widths, strict=False)
        return "  ".join(c.ljust(w) for c, w in cols).rstrip()
    lines = [fmt(headers), fmt(["-" * w for w in widths])]
    lines.extend(fmt(row) for row in rows)
    return lines


def format_text(root, total_files: int,
                stage_counts: dict[str, dict[str, int]],
                unresolved: list[dict], reacquire: list[dict],
                stubs: list, review_queue_path,
                progress: list[dict] | None = None) -> str:
    """Render the report as plain text: a summary a person can read in a
    terminal or a redirected file, not just the CSVs a spreadsheet reads."""
    out = []
    out.append(f"tagfill report -- {root}")
    out.append("=" * 80)
    out.append(f"{total_files} files tracked")
    out.append("")

    if progress:
        out.append("Fixed since the first run")
        out.append("-" * 25)
        table = _render_table(
            ["field", "missing then", "missing now", "fixed", "fix rate"],
            [[p["field"], str(p["before"]), str(p["after"]),
             str(p["fixed"]), f"{p['pct']}%"] for p in progress])
        out.extend("  " + line for line in table)
        out.append("")

    out.append("Stage activity")
    out.append("-" * 14)
    if stage_counts:
        for stage in sorted(stage_counts):
            parts = ", ".join(f"{a}={n}" for a, n
                              in sorted(stage_counts[stage].items()))
            out.append(f"  {stage:<12} {parts}")
    else:
        out.append("  (no stages have run yet)")
    out.append("")

    out.append(f"Unresolved: {len(unresolved)} file(s) "
               "-> report/unresolved.csv")
    if unresolved:
        table = _render_table(["path", "missing", "tried"],
                              [[r["path"], r["missing"], r["tried"]]
                               for r in unresolved])
        out.extend("  " + line for line in table)
    out.append("")

    out.append(f"Reacquire (zero-byte/unreadable): {len(reacquire)} file(s) "
               "-> report/reacquire.csv")
    if reacquire:
        table = _render_table(["path", "issue"],
                              [[r["path"], r["issue"]] for r in reacquire])
        out.extend("  " + line for line in table)
    out.append("")

    if stubs:
        out.append(f"AppleDouble stubs: {len(stubs)} present "
                   "(reported, not touched)")
    if review_queue_path is not None:
        out.append(f"Review queue: edit `accept` in {review_queue_path} "
                   "and feed back with `tagfill filename --from-review`")
    return "\n".join(out)
