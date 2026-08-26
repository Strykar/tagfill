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
import sys
from pathlib import Path, PureWindowsPath

from . import probe
from .util import relpath


class TagBackup:
    def __init__(self, workdir: Path):
        d = workdir / "backup"
        d.mkdir(parents=True, exist_ok=True)
        self.path = d / "tags.jsonl"
        self._seen: set[str] = set()
        if self.path.exists():
            with open(self.path, encoding="utf-8") as f:
                for line in f:
                    try:
                        self._seen.add(json.loads(line)["path"])
                    except (json.JSONDecodeError, KeyError):
                        continue

    def snapshot(self, root: Path, path: Path) -> None:
        rel = relpath(path, root)
        if rel in self._seen:
            return
        try:
            tags = probe.read(path)
        except probe.ProbeError:
            return
        art, unreadable = None, False
        if tags.has_art:
            try:
                art = probe.read_art(path)
            except probe.ProbeError:
                # Backing up a file whose art will not parse is still worth
                # doing for its tags; recording art=None instead would have
                # restore strip whatever is actually in there.
                unreadable = True
        rec = {"path": rel,
               "fields": {f: tags.get(f) for f in probe.FIELDS},
               "art": base64.b64encode(art[0]).decode() if art else None,
               "art_mime": art[1] if art else None}
        if unreadable:
            rec["art_unreadable"] = True
        with open(self.path, "a", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        self._seen.add(rel)


def restore(root: Path, backup_file: Path, only: str | None = None) -> int:
    # Stored paths are POSIX by convention (see util.relpath); a Windows
    # user types backslashes and would match nothing. NTFS also hands back
    # one canonical casing while the user may well type another.
    #
    # Both translations are gated on the platform on purpose: a backslash
    # is a legal character in a POSIX filename, and case matters there, so
    # normalizing unconditionally would break matches that work today.
    windows = sys.platform == "win32"
    if only and windows:
        only = PureWindowsPath(only).as_posix().casefold()
    # The workdir is the user's own, so this is self-tampering rather than
    # an attack -- but "nothing outside root is ever touched" should be
    # true by construction, not by provenance.
    root_resolved = root.resolve()
    n = 0
    with open(backup_file, encoding="utf-8") as f:
        for line in f:
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            stored = rec["path"].casefold() if windows else rec["path"]
            if only and stored != only:
                continue
            path = root / rec["path"]
            try:
                path.resolve().relative_to(root_resolved)
            except ValueError:
                continue
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
            elif not rec.get("art_unreadable"):
                probe.remove_art(path)
            n += 1
    return n
