"""Fields and art go into one save.

External review: mb applied fields with probe.write and art with
probe.embed_art as separate calls, so a 60 MB FLAC getting date,
albumartist and a cover paid two full copy/parse/save/fsync cycles --
about 240 MB of I/O and five container parses for one logical change.
_atomic_save's copy-then-replace is the honest price of the crash-safety
guarantee; paying it twice is not.

The read probe.write already does answers "does this file still need art",
so batching removes the separate needs_art parse as well.
"""

import io
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

pytest.importorskip("mutagen")
pytest.importorskip("PIL")
if not shutil.which("ffmpeg"):
    pytest.skip("needs ffmpeg", allow_module_level=True)

from PIL import Image

from tagfill import probe


def _mp3(path: Path) -> Path:
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                    "sine=frequency=440:duration=1", "-c:a", "libmp3lame",
                    str(path)], check=True)
    return path


def _jpeg(px=900) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (px, px), (7, 8, 9)).save(buf, "JPEG")
    return buf.getvalue()


def _count_saves(monkeypatch):
    saves = []
    real = probe._atomic_save
    monkeypatch.setattr(probe, "_atomic_save",
                        lambda p, m: (saves.append(p), real(p, m))[1])
    return saves


def test_fields_and_art_cost_one_save(tmp_path, monkeypatch):
    f = _mp3(tmp_path / "t.mp3")
    saves = _count_saves(monkeypatch)

    changed = probe.write(f, {"album": "A", "date": "1994"},
                          art=(_jpeg(), "image/jpeg"), art_min_px=500)

    assert len(saves) == 1
    assert ("art", None, "900px") in changed
    st = probe.read(f)
    assert st.fields["album"] == "A" and st.has_art


def test_art_is_skipped_when_the_file_already_has_big_enough_art(tmp_path,
                                                                 monkeypatch):
    f = _mp3(tmp_path / "t.mp3")
    probe.embed_art(f, _jpeg(1200), "image/jpeg")
    saves = _count_saves(monkeypatch)

    changed = probe.write(f, {"album": "A"}, art=(_jpeg(900), "image/jpeg"),
                          art_min_px=500)

    assert len(saves) == 1
    assert not any(c[0] == "art" for c in changed)
    assert probe.read(f).art_min_px == 1200, "the better art survived"


def test_undersized_existing_art_is_replaced(tmp_path):
    f = _mp3(tmp_path / "t.mp3")
    probe.embed_art(f, _jpeg(200), "image/jpeg")
    probe.write(f, {}, art=(_jpeg(1000), "image/jpeg"), art_min_px=500)
    assert probe.read(f).art_min_px == 1000


def test_art_alone_still_saves_when_no_field_changes(tmp_path, monkeypatch):
    """`not todo` used to be an early return, which would have dropped the
    art on any file whose fields were already correct."""
    f = _mp3(tmp_path / "t.mp3")
    probe.write(f, {"album": "A"})
    saves = _count_saves(monkeypatch)

    changed = probe.write(f, {"album": "A"}, art=(_jpeg(), "image/jpeg"),
                          art_min_px=500)

    assert len(saves) == 1
    assert changed == [("art", None, "900px")]


def test_nothing_to_do_still_writes_nothing(tmp_path, monkeypatch):
    f = _mp3(tmp_path / "t.mp3")
    probe.write(f, {"album": "A"}, art=(_jpeg(), "image/jpeg"),
                art_min_px=500)
    saves = _count_saves(monkeypatch)

    assert probe.write(f, {"album": "A"}, art=(_jpeg(), "image/jpeg"),
                       art_min_px=500) == []
    assert saves == []


def test_mb_no_longer_re_parses_to_ask_whether_art_is_needed():
    src = (Path(__file__).resolve().parents[1] / "tagfill" / "stages"
           / "mb.py").read_text(encoding="utf-8")
    assert "needs_art(path" not in src
    assert "probe.embed_art" not in src, "art rides along with the fields"
