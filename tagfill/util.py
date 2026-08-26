"""Pure helpers. No third-party imports, so everything here is testable anywhere."""

from __future__ import annotations

import fnmatch
import hashlib
import os.path
import re
import time
from difflib import SequenceMatcher
from pathlib import Path

# Containers tagfill knows. probe.py is the only module that knows what
# to *do* with each; this set only decides what counts as an audio file.
# How to capture a helper's text output. Without an explicit encoding the
# platform default decodes it, which on Windows is the ANSI code page --
# ffmpeg and flac emit UTF-8, and their diagnostics quote the file path, so
# one Cyrillic album name turns a readable error into mojibake or a
# UnicodeDecodeError. errors="replace" because a message about a failure
# must never itself raise.
CAPTURE_TEXT = {"capture_output": True, "text": True,
                "encoding": "utf-8", "errors": "replace"}

AUDIO_SUFFIXES = {
    ".mp3", ".flac", ".ogg", ".opus", ".m4a", ".mp4",
    ".aiff", ".aif", ".aifc", ".wav",
    ".dsf", ".dff",                      # DSD
}


def relpath(path: Path, root: Path) -> str:
    """A path relative to root, as the one string form everything stores.

    census.csv, journal.jsonl and backup/tags.jsonl are cross-referenced by
    this string -- the report looks a census path up in an index keyed on
    journal paths. str() gives backslashes on Windows, so mixing the two
    conventions made that lookup miss every time and the report's "why was
    this declined" column silently read (none) for every file.
    """
    return path.relative_to(root).as_posix()


def is_appledouble(path: Path) -> bool:
    """macOS AppleDouble resource-fork stubs (`._*`). They are 4096-byte
    metadata files that a raw `find` happily counts as audio."""
    return path.name.startswith("._")


def is_link(p: Path) -> bool:
    """A symlink, or a Windows junction, which is a reparse point that
    is_symlink() does not report. os.path.isjunction arrived in 3.12 and
    the floor here is 3.11."""
    if p.is_symlink():
        return True
    isjunction = getattr(os.path, "isjunction", None)
    return bool(isjunction and isjunction(p))


def iter_audio(root: Path, excludes: list[str] | None = None,
               workdir: Path | None = None,
               skipped_links: list[Path] | None = None):
    """Yield audio files under root, skipping AppleDouble stubs, anything
    hidden, the workdir (if it lives inside root) and any exclude globs
    (matched against the path relative to root).

    Hidden covers files as well as directories, which also sweeps up any
    `.name.tagfill-tmp.ext` left behind if the process was killed
    mid-write: it must never be mistaken for music and tagged in a later run.

    Globs are matched against the POSIX form of the relative path. On Windows
    `str(rel)` yields backslashes, so a perfectly ordinary `["DJ Pool/*"]` in
    the config would silently match nothing at all.

    Symlinks are skipped and collected into `skipped_links` if given.
    `is_file()` follows them, so a link inside the collection pointing
    outside it used to be censused and written -- and the write does not go
    *through* the link, it replaces it: copy2 plus os.replace leaves a
    regular file where the link was, the real target untouched and the
    content silently forked. Confirmed on a link into a directory outside
    root. Link-farm layouts are common enough in this audience (dedup
    setups, beets-style views) that this has to be a rule, not a
    footnote."""
    excludes = excludes or []
    workdir = workdir.resolve() if workdir else None
    for p in sorted(root.rglob("*")):
        if p.suffix.lower() not in AUDIO_SUFFIXES:
            continue
        if is_link(p):
            if skipped_links is not None:
                skipped_links.append(p)
            continue
        if not p.is_file():
            continue
        if is_appledouble(p):
            continue
        rel = p.relative_to(root)
        if any(part.startswith(".") for part in rel.parts):
            continue
        if workdir and workdir in p.resolve().parents:
            continue
        if any(fnmatch.fnmatch(rel.as_posix(), g) for g in excludes):
            continue
        yield p


_MB_PLACEHOLDER = re.compile(r"^\[.+\]$")


def is_mb_placeholder(value: str | None) -> bool:
    """MusicBrainz's special-purpose artists/recordings use a bracketed
    convention for "we don't actually know this": [unknown], [traditional],
    [data], [dialogue], [no artist], [silence], [anonymous], and similar.
    Found live: an AcoustID match returned a real recording whose title field
    is literally the string "[unknown]" — a faithful passthrough of that
    convention would write that literal string into a file's title tag,
    which reads as corrupted metadata to anyone who sees it and is strictly
    worse than leaving the field empty (which honestly says "not known").

    A real title or artist name is never *only* a bracketed phrase with
    nothing else — "Song Title [Extended Mix]" has brackets as a suffix, not
    as the entire value — so matching the whole trimmed string is safe and
    doesn't reject legitimate bracketed titles.
    """
    return bool(value and _MB_PLACEHOLDER.match(value.strip()))


def norm(s: str) -> str:
    s = re.sub(r"[^\w\s]", " ", s.lower())
    return re.sub(r"\s+", " ", s).strip()


def similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, norm(a), norm(b)).ratio()


def sha1_head(path: Path, n: int = 65536) -> str:
    """Hash of the first 64KB: cheap change detection for the resume guard."""
    h = hashlib.sha1()
    with open(path, "rb") as f:
        h.update(f.read(n))
    return h.hexdigest()


class RateLimiter:
    """Blocking limiter: at most one call per `min_interval` seconds."""

    def __init__(self, min_interval: float):
        self.min_interval = min_interval
        self._last = 0.0

    def wait(self) -> None:
        delta = time.monotonic() - self._last
        if delta < self.min_interval:
            time.sleep(self.min_interval - delta)
        self._last = time.monotonic()
