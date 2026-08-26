"""probe.py against the synthesized fixtures: files tagged by something
other than tagfill.

Round-tripping our own writes lives in test_probe_roundtrip.py, which
covers every container against every field. What is left here is the case
that test cannot reach -- reading tags a *different* tool wrote, which is
where the easy=True bug hid -- plus the backup/restore round trip.
"""

import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

try:
    import mutagen  # noqa: F401
    HAVE_MUTAGEN = True
except ImportError:
    HAVE_MUTAGEN = False
HAVE_FFMPEG = shutil.which("ffmpeg") is not None

try:
    import pytest
    needs_env = pytest.mark.skipif(
        not (HAVE_MUTAGEN and HAVE_FFMPEG),
        reason="needs mutagen + ffmpeg")
except ImportError:
    def needs_env(fn):
        return fn

FIXTURES = Path(__file__).parent / "fixtures"


def _ensure_fixtures():
    if not FIXTURES.exists():
        from make_fixtures import build
        build(FIXTURES)


@needs_env
def test_aiff_regression_finding_1():
    """The fully tagged AIFF must read as fully tagged. This is the exact
    failure `easy=True` produced: TPE1 present, `tags.get('artist')` empty."""
    _ensure_fixtures()
    from tagfill import probe
    st = probe.read(FIXTURES / "Singles" / "Aiff Artist - Fully Tagged.aiff")
    assert st.get("artist") == "Aiff Artist"
    assert st.get("title") == "Fully Tagged"
    assert st.get("album") == "Aiff Album"
    assert st.has_art


@needs_env
def test_read_each_container():
    _ensure_fixtures()
    from tagfill import probe
    mp3 = probe.read(FIXTURES / "Tagged Album" / "01 Opening.mp3")
    assert mp3.get("artist") == "Fixture Artist" and not mp3.has_art
    flac = probe.read(FIXTURES / "Partial Album" / "01 First.flac")
    assert flac.get("album") == "Partial Album" and flac.has_art
    ogg = probe.read(FIXTURES / "Singles" / "Ogg Artist - No Art.ogg")
    assert ogg.get("artist") == "Ogg Artist" and not ogg.has_art
    untagged = probe.read(
        FIXTURES / "DJ Pool" / "Some Crate"
        / "Dysmorph - Quietus Korero [Fixture Recs].flac")
    assert untagged.missing("artist") and untagged.missing("title")




@needs_env
def test_backup_restore_roundtrip():
    _ensure_fixtures()
    from tagfill import probe
    from tagfill.backup import TagBackup, restore
    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "coll"
        (root / "d").mkdir(parents=True)
        dst = root / "d" / "x.mp3"
        shutil.copy2(FIXTURES / "Tagged Album" / "01 Opening.mp3", dst)
        before = probe.read(dst)

        work = Path(td) / "work"
        tb = TagBackup(work)
        tb.snapshot(root, dst)
        probe.write(dst, {"album": "Mutated"}, overwrite=True)
        probe.embed_art(dst, b"\xff\xd8\xff" + b"0" * 100, "image/jpeg")

        n = restore(root, work / "backup" / "tags.jsonl")
        assert n == 1
        after = probe.read(dst)
        assert after.fields == before.fields
        assert after.has_art == before.has_art


if __name__ == "__main__":
    if not (HAVE_MUTAGEN and HAVE_FFMPEG):
        print("skipped: needs mutagen + ffmpeg")
        sys.exit(0)
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"ok {name}")
