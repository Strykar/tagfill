"""Stage registry.

Stages are independent passes ordered cheapest-and-most-certain first, so the
offline stages establish as much as possible before any network stage spends a
request or takes a guess. Each stage module exposes `run(ctx) -> None`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from ..config import Config
from ..journal import Journal, ReviewQueue

# (number, cli name, module name, needs network)
STAGES = [
    (0, "census", "census", False),
    (1, "convert", "convert", False),
    (2, "art-local", "art_local", False),
    (3, "filename", "filename", False),
    (4, "mb", "mb", True),
    (5, "acoustid", "acoustid", True),
    (6, "itunes", "itunes", True),
    (8, "report", "report", False),
    (9, "submit", "submit", True),
]


@dataclass
class Context:
    cfg: Config
    journal: Journal
    review: ReviewQueue
    apply: bool = False
    overwrite: bool = False
    limit: int | None = None
    subpath: Path | None = None       # --path restriction
    backup_tags: bool = False
    recheck: bool = False       # ignore the resume guard
    from_review: Path | None = None
    extras: dict = field(default_factory=dict)

    @property
    def root(self) -> Path:
        return self.cfg.root

    @property
    def workdir(self) -> Path:
        return self.cfg.workdir

    def within_scope(self, path: Path) -> bool:
        if self.subpath is None:
            return True
        try:
            path.relative_to(self.root / self.subpath)
            return True
        except ValueError:
            return False

    def say(self, msg: str) -> None:
        print(msg)


def guarded_write(ctx: Context, stage: str, rel_path: str, op, *args,
                  **kwargs) -> tuple[bool, object]:
    """Run one probe write, journal a failure, and let the batch continue.

    probe.write() and probe.embed_art() validate the container before
    touching anything, but validating is not the same as succeeding: the
    file can be read-only, the disk can fill, and a file can be replaced or
    unmounted between the census and the write. Found live by chmod 444 on
    the second of three files in an album -- the MutagenError propagated
    out of the stage, printed a traceback, and the perfectly writable third
    file never got its art. One unwritable file must cost that file, not
    the rest of the run.

    Returns (ok, result). On failure the reason is journalled as a skip, so
    report.py can say why that file went untouched instead of it looking
    like nothing was ever attempted.
    """
    from ..journal import Record
    try:
        return True, op(*args, **kwargs)
    except Exception as e:
        ctx.journal.append(Record(
            stage=stage, path=rel_path, action="skip",
            evidence={"reason": f"write failed: {type(e).__name__}: {e}"}))
        return False, None
