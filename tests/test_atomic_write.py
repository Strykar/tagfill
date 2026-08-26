"""Tag writes must be crash-safe.

mutagen edits in place: verified by adding a 1200px cover to an MP3 and
watching the inode stay the same while the file grew 24KB, which means the
whole audio payload was shifted forward inside the original. A power cut
during that leaves a corrupt track and no copy to fall back on.

probe._atomic_save() does the edit on a copy in the same directory, fsyncs
it, then os.replace()s it over the original -- atomic on POSIX and on
Windows for a same-directory rename. Verified by SIGKILLing a real process
mid-write: the original came back byte-identical.
"""

import hashlib
import os
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from tagfill import probe
from tagfill.util import iter_audio

needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None,
                                  reason="needs ffmpeg")


def _mp3(path, seconds=2):
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi",
                    "-i", f"sine=frequency=440:duration={seconds}",
                    "-c:a", "libmp3lame", "-b:a", "128k", str(path)],
                   check=True)


def _sha(p):
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


@needs_ffmpeg
def test_write_replaces_the_file_rather_than_editing_in_place(tmp_path):
    p = tmp_path / "t.mp3"
    _mp3(p)
    before = os.stat(p).st_ino
    probe.write(p, {"artist": "Someone", "title": "Something"})
    assert os.stat(p).st_ino != before, (
        "an in-place edit is not crash-safe; the write must land via "
        "os.replace onto a new inode")
    assert probe.read(p).get("artist") == "Someone"


@needs_ffmpeg
def test_a_failed_write_leaves_the_original_untouched(tmp_path):
    p = tmp_path / "t.mp3"
    _mp3(p)
    before = _sha(p)

    def boom(audio, family):
        raise RuntimeError("disk went away")

    with pytest.raises(RuntimeError):
        probe._atomic_save(p, boom)

    assert _sha(p) == before, "original must be byte-identical after a failure"
    assert not list(tmp_path.glob(".*tagfill-tmp*")), "temp must be cleaned"


@needs_ffmpeg
def test_a_hard_kill_mid_write_cannot_corrupt_the_original(tmp_path):
    """The power-cut case: SIGKILL gives no chance to clean up, so this is
    the one that proves os.replace is doing the work rather than the
    exception handler."""
    p = tmp_path / "t.mp3"
    _mp3(p)
    before = _sha(p)

    script = (
        "import os, signal, sys;"
        f"sys.path.insert(0, {str(Path(__file__).resolve().parents[1])!r});"
        "from pathlib import Path;"
        "from tagfill import probe;"
        "probe._atomic_save(Path(%r), "
        "lambda a, f: os.kill(os.getpid(), signal.SIGKILL))" % str(p)
    )
    r = subprocess.run([sys.executable, "-c", script], capture_output=True)
    assert r.returncode != 0, "the child was supposed to die"
    assert _sha(p) == before, "a killed write must not touch the original"


@needs_ffmpeg
def test_a_leftover_temp_is_not_mistaken_for_music(tmp_path):
    """A killed run leaves its temp behind. A later scan must never pick it
    up and start tagging it as if it were a track."""
    _mp3(tmp_path / "real.mp3")
    shutil.copy2(tmp_path / "real.mp3",
                 tmp_path / ".real.tagfill-tmp.mp3")
    found = [p.name for p in iter_audio(tmp_path)]
    assert found == ["real.mp3"]


@needs_ffmpeg
def test_a_write_does_not_change_ownership_or_permissions(tmp_path):
    """Measured, not assumed: shutil.copy2 carries mode and xattrs but not
    ownership, so a library group-owned by `media` or `audio` had every
    file silently rewritten to the running user's primary group. That is a
    permission change on files the tool is only supposed to be tagging."""
    import grp
    import os
    import stat

    p = tmp_path / "t.mp3"
    _mp3(p)
    os.chmod(p, 0o640)

    # A group we belong to but that is not our primary one, if one exists.
    primary = os.getgid()
    secondary = next((g for g in os.getgroups() if g != primary), None)
    if secondary is not None:
        os.chown(p, -1, secondary)

    before = os.stat(p)
    probe.write(p, {"artist": "Someone"})
    after = os.stat(p)

    assert stat.S_IMODE(after.st_mode) == stat.S_IMODE(before.st_mode)
    assert after.st_uid == before.st_uid
    assert after.st_gid == before.st_gid, (
        f"group changed from {grp.getgrgid(before.st_gid).gr_name} to "
        f"{grp.getgrgid(after.st_gid).gr_name}")


@needs_ffmpeg
def test_a_write_preserves_extended_attributes(tmp_path):
    import os
    p = tmp_path / "t.mp3"
    _mp3(p)
    try:
        os.setxattr(p, "user.tagfill_test", b"keepme")
    except OSError:
        pytest.skip("filesystem does not support xattrs")

    probe.write(p, {"artist": "Someone"})
    assert os.getxattr(p, "user.tagfill_test") == b"keepme"


@needs_ffmpeg
def test_a_write_works_where_os_chown_does_not_exist(tmp_path, monkeypatch):
    """Windows has no os.chown, and AttributeError is not an OSError, so
    suppressing only PermissionError/OSError made every write there raise.
    CI caught it; this reproduces it on Linux by hiding the attribute."""
    import os
    monkeypatch.delattr(os, "chown", raising=False)
    p = tmp_path / "t.mp3"
    _mp3(p)
    probe.write(p, {"artist": "Someone"})       # must not raise
    assert probe.read(p).get("artist") == "Someone"
