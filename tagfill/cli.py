"""tagfill CLI.

    tagfill init                        write an example config
    tagfill --music-dir DIR --report    formatted report, no config needed
    tagfill census                      stage 0 (read-only)
    tagfill run                         the pipeline in order, dry run
    tagfill run --convert-wav --apply   the same, writing (+ WAV->FLAC)
    tagfill <stage> [--apply]           one stage
    tagfill filename --from-review report/review-queue.csv --apply
    tagfill restore                     undo from backup/tags.jsonl

Dry run is the default everywhere. `--apply` is the only thing that writes.
`--music-dir`, `--config` and `--workdir` are top-level flags (they precede
the command); `--report` is also top-level, so it works with no command at
all. `run` skips stage 1 (WAV->FLAC conversion) unless `--convert-wav` is
passed -- it's the one stage that replaces a file, so it isn't on by default.
"""

from __future__ import annotations

import argparse
import contextlib
import sys
from pathlib import Path

from . import __version__, config, pipeline
from .journal import Journal, ReviewQueue
from .lock import WorkdirBusy, WorkdirLock
from .stages import STAGES, Context, StagePrecondition


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="tagfill",
        description="Fix metadata and album art in place, without moving "
                    "your files.")
    p.add_argument("--version", action="version", version=__version__)
    p.add_argument("--config", type=Path, help="path to tagfill.toml")
    p.add_argument("--music-dir", type=Path, help="override collection root")
    p.add_argument("--workdir", type=Path, help="override workdir")
    p.add_argument("--report", action="store_true",
                   help="print a formatted report and exit; no command needed")

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--apply", action="store_true",
                        help="write changes (default: dry run)")
    common.add_argument("--overwrite", action="store_true",
                        help="allow overwriting non-empty fields")
    common.add_argument("--path", type=Path, default=None,
                        help="restrict to a subtree (relative to root)")
    common.add_argument("--limit", type=int, default=None,
                        help="handle at most N targets")
    common.add_argument("--backup-tags", action="store_true",
                        help="snapshot original tags before first write")
    common.add_argument("--recheck", action="store_true",
                        help="re-examine files already handled (use after "
                             "enabling a new field in extra_tags)")

    sub = p.add_subparsers(dest="command")
    sub.add_parser("init", help="write tagfill.toml example")

    for _, name, _, _ in STAGES:
        if name == "report":
            continue  # invoked via --report, not as a subcommand
        sp = sub.add_parser(name, parents=[common])
        if name == "filename":
            sp.add_argument("--from-review", type=Path,
                            help="apply accepted rows from a review-queue CSV")

    runp = sub.add_parser("run", parents=[common],
                          help="the full pipeline in order (stage 9 stays explicit)")
    runp.add_argument("--offline", action="store_true",
                      help="skip the network stages")
    runp.add_argument("--convert-wav", action="store_true",
                      help="also run stage 1, WAV to FLAC conversion")

    rp = sub.add_parser("restore", help="undo from backup/tags.jsonl")
    rp.add_argument("--only", help="restore a single relative path")
    return p


def _make_output_encoding_safe() -> None:
    """Never let an unprintable filename kill a run.

    Track names are not ASCII. On Windows, redirecting output (`tagfill
    --report > report.txt`) encodes with the legacy ANSI code page rather
    than UTF-8, and cp1252 has no mapping for Japanese, Cyrillic, Devanagari
    or emoji -- printing one raises UnicodeEncodeError and takes down a run
    that had otherwise worked. Ask for UTF-8, and fall back to replacing the
    odd character rather than aborting over cosmetics.
    """
    for stream in (sys.stdout, sys.stderr):
        # Not every stream is reconfigurable (piped, captured, replaced).
        with contextlib.suppress(AttributeError, ValueError, OSError):
            stream.reconfigure(encoding="utf-8", errors="replace")


def main(argv: list[str] | None = None) -> int:
    _make_output_encoding_safe()
    args = _build_parser().parse_args(argv)

    if args.command == "init":
        out = Path("tagfill.toml")
        if out.exists():
            print(f"{out} already exists; not overwriting")
            return 1
        out.write_text(config.EXAMPLE, encoding="utf-8")
        print(f"wrote {out}; edit [collection].root and go")
        return 0

    if not args.report and args.command is None:
        _build_parser().error("a command or --report is required")

    cfg = config.load(args.config)
    if args.music_dir:
        cfg.root = args.music_dir.resolve()
    if args.workdir:
        cfg.workdir = args.workdir.resolve()
    if cfg.root is None:
        print("no collection root: pass --music-dir, or set "
              "[collection].root in tagfill.toml")
        return 1
    if not cfg.root.is_dir():
        print(f"collection root {cfg.root} is not a directory")
        return 1

    if args.report:
        from .stages import report
        ctx = Context(cfg=cfg, journal=Journal(cfg.workdir),
                      review=ReviewQueue(cfg.workdir))
        report.run(ctx)
        return 0

    if args.command == "restore":
        from .backup import restore
        backup_file = cfg.workdir / "backup" / "tags.jsonl"
        if not backup_file.exists():
            print(f"no backup at {backup_file}")
            return 1
        n = restore(cfg.root, backup_file, getattr(args, "only", None))
        print(f"restored {n} files from {backup_file}")
        return 0

    ctx = Context(cfg=cfg, journal=Journal(cfg.workdir),
                  review=ReviewQueue(cfg.workdir),
                  apply=getattr(args, "apply", False),
                  overwrite=getattr(args, "overwrite", False),
                  limit=getattr(args, "limit", None),
                  subpath=getattr(args, "path", None),
                  backup_tags=getattr(args, "backup_tags", False),
                  recheck=getattr(args, "recheck", False),
                  from_review=getattr(args, "from_review", None))

    if ctx.backup_tags and ctx.apply:
        from .backup import TagBackup
        ctx.backup = TagBackup(ctx.workdir)

    # Anything that writes to the workdir takes the lock. --report and
    # restore above do not reach here.
    try:
        with WorkdirLock(ctx.workdir):
            if args.command == "run":
                pipeline.run(ctx, offline=args.offline,
                             convert_wav=args.convert_wav)
                return 0

            for _num, name, module, _ in STAGES:
                if name == args.command:
                    try:
                        pipeline.stage_module(module).run(ctx)
                    except StagePrecondition as e:
                        # Asked for this stage by name and it cannot start,
                        # so this is a failed command, not a quiet no-op.
                        print(f"{name}: {e}")
                        return 1
                    print("\njournal:")
                    print(ctx.journal.summary())
                    return 0
    except WorkdirBusy as e:
        print(e)
        print("pass --workdir to run against a different one")
        return 1
    return 1


if __name__ == "__main__":
    sys.exit(main())
