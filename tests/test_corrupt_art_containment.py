"""One corrupt file in 100 GB must cost that file.

External review: census.collect called probe.read_art bare after has_art
came back true. For Ogg and Opus, has_art only checks that a
metadata_block_picture comment exists -- the base64 decode and the
Picture() struct parse happen inside read_art. A truncated or crafted
comment raises binascii.Error or a mutagen error, neither of which is a
ProbeError, so the census died and took every stage with it, since
census.load() calls run().

The same unguarded call was in backup.snapshot, which would have crashed a
--backup-tags run partway through, and in art_local.needs_art, the late
re-check mb makes before embedding.
"""

import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

pytest.importorskip("mutagen")
if not shutil.which("ffmpeg"):
    pytest.skip("needs ffmpeg", allow_module_level=True)

from make_fixtures import vorbis_args

from tagfill import config, probe
from tagfill.backup import TagBackup
from tagfill.journal import Journal, ReviewQueue
from tagfill.stages import Context, art_local, census


def _ogg_with_broken_art(path: Path) -> None:
    """A picture comment that is not decodable base64. has_art says yes;
    everything that tries to read it says otherwise."""
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                    "sine=frequency=440:duration=1", *vorbis_args(),
                    str(path)], check=True)
    from mutagen.oggvorbis import OggVorbis
    audio = OggVorbis(path)
    audio["metadata_block_picture"] = ["!!!! not base64 at all !!!!"]
    audio.save()


def _ctx(tmp_path, root):
    cfg = config.Config()
    cfg.root, cfg.workdir = root, tmp_path / "work"
    return Context(cfg=cfg, journal=Journal(cfg.workdir),
                   review=ReviewQueue(cfg.workdir))


def test_read_art_raises_probeerror_not_binascii(tmp_path):
    f = tmp_path / "broken.ogg"
    _ogg_with_broken_art(f)
    assert probe.read(f).has_art, "the comment is there; that is the trap"
    with pytest.raises(probe.ProbeError):
        probe.read_art(f)


def test_the_census_survives_and_says_why(tmp_path):
    root = tmp_path / "Music"
    _ogg_with_broken_art(root / "Album" / "broken.ogg")
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                    "sine=frequency=440:duration=1", *vorbis_args(),
                    str(root / "Album" / "fine.ogg")], check=True)
    ctx = _ctx(tmp_path, root)

    rows = census.collect(ctx)

    assert len(rows) == 2
    broken = next(r for r in rows if "broken" in r["path"])
    assert "unreadable art" in broken["issue"]
    assert next(r for r in rows if "fine" in r["path"])["issue"] == ""


def test_the_backup_still_captures_the_tags(tmp_path):
    """And records that the art could not be read, so restore leaves it
    alone instead of stripping whatever is actually in there."""
    root = tmp_path / "Music"
    root.mkdir()
    f = root / "broken.ogg"
    _ogg_with_broken_art(f)
    probe.write(f, {"artist": "Someone"})

    tb = TagBackup(tmp_path / "work")
    tb.snapshot(root, f)

    line = tb.path.read_text(encoding="utf-8")
    assert '"artist": "Someone"' in line
    assert '"art_unreadable": true' in line


def test_needs_art_treats_unreadable_as_missing(tmp_path):
    f = tmp_path / "broken.ogg"
    _ogg_with_broken_art(f)
    assert art_local.needs_art(f, 500) is True


def test_the_art_size_comes_from_the_same_parse(tmp_path):
    """External review: census called read_art purely to measure the art,
    re-opening and re-parsing the whole container to read a JPEG header."""
    f = tmp_path / "fine.ogg"
    f.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                    "sine=frequency=440:duration=1", *vorbis_args(),
                    str(f)], check=True)
    import io

    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (800, 600), (10, 20, 30)).save(buf, "PNG")
    probe.embed_art(f, buf.getvalue(), "image/png")

    opens = []
    real = probe._open
    probe._open = lambda p: (opens.append(p), real(p))[1]
    try:
        st = probe.read(f)
    finally:
        probe._open = real

    assert st.art_min_px == 600
    assert len(opens) == 1, f"read() opened the container {len(opens)} times"
