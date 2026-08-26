"""Tag backup and restore.

`--backup-tags` snapshots, for every file a run is about to touch, every
managed field (see probe.FIELDS) plus the embedded art bytes, into
`backup/tags.jsonl` — once per file, before the first write.

Restore is an implemented inverse, not a JSONL you hand-process:
`tagfill restore` writes each backed-up field back, **deletes** fields
that were empty pre-run, and re-embeds (or removes) art. Reversibility that
has not been exercised is a claim, not a property, so the test suite includes
a backup -> mutate -> restore -> byte-compare round trip.

Scope of the guarantee: tagfill only ever writes probe.FIELDS and
front-cover art, and never touches frames outside that set — so restoring
that set restores the pre-run tag state for anything tagfill could have
changed.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path

from . import probe


class TagBackup:
    def __init__(self, workdir: Path):
        d = workdir / "backup"
        d.mkdir(parents=True, exist_ok=True)
        self.path = d / "tags.jsonl"
        self._seen: set[str] = set()
        if self.path.exists():
            with open(self.path) as f:
                for line in f:
                    try:
                        self._seen.add(json.loads(line)["path"])
                    except (json.JSONDecodeError, KeyError):
                        continue

    def snapshot(self, root: Path, path: Path) -> None:
        rel = str(path.relative_to(root))
        if rel in self._seen:
            return
        try:
            tags = probe.read(path)
        except probe.ProbeError:
            return
        art = probe.read_art(path) if tags.has_art else None
        rec = {"path": rel,
               "fields": {f: tags.get(f) for f in probe.FIELDS},
               "art": base64.b64encode(art[0]).decode() if art else None,
               "art_mime": art[1] if art else None}
        with open(self.path, "a") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        self._seen.add(rel)


def restore(root: Path, backup_file: Path, only: str | None = None) -> int:
    n = 0
    with open(backup_file) as f:
        for line in f:
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if only and rec["path"] != only:
                continue
            path = root / rec["path"]
            if not path.exists():
                continue
            fields = rec.get("fields", {})
            keep = {k: v for k, v in fields.items() if v}
            empty = [k for k, v in fields.items() if not v]
            if keep:
                probe.write(path, keep, overwrite=True)
            if empty:
                probe.delete_fields(path, empty)
            if rec.get("art"):
                probe.embed_art(path, base64.b64decode(rec["art"]),
                                rec.get("art_mime") or "image/jpeg")
            else:
                probe.remove_art(path)
            n += 1
    return n
