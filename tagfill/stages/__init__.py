"""Stage registry.

Stages are independent passes ordered cheapest-and-most-certain first, so the
offline stages establish as much as possible before any network stage spends a
request or takes a guess. Each stage module exposes `run(ctx) -> None`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from ..backup import TagBackup
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


class StagePrecondition(RuntimeError):
    """A stage cannot start: no API key, a missing helper binary, an
    uninstalled dependency.

    Distinct from "ran and found nothing to do", which is a normal outcome
    and stays a journal entry. These used to be a ctx.say() and a bare
    return, so an embedder got prose on stdout and no way to tell a
    misconfiguration from a clean no-op. Per-file resilience is unchanged:
    a single unwritable file is still a journalled skip, not this.
    """


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
    # Set when --backup-tags is on; guarded_write snapshots through it.
    # This used to be a monkeypatch of probe.write and probe.embed_art,
    # which is process-wide, closed over one workdir, and never uninstalled
    # -- fine for a CLI that runs one command and exits, wrong for anything
    # long-lived. A second collection's writes went on flowing through the
    # first's wrapper and into the first's backup file.
    backup: TagBackup | None = None
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
    # Every write in every stage comes through here (there is a test that
    # says so), which makes this the one place a snapshot has to happen.
    if ctx.backup is not None and args:
        ctx.backup.snapshot(ctx.root, Path(args[0]))
    try:
        return True, op(*args, **kwargs)
    except Exception as e:
        ctx.journal.append(Record(
            stage=stage, path=rel_path, action="skip",
            evidence={"reason": f"write failed: {type(e).__name__}: {e}"}))
        return False, None
