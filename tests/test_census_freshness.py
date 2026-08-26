"""A census is a snapshot, and writing tags makes it stale immediately.

External review: on a second `tagfill mb --recheck`, the stale census still
says every folder has no date/genre/art, so every folder is re-queried. The
resume guard normally absorbs that, but --recheck is the flag that turns the
guard off, and --recheck is exactly what you are told to run after enabling
a new field in extra_tags. census.load() folds the journal's later writes
back over the rows; no re-walk, one file read.

No audio fixtures here -- census.csv is written directly, so this runs
everywhere.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tagfill import config
from tagfill.journal import Journal, Record, ReviewQueue
from tagfill.stages import Context, census

HEADER = ("path,container,size,mtime,duration,artist,album,genre,"
          "has_art,art_min_px,issue\n")
ROW = "Album/01.mp3,mp3,1,0,200,A,B,,,,\n"


def _ctx_with_census(tmp_path):
    cfg = config.Config()
    cfg.root = tmp_path / "Music"
    cfg.workdir = tmp_path / "work"
    (cfg.root / "Album").mkdir(parents=True)
    cfg.workdir.mkdir(parents=True)
    (cfg.workdir / "census.csv").write_text(HEADER + ROW, encoding="utf-8")
    ctx = Context(cfg=cfg, journal=Journal(cfg.workdir),
                  review=ReviewQueue(cfg.workdir))
    return ctx, (cfg.workdir / "census.csv").stat().st_mtime


def _applied(ctx, field, new, mtime):
    ctx.journal.append(Record(stage="mb", path="Album/01.mp3", action="apply",
                              field=field, new=new, mtime=mtime, size=1,
                              sha1_head="x"))


def test_the_bare_census_is_what_it_says(tmp_path):
    ctx, _ = _ctx_with_census(tmp_path)
    row = census.load(ctx)[0]
    assert row["genre"] == ""
    assert row["has_art"] == ""


def test_writes_since_the_census_are_folded_in(tmp_path):
    ctx, taken = _ctx_with_census(tmp_path)
    _applied(ctx, "genre", "Techno", taken + 10)
    _applied(ctx, "art", "1000px", taken + 10)
    row = census.load(ctx)[0]
    assert row["genre"] == "Techno"
    assert row["has_art"] == "1"
    assert row["art_min_px"] == "1000"


def test_a_write_older_than_the_census_does_not_override_it(tmp_path):
    """The census re-read the file after that write. If something else has
    since cleared the tag, the census is the current reading and the journal
    entry is history."""
    ctx, taken = _ctx_with_census(tmp_path)
    _applied(ctx, "genre", "Techno", taken - 10)
    assert census.load(ctx)[0]["genre"] == ""


def test_a_corrupt_journal_line_does_not_take_the_census_down(tmp_path):
    ctx, taken = _ctx_with_census(tmp_path)
    with open(ctx.journal.path, "a", encoding="utf-8") as f:
        f.write("{not json\n")
    _applied(ctx, "genre", "Techno", taken + 10)
    assert census.load(ctx)[0]["genre"] == "Techno"
