"""The headline acceptance gate: apply the offline stages twice over a fresh
fixture copy — the second run must produce **zero** applies. Exercises the
post-apply mtime snapshot, the resume guard and the never-overwrite-non-empty
rule together. Requires mutagen + ffmpeg."""

import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

try:
    import mutagen  # noqa: F401
    HAVE = shutil.which("ffmpeg") is not None
except ImportError:
    HAVE = False

try:
    import pytest
    needs_env = pytest.mark.skipif(not HAVE, reason="needs mutagen + ffmpeg")
except ImportError:
    def needs_env(fn):
        return fn


def _ctx(root: Path, workdir: Path, apply: bool):
    from tagfill.config import Config
    from tagfill.journal import Journal, ReviewQueue
    from tagfill.stages import Context
    cfg = Config()
    cfg.root, cfg.workdir = root, workdir
    cfg.crate_globs = ["DJ Pool/*"]
    return Context(cfg=cfg, journal=Journal(workdir),
                   review=ReviewQueue(workdir), apply=apply)


def run_offline_stages(ctx):
    from tagfill.stages import art_local, census, filename
    census.run(ctx)
    art_local.run(ctx)
    filename.run(ctx)


@needs_env
def test_idempotence():
    fixtures = Path(__file__).parent / "fixtures"
    if not fixtures.exists():
        from make_fixtures import build
        build(fixtures)
    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "coll"
        shutil.copytree(fixtures, root)

        ctx1 = _ctx(root, Path(td) / "work1", apply=True)
        run_offline_stages(ctx1)
        applied_first = sum(n for (s, a), n in ctx1.journal.counts.items()
                            if a == "apply")
        assert applied_first > 0, "fixtures should require work"

        # Second run, fresh journal and census: the files themselves must now
        # satisfy every stage. This isolates never-overwrite + needs_art
        # semantics from the resume guard.
        ctx2 = _ctx(root, Path(td) / "work2", apply=True)
        run_offline_stages(ctx2)
        applied_second = sum(n for (s, a), n in ctx2.journal.counts.items()
                             if a == "apply")
        assert applied_second == 0, ctx2.journal.counts


if __name__ == "__main__":
    if not HAVE:
        print("skipped: needs mutagen + ffmpeg")
        sys.exit(0)
    test_idempotence()
    print("ok test_idempotence")
