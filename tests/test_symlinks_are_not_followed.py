"""A write does not go *through* a symlink, it replaces it.

External review, and reproduced before fixing: iter_audio used is_file(),
which follows symlinks, so a link inside the collection pointing outside it
was censused and written. _atomic_save is copy2 + os.replace, so the link
was left as a regular file, the real target untouched, and the content
silently forked. The probe docstring reasons carefully about hardlinks
(nlink 2 -> 1) and never mentioned this case.

Link-farm layouts -- dedup setups, beets-style views -- are common enough
in this audience that the policy has to be explicit: skipped, and recorded
in the census so a link farm does not look like a collection where nothing
needs doing.
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

from tagfill import config, probe
from tagfill.journal import Journal, ReviewQueue
from tagfill.stages import Context, census
from tagfill.util import iter_audio


def _mp3(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                    "sine=frequency=440:duration=1", "-c:a", "libmp3lame",
                    str(path)], check=True)


def _linked(tmp_path):
    outside = tmp_path / "outside"
    root = tmp_path / "Music" / "Album"
    root.mkdir(parents=True)
    outside.mkdir()
    real = outside / "track.mp3"
    _mp3(real)
    link = root / "track.mp3"
    link.symlink_to(real)
    return tmp_path / "Music", real, link


def test_the_walk_does_not_yield_a_symlink(tmp_path):
    music, _real, _link = _linked(tmp_path)
    skipped = []
    assert list(iter_audio(music, skipped_links=skipped)) == []
    assert [p.name for p in skipped] == ["track.mp3"]


def test_the_write_path_refuses_one_outright(tmp_path):
    """Belt and braces: paths also arrive from --from-review and restore,
    which read a CSV a human edits."""
    _music, real, link = _linked(tmp_path)
    with pytest.raises(probe.ProbeError, match="symlink"):
        probe.write(link, {"album": "Written"})
    assert link.is_symlink(), "the link itself must survive"
    assert probe.read(real).fields["album"] is None


def test_the_census_records_it_instead_of_dropping_it(tmp_path):
    music, _real, _link = _linked(tmp_path)
    cfg = config.Config()
    cfg.root, cfg.workdir = music, tmp_path / "work"
    ctx = Context(cfg=cfg, journal=Journal(cfg.workdir),
                  review=ReviewQueue(cfg.workdir))

    rows = census.collect(ctx)

    assert len(rows) == 1
    assert rows[0]["path"] == "Album/track.mp3"
    assert "symlink" in rows[0]["issue"]


def test_a_real_file_beside_a_link_is_still_handled(tmp_path):
    """The skip must not cost the folder."""
    music, _real, _link = _linked(tmp_path)
    _mp3(music / "Album" / "real.mp3")
    found = [p.name for p in iter_audio(music)]
    assert found == ["real.mp3"]


def test_a_link_pointing_inside_the_collection_is_skipped_too(tmp_path):
    """Otherwise the same underlying file is tagged twice under two
    relative paths, and the resume guard keys on the relative path."""
    music = tmp_path / "Music" / "Album"
    music.mkdir(parents=True)
    real = music / "real.mp3"
    _mp3(real)
    (music / "alias.mp3").symlink_to(real)
    assert [p.name for p in iter_audio(tmp_path / "Music")] == ["real.mp3"]
