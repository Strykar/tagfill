"""Tests for stages/report.py's plain-text formatter and the "tried" column
dedup fix.

Found live: --report against the real 1822-file collection produced
"tried" cells hundreds of characters wide, because journal.jsonl is
append-only across every session ever run and the same stage/reason pair
gets logged again each time a file is revisited without changing. A table
column sized to its widest cell made the whole report unreadable. Fixed by
deduplicating declined reasons per path while preserving order.
"""

import csv
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from tagfill import config
from tagfill.journal import Journal, ReviewQueue
from tagfill.stages import Context, census, report


def _ctx(tmp_path, root):
    # workdir must be a sibling of root, not nested inside it -- iter_audio()
    # skips anything that lives under the workdir.
    cfg = config.Config()
    cfg.root = root
    cfg.workdir = tmp_path / "work"
    return Context(cfg=cfg, journal=Journal(cfg.workdir),
                  review=ReviewQueue(cfg.workdir))


def test_format_text_renders_unresolved_table():
    text = report.format_text(
        Path("/music"), total_files=3,
        stage_counts={"mb": {"apply": 1, "reject": 2}},
        unresolved=[{"path": "a.mp3", "missing": "art",
                     "tried": "mb: no candidate passed the duration-vector gate"}],
        reacquire=[], stubs=[], review_queue_path=None)
    assert f"tagfill report for {Path('/music')}" in text
    assert "3 files tracked" in text
    assert "mb           apply=1, reject=2" in text
    assert "Unresolved: 1 file(s)" in text
    assert "a.mp3" in text
    assert "no candidate passed the duration-vector gate" in text


def test_format_text_handles_empty_report():
    text = report.format_text(Path("/music"), total_files=0,
                              stage_counts={}, unresolved=[], reacquire=[],
                              stubs=[], review_queue_path=None)
    assert "(no stages have run yet)" in text
    assert "Unresolved: 0 file(s)" in text


def test_format_text_renders_fix_rate_progress():
    text = report.format_text(
        Path("/music"), total_files=100, stage_counts={}, unresolved=[],
        reacquire=[], stubs=[], review_queue_path=None,
        progress=[{"field": "art", "before": 200, "after": 40,
                  "fixed": 160, "pct": 80}])
    assert "Fixed since the first run" in text
    assert "art" in text and "200" in text and "40" in text and "80%" in text


def test_format_text_omits_progress_section_when_no_baseline():
    text = report.format_text(Path("/music"), total_files=5,
                              stage_counts={}, unresolved=[], reacquire=[],
                              stubs=[], review_queue_path=None, progress=[])
    assert "Fixed since the first run" not in text


def test_census_columns_cover_every_managed_field():
    """COLUMNS is derived from probe.FIELDS rather than restated, because
    collect() writes every FIELDS entry into a row and DictWriter's default
    extrasaction is "raise" -- a field added to FIELDS but forgotten here
    made census.run() blow up on the first row, and census.load() calls
    run(), so every stage went down with it."""
    from tagfill import probe
    assert set(probe.FIELDS) <= set(census.COLUMNS)
    assert [c for c in census.COLUMNS if c in probe.FIELDS] == list(probe.FIELDS)


def test_one_definition_of_missing_shared_by_census_and_report():
    """census's summary, the progress table and the unresolved list all
    used to restate the same `not r[f]` + row_needs_art() rules. They now
    read from census.missing_paths(), so the counts cannot disagree."""
    import inspect

    from tagfill.stages import report as rep
    src = inspect.getsource(rep)
    assert "census.missing_paths(" in src
    assert "def _missing_paths" not in src, "report must not keep its own copy"


def test_missing_paths_excludes_issue_rows_and_counts_undersized_art():
    rows = [
        {"path": "a", "issue": "zero-byte", "artist": "", "title": "",
         "album": "", "has_art": "", "art_min_px": ""},
        {"path": "b", "issue": "", "artist": "", "title": "x", "album": "x",
         "has_art": "", "art_min_px": ""},
        {"path": "c", "issue": "", "artist": "x", "title": "x", "album": "x",
         "has_art": "1", "art_min_px": "100"},
    ]
    missing = census.missing_paths(rows, art_min_px=300)
    assert missing["artist"] == {"b"}      # the issue row is excluded
    assert missing["art"] == {"b", "c"}    # absent + undersized (100 < 300)


def test_progress_ignores_files_added_since_the_baseline():
    """before/after used to be totals over two different file sets, so
    adding untagged music made "fixed" negative -- a real run rendered
    `artist 1 10 -9 -900%`. Only paths in both censuses can be compared."""

    def row(path, artist):
        return {"path": path, "issue": "", "artist": artist, "title": "t",
                "album": "a", "has_art": "1", "art_min_px": "900"}

    baseline = [row("keep.mp3", ""), row("fixed.mp3", "")]
    current = [row("keep.mp3", ""), row("fixed.mp3", "Someone")] + [
        row(f"new{i}.mp3", "") for i in range(9)]        # 9 new untagged files

    was = census.missing_paths(baseline, 300)
    now = census.missing_paths(current, 300)
    tracked = {r["path"] for r in baseline} & {r["path"] for r in current}

    b = was["artist"] & tracked
    still = b & now["artist"]
    assert len(b) == 2 and len(still) == 1
    fixed = len(b) - len(still)
    assert fixed == 1, "one of the two tracked files got an artist"
    assert round(100 * fixed / len(b)) == 50, "not a negative percentage"


def test_first_run_shows_no_progress_table(tmp_path, capsys):
    """census.run() creates the baseline when it's missing, so on a
    first-ever run the baseline is a copy of the census taken moments
    earlier and every field reports 0 fixed / 0%. A table of zeros reads
    like a failure rather than "nothing to compare against yet", so the
    section is suppressed until a real baseline predates the run."""
    if shutil.which("ffmpeg") is None:
        pytest.skip("needs ffmpeg to build a real fixture mp3")
    root = tmp_path / "music"
    (root / "Album").mkdir(parents=True)
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi",
                    "-i", "sine=frequency=440:duration=1",
                    "-c:a", "libmp3lame", "-b:a", "64k",
                    str(root / "Album" / "01.mp3")], check=True)

    ctx = _ctx(tmp_path, root)
    assert not (ctx.workdir / "census-baseline.csv").exists()
    report.run(ctx)
    first_out = capsys.readouterr().out
    assert "Fixed since the first run" not in first_out
    assert "files tracked" in first_out  # the rest of the report still ran

    # Second run: a baseline now predates it, so the comparison is real.
    ctx2 = _ctx(tmp_path, root)
    assert (ctx2.workdir / "census-baseline.csv").exists()
    report.run(ctx2)
    assert "Fixed since the first run" in capsys.readouterr().out


def test_repeated_journal_entries_across_runs_are_deduped(tmp_path):
    if shutil.which("ffmpeg") is None:
        pytest.skip("needs ffmpeg to build a real fixture mp3")
    root = tmp_path / "music"
    (root / "Some Album").mkdir(parents=True)
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi",
                    "-i", "sine=frequency=440:duration=1",
                    "-c:a", "libmp3lame", "-b:a", "64k",
                    str(root / "Some Album" / "01 Track.mp3")], check=True)
    ctx = _ctx(tmp_path, root)

    # Three sessions' worth of the identical reject, as append-only
    # journal.jsonl would actually accumulate it. The path is the folder,
    # which is the only shape mb.py ever emits -- this test used to write a
    # per-file path, so it passed while exercising a record production does
    # not produce, and missed the report-side lookup bug entirely.
    line = ('{"stage": "mb", "path": "Some Album", '
           '"action": "reject", "field": "release", '
           '"evidence": {"reason": "no candidate passed the gate"}, '
           '"ts": "2026-08-2%d"}\n')
    with open(ctx.workdir / "journal.jsonl", "w", encoding="utf-8") as f:
        for day in (1, 2, 3):
            f.write(line % day)

    report.run(ctx)

    with open(ctx.workdir / "report" / "unresolved.csv", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    row = next(r for r in rows if r["path"] == "Some Album/01 Track.mp3")
    assert row["tried"] == "mb: no candidate passed the gate", (
        "three identical reject records across three sessions must collapse "
        "to one entry, not repeat verbatim")


def test_report_returns_what_it_rendered(tmp_path):
    """External review: run() computed clean intermediate structures and
    handed them straight to format_text, so an embedder had to re-read the
    CSVs it had just watched get written."""
    root = tmp_path / "music"
    (root / "Some Album").mkdir(parents=True)
    ctx = _ctx(tmp_path, root)

    data = report.run(ctx)

    assert set(data) == {"tracked", "stage_counts", "unresolved",
                         "reacquire", "stubs", "review_queue", "progress"}
    assert data["tracked"] == 0

    # And it agrees with the CSV it wrote, which is the point.
    with open(ctx.workdir / "report" / "unresolved.csv", newline="",
              encoding="utf-8") as f:
        assert [r["path"] for r in csv.DictReader(f)] == \
            [r["path"] for r in data["unresolved"]]


def test_collect_does_not_print(tmp_path, capsys):
    """The split is only worth having if the compute half is usable on its
    own."""
    root = tmp_path / "music"
    root.mkdir(parents=True)
    ctx = _ctx(tmp_path, root)
    capsys.readouterr()

    report.collect(ctx)

    out = capsys.readouterr().out
    assert "=====" not in out, "collect() rendered a report"
