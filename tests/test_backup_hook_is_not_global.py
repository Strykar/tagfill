"""The backup hook used to be a monkeypatch of probe.write and
probe.embed_art -- process-wide mutation of module globals, closed over one
Context's workdir, never uninstalled.

External review: fine for a CLI process that runs one command and exits,
hazardous for anything long-lived. Run collection A with --backup-tags,
then collection B without, and B's writes still flow through A's wrapper
and snapshot into A's backup file, silently, against the wrong workdir.
Toggle backup across runs and the wrappers stack.

The seam already existed: every write in every stage goes through
guarded_write. So the backup is a field on Context now, and the first test
here pins the invariant that makes that safe.
"""

import re
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from tagfill import config, probe
from tagfill.backup import TagBackup
from tagfill.journal import Journal, ReviewQueue
from tagfill.stages import Context, guarded_write

STAGES = Path(__file__).resolve().parents[1] / "tagfill" / "stages"


def test_every_stage_write_goes_through_guarded_write():
    """The whole design rests on this. A stage calling probe.write directly
    would skip the backup and the failure journaling both."""
    offenders = []
    for src in sorted(STAGES.glob("*.py")):
        if src.name == "__init__.py":
            continue
        text = src.read_text(encoding="utf-8")
        for m in re.finditer(r"probe\.(write|embed_art|delete_fields|"
                             r"remove_art)\(", text):
            line_start = text.rindex("\n", 0, m.start()) + 1
            # guarded_write takes the function as an argument, so the call
            # site reads "probe.write," with no open paren of its own.
            before = text[max(0, m.start() - 200):m.start()]
            if "guarded_write(" in before:
                continue
            offenders.append(
                f"{src.name}:{text[:line_start].count(chr(10)) + 1}")
    assert not offenders, (
        "direct probe write outside guarded_write: " + ", ".join(offenders))


def test_probe_is_never_monkeypatched():
    """The specific mechanism that made this global. If it comes back, it
    comes back silently."""
    cli = (Path(__file__).resolve().parents[1] / "tagfill" / "cli.py"
           ).read_text(encoding="utf-8")
    assert "probe.write, probe.embed_art =" not in cli
    assert "_install_backup_hook" not in cli


def _ctx(tmp_path, name, *, backup):
    cfg = config.Config()
    cfg.root, cfg.workdir = tmp_path / name, tmp_path / f"work-{name}"
    cfg.root.mkdir(parents=True, exist_ok=True)
    ctx = Context(cfg=cfg, journal=Journal(cfg.workdir),
                  review=ReviewQueue(cfg.workdir), apply=True)
    if backup:
        ctx.backup = TagBackup(cfg.workdir)
    return ctx


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")
def test_one_collections_backup_does_not_capture_anothers_writes(tmp_path):
    """The exact failure the monkeypatch produced: run A with backup, then
    B without, and B ended up in A's tags.jsonl."""
    a = _ctx(tmp_path, "A", backup=True)
    b = _ctx(tmp_path, "B", backup=False)
    for ctx, stem in ((a, "a"), (b, "b")):
        track = ctx.root / f"{stem}.mp3"
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                        "sine=frequency=440:duration=1", "-c:a",
                        "libmp3lame", str(track)], check=True)
        guarded_write(ctx, "mb", f"{stem}.mp3", probe.write, track,
                      {"album": "Written"})

    captured = a.backup.path.read_text(encoding="utf-8")
    assert "a.mp3" in captured
    assert "b.mp3" not in captured
    assert not (b.workdir / "backup" / "tags.jsonl").exists()


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")
def test_the_snapshot_still_happens_before_the_write(tmp_path):
    """A backup taken after the write backs up the new value, which is not
    a backup."""
    ctx = _ctx(tmp_path, "A", backup=True)
    track = ctx.root / "a.mp3"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                    "sine=frequency=440:duration=1", "-c:a", "libmp3lame",
                    str(track)], check=True)
    probe.write(track, {"album": "Original"})

    guarded_write(ctx, "mb", "a.mp3", probe.write, track,
                  {"album": "Replaced"}, overwrite=True)

    assert probe.read(track).fields["album"] == "Replaced"
    assert '"album": "Original"' in ctx.backup.path.read_text(encoding="utf-8")


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")
def test_backup_tags_on_the_command_line_produces_a_backup(tmp_path):
    """End to end through cli.main, which is the wiring that changed.
    Everything else here builds its own Context, so nothing was checking
    that --backup-tags reaches a stage at all."""
    from tagfill import cli
    root = tmp_path / "Music" / "Some Album"
    root.mkdir(parents=True)
    track = root / "Portishead - Mysterons.mp3"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                    "sine=frequency=440:duration=1", "-c:a", "libmp3lame",
                    str(track)], check=True)
    work = tmp_path / "work"

    rc = cli.main(["--music-dir", str(tmp_path / "Music"),
                   "--workdir", str(work), "filename", "--apply",
                   "--backup-tags"])

    assert rc == 0
    assert probe.read(track).fields["artist"] == "Portishead"
    backup = work / "backup" / "tags.jsonl"
    assert backup.exists(), "--backup-tags wrote no backup"
    assert "Portishead - Mysterons.mp3" in backup.read_text(encoding="utf-8")


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")
def test_no_backup_flag_means_no_backup_file(tmp_path):
    from tagfill import cli
    root = tmp_path / "Music" / "Some Album"
    root.mkdir(parents=True)
    track = root / "Portishead - Mysterons.mp3"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                    "sine=frequency=440:duration=1", "-c:a", "libmp3lame",
                    str(track)], check=True)
    work = tmp_path / "work"

    cli.main(["--music-dir", str(tmp_path / "Music"), "--workdir", str(work),
              "filename", "--apply"])

    assert not (work / "backup" / "tags.jsonl").exists()
