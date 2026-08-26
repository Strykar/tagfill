"""A real music folder is never just audio. Rips ship .nfo, .cue, .m3u,
.log and .txt alongside the tracks, cover images next to them, scans in a
subfolder, and occasionally a file with an audio extension that isn't
audio at all (a truncated download, an HTML error page saved as .mp3).

Pointing the tool at that must not crash and must not mistake clutter for
music: `iter_audio()` filters on extension, so non-audio never enters the
file universe, and a liar file with an audio extension is caught by
probe.read() and recorded as an `issue` for the reacquire list rather
than taking the run down with it.
"""

import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from tagfill import config
from tagfill.journal import Journal, ReviewQueue
from tagfill.stages import Context, census

needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None,
                                  reason="needs ffmpeg")


def _ctx(tmp_path, root):
    cfg = config.Config()
    cfg.root = root
    cfg.workdir = tmp_path / "work"
    return Context(cfg=cfg, journal=Journal(cfg.workdir),
                  review=ReviewQueue(cfg.workdir))


@needs_ffmpeg
def test_clutter_is_ignored_and_a_fake_mp3_is_reported_not_fatal(tmp_path):
    root = tmp_path / "music"
    album = root / "Some Album"
    (album / "Scans").mkdir(parents=True)
    (root / ".hidden").mkdir()

    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi",
                    "-i", "sine=frequency=440:duration=1",
                    "-c:a", "libmp3lame", "-b:a", "64k",
                    str(album / "01 real.mp3")], check=True)

    for name in ("info.nfo", "rip.log", "album.cue", "list.m3u", "notes.txt"):
        (album / name).write_text("not audio\n")
    (album / "booklet.pdf").write_bytes(b"%PDF-1.4 fake")
    (album / "cover.jpg").write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 64)
    (album / "Scans" / "front.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 64)
    # An audio extension that is not audio.
    (album / "fake.mp3").write_text("plain text pretending to be an mp3\n")
    # Hidden directories are outside the file universe entirely.
    (root / ".hidden" / "hidden.mp3").write_text("x\n")

    ctx = _ctx(tmp_path, root)
    rows = census.collect(ctx)  # must not raise
    paths = {r["path"] for r in rows}

    assert paths == {"Some Album/01 real.mp3", "Some Album/fake.mp3"}, (
        "only files with an audio extension enter the census; images, "
        "text, cue/log/m3u/pdf and hidden dirs must not")

    real = next(r for r in rows if r["path"].endswith("01 real.mp3"))
    fake = next(r for r in rows if r["path"].endswith("fake.mp3"))
    assert not real["issue"]
    assert fake["issue"].startswith("unreadable:"), (
        "a non-audio file wearing an audio extension must be recorded as an "
        "issue for the reacquire list, not crash the walk")


@needs_ffmpeg
def test_a_directory_with_no_audio_at_all_is_not_an_error(tmp_path):
    root = tmp_path / "music"
    root.mkdir()
    (root / "readme.txt").write_text("nothing to see\n")
    (root / "pic.jpg").write_bytes(b"\xff\xd8\xff\xe0")

    ctx = _ctx(tmp_path, root)
    assert census.collect(ctx) == []


@needs_ffmpeg
@pytest.mark.skipif(hasattr(__import__("os"), "geteuid")
                    and __import__("os").geteuid() == 0,
                    reason="root ignores the read-only bit")
def test_one_unwritable_file_does_not_abort_the_batch(tmp_path):
    """Found live: chmod 444 on the second of three files in an album made
    embed_art() raise MutagenError straight out of the stage. The run died
    with a traceback and exit 1, and the perfectly writable third file never
    got its art. One unwritable file must cost that file only, and the
    reason must reach the journal so report.py can explain the gap."""
    import os

    from tagfill.stages import art_local

    root = tmp_path / "music"
    album = root / "Album"
    album.mkdir(parents=True)
    for n in ("01", "02", "03"):
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi",
                        "-i", "sine=frequency=440:duration=1",
                        "-c:a", "libmp3lame", "-b:a", "64k",
                        str(album / f"{n}.mp3")], check=True)

    # Seed 01 with big art so the siblings have a source to harvest.
    import io

    from mutagen.id3 import APIC
    from mutagen.mp3 import MP3
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (800, 800), (10, 90, 200)).save(buf, "JPEG")
    a = MP3(album / "01.mp3")
    if a.tags is None:
        a.add_tags()
    a.tags.add(APIC(encoding=3, mime="image/jpeg", type=3, desc="",
                    data=buf.getvalue()))
    a.save()

    os.chmod(album / "02.mp3", 0o444)      # sorts before 03
    try:
        ctx = _ctx(tmp_path, root)
        ctx.apply = True
        art_local.run(ctx)                  # must not raise

        assert not probe_has_art(album / "02.mp3"), "unwritable file unchanged"
        assert probe_has_art(album / "03.mp3"), (
            "the file after the unwritable one must still be processed")

        import json
        with open(ctx.workdir / "journal.jsonl") as jf:
            skips = [json.loads(line) for line in jf]
        assert any(r.get("action") == "skip"
                   and "write failed" in r.get("evidence", {}).get("reason", "")
                   for r in skips), "the failure must be journalled, not silent"
    finally:
        os.chmod(album / "02.mp3", 0o644)


def probe_has_art(path) -> bool:
    from tagfill import probe
    return probe.read(Path(path)).has_art
