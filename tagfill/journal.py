"""Append-only decision journal, review queue, and resume guard.

Every stage records every decision: `propose` on a dry run, `apply` on a
write, `reject` and `skip` with evidence either way. A dry run's journal is a
plan; it can be diffed against the eventual apply.

The resume guard snapshots size, mtime and a 64KB head-hash **after** a write,
never before. Writing tags changes mtime, so a pre-write snapshot would make
every touched file look changed on the next run and the run-twice-zero-changes
acceptance gate would fail for the wrong reason.
"""

from __future__ import annotations

import csv
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

from .util import relpath, sha1_head

ACTIONS = ("propose", "apply", "reject", "skip")


@dataclass
class Record:
    stage: str
    path: str          # relative to collection root
    action: str
    field: str = ""
    old: object = None
    new: object = None
    evidence: dict | None = None
    size: int | None = None
    mtime: float | None = None
    sha1_head: str | None = None
    ts: str = ""


class Journal:
    def __init__(self, workdir: Path):
        workdir.mkdir(parents=True, exist_ok=True)
        self.path = workdir / "journal.jsonl"
        self.counts: dict[tuple[str, str], int] = {}
        self._applied: dict[tuple[str, str], dict] | None = None

    def append(self, rec: Record) -> None:
        assert rec.action in ACTIONS, rec.action
        rec.ts = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        key = (rec.stage, rec.action)
        self.counts[key] = self.counts.get(key, 0) + 1
        with open(self.path, "a") as f:
            f.write(json.dumps(rec.__dict__, ensure_ascii=False) + "\n")

    def record_write(self, stage: str, root: Path, path: Path, field: str,
                     old, new, evidence: dict | None = None,
                     applied: bool = True) -> None:
        """Journal one field change. On apply, snapshot the file *after* the
        write so the resume guard sees the post-write state."""
        rec = Record(stage=stage, path=relpath(path, root),
                     action="apply" if applied else "propose",
                     field=field, old=old, new=new, evidence=evidence)
        if applied and path.exists():
            st = path.stat()
            rec.size, rec.mtime = st.st_size, st.st_mtime
            rec.sha1_head = sha1_head(path)
        self.append(rec)

    # -- resume guard ------------------------------------------------------

    def _load_applied(self) -> dict[tuple[str, str], dict]:
        if self._applied is None:
            self._applied = {}
            if self.path.exists():
                with open(self.path) as f:
                    for line in f:
                        try:
                            d = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if d.get("action") == "apply" and d.get("sha1_head"):
                            self._applied[(d["stage"], d["path"])] = d
        return self._applied

    def already_done(self, stage: str, root: Path, path: Path) -> bool:
        """True when this stage already applied to this file and the file has
        not changed since (size + mtime + head-hash all match)."""
        d = self._load_applied().get((stage, relpath(path, root)))
        if not d or not path.exists():
            return False
        st = path.stat()
        if (st.st_size != d.get("size")
                or abs(st.st_mtime - (d.get("mtime") or 0)) > 1e-6):
            return False
        return sha1_head(path) == d.get("sha1_head")

    def summary(self) -> str:
        lines = []
        for (stage, action), n in sorted(self.counts.items()):
            lines.append(f"  {stage:<18} {action:<8} {n}")
        return "\n".join(lines) if lines else "  (no decisions)"


class ReviewQueue:
    """Low-confidence proposals await a human. The CSV has an `accept` column;
    edit it to y/n and feed the file back with `--from-review`."""

    FIELDS: ClassVar[list[str]] = [
        "path", "stage", "proposed_artist", "proposed_title",
        "proposed_label", "confidence", "reason", "accept"]

    def __init__(self, workdir: Path):
        report = workdir / "report"
        report.mkdir(parents=True, exist_ok=True)
        self.path = report / "review-queue.csv"
        self._seen: set[str] = set()
        if self.path.exists():
            with open(self.path, newline="") as f:
                for existing in csv.DictReader(f):
                    self._seen.add(existing.get("path", ""))

    def add(self, row: dict) -> None:
        # Re-runs must not append duplicates to a CSV a human hand-edits.
        if row.get("path", "") in self._seen:
            return
        new_file = not self.path.exists()
        with open(self.path, "a", newline="") as f:
            w = csv.DictWriter(f, fieldnames=self.FIELDS)
            if new_file:
                w.writeheader()
            w.writerow({k: row.get(k, "") for k in self.FIELDS})
        self._seen.add(row.get("path", ""))

    @staticmethod
    def load_accepted(path: Path) -> list[dict]:
        out = []
        with open(path, newline="") as f:
            for row in csv.DictReader(f):
                if str(row.get("accept", "")).strip().lower() in (
                        "y", "yes", "1", "true"):
                    out.append(row)
        return out
