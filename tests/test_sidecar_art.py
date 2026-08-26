"""Tests for probe.find_sidecar_art().

Found live on the real collection: a 5-disc scene rip ("VA - Rhythm Is A
Dancer - 90's Club Classics") named each disc's cover after the release
itself — "Various Artists - Rhythm Is A Dancer - 90's Club Classics.jpg" —
which no fixed-name list (cover.jpg/folder.jpg/front.jpg) can enumerate.
The same release also keeps a separate Cover/ subfolder holding Front.jpg,
Back.jpg, Full.jpg and five per-disc scans, with nothing at the album root
matching a generic name at all in the general case (this specific release
happens to also symlink cover.jpg -> Front.jpg at the root, but the
subfolder fallback must work without that too).

No fixtures/mutagen needed — this is pure filesystem logic.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tagfill import probe


def _touch(*paths):
    for p in paths:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"x")


def test_generic_name_wins_over_everything_else(tmp_path):
    _touch(tmp_path / "track.mp3", tmp_path / "cover.jpg",
          tmp_path / "some-random-scan.jpg")
    hit = probe.find_sidecar_art(tmp_path)
    assert hit.name == "cover.jpg"


def test_album_named_sidecar_is_found_when_it_is_the_only_image(tmp_path):
    """The exact real case: no generic name, but exactly one image."""
    _touch(tmp_path / "01 Track.flac",
          tmp_path / "Various Artists - Rhythm Is A Dancer.jpg")
    hit = probe.find_sidecar_art(tmp_path)
    assert hit.name == "Various Artists - Rhythm Is A Dancer.jpg"


def test_ambiguous_untitled_images_are_not_guessed(tmp_path):
    """Two or more images with no generic name is a real ambiguity, not a
    single-image case — must not guess."""
    _touch(tmp_path / "01 Track.flac",
          tmp_path / "scan1.jpg", tmp_path / "scan2.jpg")
    assert probe.find_sidecar_art(tmp_path) is None


def test_no_image_at_all_returns_none(tmp_path):
    _touch(tmp_path / "01 Track.flac")
    assert probe.find_sidecar_art(tmp_path) is None


def test_falls_through_to_art_named_subdirectory_one_level_down(tmp_path):
    """The exact real case: no art at the album root, but a Cover/
    subfolder with a generically-named front cover among several other
    images (back, full, per-disc scans)."""
    _touch(tmp_path / "01 Track.flac")
    _touch(tmp_path / "Cover" / "Back.jpg", tmp_path / "Cover" / "CD1.jpg",
          tmp_path / "Cover" / "Front.jpg", tmp_path / "Cover" / "Full.jpg")
    hit = probe.find_sidecar_art(tmp_path)
    assert hit is not None
    assert hit.parent.name == "Cover"
    assert hit.name == "Front.jpg"


def test_art_subdirectory_match_is_case_insensitive(tmp_path):
    _touch(tmp_path / "01 Track.flac")
    _touch(tmp_path / "ARTWORK" / "folder.png")
    hit = probe.find_sidecar_art(tmp_path)
    assert hit is not None and hit.name == "folder.png"


def test_does_not_descend_past_one_level(tmp_path):
    """A cover buried two levels down (Cover/Scans/front.jpg) must not be
    found — the depth cap is deliberate, not an oversight."""
    _touch(tmp_path / "01 Track.flac")
    _touch(tmp_path / "Cover" / "Scans" / "front.jpg")
    assert probe.find_sidecar_art(tmp_path) is None


def test_non_art_subdirectory_is_not_descended_into(tmp_path):
    """A subfolder with an unrelated name (e.g. bonus tracks) must not be
    raided for "the only image in there" — only known art-folder names."""
    _touch(tmp_path / "01 Track.flac")
    _touch(tmp_path / "Bonus Disc" / "some-photo.jpg")
    assert probe.find_sidecar_art(tmp_path) is None


def test_same_directory_takes_priority_over_subdirectory(tmp_path):
    _touch(tmp_path / "cover.jpg")
    _touch(tmp_path / "Cover" / "Front.jpg")
    hit = probe.find_sidecar_art(tmp_path)
    assert hit.parent == tmp_path
    assert hit.name == "cover.jpg"
