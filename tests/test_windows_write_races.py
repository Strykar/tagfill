"""Two Windows-only behaviours that Linux cannot reproduce, so both are
tested by driving the platform branch directly.

External review, both real:

os.replace on a file another process holds open is unremarkable on POSIX
and a sharing violation on Windows unless that process passed
FILE_SHARE_DELETE. The holders a music collection attracts are a player
with the album loaded, Explorer's preview pane, the search indexer, and
real-time antivirus scanning the temp file we just wrote. guarded_write
turns that into a journalled skip, which is the right floor, but the
antivirus case is a transient race -- without a retry, runs go
nondeterministically lossy on the one OS where real-time scanning ships on
by default.

The resume guard demanded mtime equality to within a microsecond before it
would even consult the hash. exFAT and FAT32 -- USB sticks, SD cards --
store mtimes at 2-second granularity and in local time, so a remount or a
DST transition shifts every one of them and defeats the guard wholesale.
"""

import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from tagfill import probe
from tagfill.journal import Journal


class _FlakyReplace:
    """Fails like Windows does, then succeeds, like antivirus letting go."""

    def __init__(self, failures, real):
        self.left, self.real, self.calls = failures, real, 0

    def __call__(self, src, dst):
        self.calls += 1
        if self.left:
            self.left -= 1
            raise PermissionError(32, "The process cannot access the file")
        return self.real(src, dst)


def test_a_transient_sharing_violation_is_retried(tmp_path, monkeypatch):
    src, dst = tmp_path / "a", tmp_path / "b"
    src.write_text("new", encoding="utf-8")
    dst.write_text("old", encoding="utf-8")
    import os
    flaky = _FlakyReplace(2, os.replace)
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(os, "replace", flaky)

    probe._replace_with_retry(src, dst)

    assert dst.read_text(encoding="utf-8") == "new"
    assert flaky.calls == 3


def test_a_permanent_sharing_violation_still_raises(tmp_path, monkeypatch):
    """The retry buys time for a race, it does not paper over a file that
    is genuinely locked -- guarded_write still gets to journal the skip."""
    src, dst = tmp_path / "a", tmp_path / "b"
    src.write_text("new", encoding="utf-8")
    import os
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(os, "replace", _FlakyReplace(99, os.replace))
    monkeypatch.setattr("time.sleep", lambda _s: None)

    with pytest.raises(PermissionError):
        probe._replace_with_retry(src, dst)


def test_posix_does_not_retry(tmp_path, monkeypatch):
    """A PermissionError on Linux means the permissions are wrong, and
    sleeping will not fix that."""
    src, dst = tmp_path / "a", tmp_path / "b"
    src.write_text("new", encoding="utf-8")
    import os
    flaky = _FlakyReplace(1, os.replace)
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(os, "replace", flaky)

    with pytest.raises(PermissionError):
        probe._replace_with_retry(src, dst)
    assert flaky.calls == 1


def _applied(journal, root, path):
    journal.record_write("mb", root, path, "genre", None, "Techno")


def test_a_coarse_mtime_no_longer_defeats_the_resume_guard(tmp_path):
    """exFAT rounds to 2 seconds; a remount or a DST shift moves every
    mtime. The bytes did not change, so the file is still done."""
    import os
    root = tmp_path / "Music"
    root.mkdir()
    f = root / "track.mp3"
    f.write_bytes(b"audio bytes")
    j = Journal(tmp_path / "work")
    _applied(j, root, f)
    assert j.already_done("mb", root, f)

    st = f.stat()
    os.utime(f, (st.st_atime, st.st_mtime + 3600))  # a DST-sized shift
    j._applied = None
    assert j.already_done("mb", root, f), (
        "the content is unchanged; only the filesystem's clock moved")


def test_changed_content_is_still_not_done(tmp_path):
    root = tmp_path / "Music"
    root.mkdir()
    f = root / "track.mp3"
    f.write_bytes(b"audio bytes")
    j = Journal(tmp_path / "work")
    _applied(j, root, f)

    f.write_bytes(b"different!")
    j._applied = None
    assert not j.already_done("mb", root, f)


def test_a_same_size_edit_is_caught_by_the_hash(tmp_path):
    """Same length, different bytes, and an mtime the guard cannot trust --
    the head-hash is the only thing left, which is why it is the evidence
    and mtime is only the prefilter."""
    import os
    root = tmp_path / "Music"
    root.mkdir()
    f = root / "track.mp3"
    f.write_bytes(b"audio bytes")
    j = Journal(tmp_path / "work")
    _applied(j, root, f)

    st = f.stat()
    f.write_bytes(b"AUDIO BYTES")
    os.utime(f, (st.st_atime, st.st_mtime + 3600))
    j._applied = None
    assert not j.already_done("mb", root, f)


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")
def test_the_real_write_path_goes_through_the_retry(tmp_path, monkeypatch):
    """Testing the helper proves the helper. This proves _atomic_save
    actually calls it, which is the part a refactor would quietly drop."""
    import os
    f = tmp_path / "track.mp3"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                    "sine=frequency=440:duration=1", "-c:a", "libmp3lame",
                    str(f)], check=True)
    flaky = _FlakyReplace(1, os.replace)
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(os, "replace", flaky)
    monkeypatch.setattr("time.sleep", lambda _s: None)

    probe.write(f, {"album": "Survived the race"})

    assert flaky.calls == 2
    assert probe.read(f).fields["album"] == "Survived the race"
