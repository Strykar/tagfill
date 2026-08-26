"""Regression test: census.csv must always be a full-collection walk.

Found live: `convert.py`'s post-apply re-census call runs with whatever
`ctx.subpath` the triggering command was scoped to (e.g. `--path "Some
Album"`). Before this fix, `census.collect()` applied that scope to its own
walk and `census.run()` overwrote the single shared `census.csv` with the
truncated result — silently, with no error. Every stage run afterward,
including a completely unscoped one, would then see only that subtree until
someone happened to notice the numbers looked small. This surfaced as a
`mb` dry run over a real 1822-file collection finding exactly 2 folders to
work with, because the last thing that had touched census.csv was a
scoped `convert --path "Pink Floyd - The Division Bell"`.

Needs mutagen + ffmpeg to build fixtures; skips cleanly otherwise.
"""

import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

try:
    import mutagen  # noqa: F401
    HAVE_MUTAGEN = True
except ImportError:
    HAVE_MUTAGEN = False
HAVE_FFMPEG = shutil.which("ffmpeg") is not None

try:
    import pytest
    needs_env = pytest.mark.skipif(
        not (HAVE_MUTAGEN and HAVE_FFMPEG), reason="needs mutagen + ffmpeg")
except ImportError:
    def needs_env(fn):
        return fn

FIXTURES = Path(__file__).parent / "fixtures"


def _ensure_fixtures():
    if not FIXTURES.exists():
        from make_fixtures import build
        build(FIXTURES)


def _ctx(tmp_path, subpath=None):
    from tagfill import config
    from tagfill.journal import Journal, ReviewQueue
    from tagfill.stages import Context

    cfg = config.Config()
    cfg.root = FIXTURES
    cfg.workdir = tmp_path
    return Context(cfg=cfg, journal=Journal(cfg.workdir),
                  review=ReviewQueue(cfg.workdir), apply=False,
                  overwrite=False, limit=None, subpath=subpath,
                  backup_tags=False,
                  from_review=None)


@needs_env
def test_census_ignores_subpath_scope(tmp_path):
    """A scoped census must record exactly as many rows as an unscoped one:
    the file universe, not a --path-restricted subset of it."""
    _ensure_fixtures()
    from tagfill.stages import census

    full = census.collect(_ctx(tmp_path))
    scoped = census.collect(_ctx(tmp_path, subpath=Path("Tagged Album")))
    assert len(scoped) == len(full)
    paths = {r["path"] for r in scoped}
    assert any(not p.startswith("Tagged Album") for p in paths), (
        "a scoped census must still include files outside the scope")


@needs_env
def test_scoped_run_does_not_truncate_shared_census_csv(tmp_path):
    """The exact failure mode: run a full census, then run a --path-scoped
    one (as convert.py does internally after an --apply). census.csv on disk
    must still cover the whole collection afterward."""
    from tagfill.stages import census

    ctx_full = _ctx(tmp_path)
    census.run(ctx_full)
    full_rows = list(census.load(ctx_full))

    ctx_scoped = _ctx(tmp_path, subpath=Path("Tagged Album"))
    census.run(ctx_scoped)
    rows_after_scoped_run = list(census.load(ctx_scoped))

    assert len(rows_after_scoped_run) == len(full_rows), (
        "census.csv was truncated by a scoped run; every other stage reads "
        "this same file as ground truth")
