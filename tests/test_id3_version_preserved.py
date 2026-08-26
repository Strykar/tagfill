"""External review: mutagen's save() writes ID3v2.4 regardless of what it
read, so every write silently upgraded an existing v2.3 tag. Confirmed
before fixing -- an ffmpeg mp3 tagged v2.3, reopened and saved, came back
(2, 4, 0).

It matters because v2.3 is the version Windows Explorer and older hardware
players read reliably; a tool whose whole promise is "fills blanks, changes
nothing else" must not cost a file its readability everywhere else on the
way. A file that already has v2.4, or has no tag at all, stays v2.4 --
this preserves, it does not downgrade.
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

from mutagen.aiff import AIFF
from mutagen.id3 import TPE1
from mutagen.mp3 import MP3
from mutagen.wave import WAVE

from tagfill import probe

# ID3.save() writes a bare tag at the head of the file, which is right for
# mp3 and corrupts an IFF/RIFF container. Build through the container class.
OPENERS = {"mp3": MP3, "aiff": AIFF, "wav": WAVE}

# Every id3-family container probe.py knows that ffmpeg can produce.
CONTAINERS = ["mp3", "aiff", "wav"]


def _build(path: Path, v2_version: int) -> None:
    subprocess.run(["ffmpeg", "-y", "-loglevel", "quiet", "-f", "lavfi",
                    "-i", "anullsrc=r=8000:cl=mono", "-t", "1", str(path)],
                   check=True)
    audio = OPENERS[path.suffix.lstrip(".")](path)
    if audio.tags is None:
        audio.add_tags()
    audio.tags.add(TPE1(encoding=1, text=["Someone"]))
    audio.save(v2_version=v2_version)


def _version(path: Path) -> tuple:
    """Read it back through the container class: bare ID3(path) only works
    on mp3, where the tag really is at the head of the file."""
    return OPENERS[path.suffix.lstrip(".")](path).tags.version[:2]


@pytest.mark.parametrize("suffix", CONTAINERS)
def test_a_v23_tag_stays_v23_across_a_write(tmp_path, suffix):
    f = tmp_path / f"track.{suffix}"
    _build(f, 3)
    assert _version(f) == (2, 3)
    probe.write(f, {"album": "Some Album"})
    assert _version(f) == (2, 3)
    assert probe.read(f).fields["album"] == "Some Album"


@pytest.mark.parametrize("suffix", CONTAINERS)
def test_a_v24_tag_stays_v24(tmp_path, suffix):
    f = tmp_path / f"track.{suffix}"
    _build(f, 4)
    probe.write(f, {"album": "Some Album"})
    assert _version(f) == (2, 4)


def test_embedding_art_does_not_upgrade_the_tag_either(tmp_path):
    """Art goes through the same save path, and it is the write most likely
    to be the only one a file gets."""
    png = (b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR"
           + (1000).to_bytes(4, "big") + (1000).to_bytes(4, "big"))
    f = tmp_path / "track.mp3"
    _build(f, 3)
    probe.embed_art(f, png, "image/png")
    assert _version(f) == (2, 3)
