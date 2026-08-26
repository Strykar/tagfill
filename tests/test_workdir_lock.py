"""One writer per workdir.

External review, on embedding tagfill in a GUI: two concurrent runs will
not corrupt the append-only journal structurally, but they race each
other's census.csv rewrite and interleave decisions about the same files.
A GUI is exactly where a user double-clicks "run".

O_EXCL create is the primitive because it is atomic on both platforms and
needs neither fcntl (absent on Windows) nor msvcrt (absent on POSIX). The
cost is a stale file after a kill, so the holder's pid goes inside and a
dead holder is taken over rather than obeyed -- otherwise one crash locks
the user out of their own workdir with no way back but deleting a file
nobody told them about.
"""

import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from tagfill.lock import WorkdirBusy, WorkdirLock

REPO = Path(__file__).resolve().parents[1]


def test_a_second_holder_is_refused(tmp_path):
    # A pid that is alive and is not ours, so it can be mistaken neither
    # for re-entry nor for a crashed run.
    lock = WorkdirLock(tmp_path)
    lock.acquire()
    (tmp_path / "lock").write_text(str(os.getppid()), encoding="utf-8")
    try:
        with pytest.raises(WorkdirBusy):
            WorkdirLock(tmp_path).acquire()
    finally:
        lock.release()


def test_the_lock_is_released_on_the_way_out(tmp_path):
    with WorkdirLock(tmp_path):
        assert (tmp_path / "lock").exists()
    assert not (tmp_path / "lock").exists()


def test_it_is_released_even_when_the_run_raises(tmp_path):
    with pytest.raises(ValueError), WorkdirLock(tmp_path):
        raise ValueError("stage blew up")
    assert not (tmp_path / "lock").exists()


def test_a_lock_left_by_a_dead_process_is_taken_over(tmp_path):
    """The alternative is a crash locking the user out of their own
    workdir, with no way back but deleting a file nobody told them about."""
    (tmp_path / "lock").write_text("999999", encoding="utf-8")
    with WorkdirLock(tmp_path):
        assert (tmp_path / "lock").read_text(encoding="utf-8") \
            == str(os.getpid())


def test_an_unreadable_lock_file_is_taken_over_not_obeyed(tmp_path):
    (tmp_path / "lock").write_text("not a pid", encoding="utf-8")
    with WorkdirLock(tmp_path):
        pass


def test_a_live_holder_blocks_the_cli(tmp_path):
    """End to end: the second run exits 1 and says what is going on,
    instead of quietly interleaving with the first."""
    work = tmp_path / "work"
    music = tmp_path / "Music"
    music.mkdir()
    lock = WorkdirLock(work)
    lock.acquire()
    (work / "lock").write_text(str(os.getpid()), encoding="utf-8")
    try:
        r = subprocess.run(
            [sys.executable, "-c",
             f"import sys; sys.path.insert(0, {str(REPO)!r}); "
             "from tagfill.cli import main; sys.exit(main(sys.argv[1:]))",
             "--music-dir", str(music), "--workdir", str(work), "census"],
            capture_output=True, text=True, encoding="utf-8")
        assert r.returncode == 1
        assert "another tagfill run" in r.stdout
        assert "--workdir" in r.stdout
    finally:
        lock.release()


def test_report_does_not_need_the_lock(tmp_path):
    """--report is read-only from the user's point of view and is the one
    thing you want to run while a long sweep is going."""
    music = tmp_path / "Music"
    music.mkdir()
    work = tmp_path / "work"
    WorkdirLock(work).acquire()
    (work / "lock").write_text(str(os.getpid() + 1), encoding="utf-8")

    from tagfill import cli
    assert cli.main(["--music-dir", str(music), "--workdir", str(work),
                     "--report"]) == 0
