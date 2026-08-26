"""Exhaustive probe round-trip: every container in probe._CONTAINERS
against every field in probe.FIELDS, plus art.

This replaces a scatter of hand-picked (container, field) pairs. The space
is small and fully enumerable -- twelve suffixes over nine handlers, nine
fields -- so covering all of it beats sampling it, and a container added to
_CONTAINERS is covered the moment it appears there rather than when someone
remembers to write tests for it.

The invariant is deliberately not "every value survives every container",
because that is false: ID3's TDRC is a timestamp frame, so a non-numeric
date is dropped on save while the same string round-trips in a FLAC
comment. What must hold is that probe.write() never claims a change that
did not happen -- the journal, the resume guard and the report all trust
its return value.
"""

import shutil
import struct
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
from make_fixtures import opus_args, vorbis_args

from tagfill import probe

needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None,
                                  reason="needs ffmpeg")


def _ffmpeg(*codec_args):
    def build(path):
        args = list(codec_args[0]() if callable(codec_args[0]) else codec_args)
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi",
                        "-i", "sine=frequency=440:duration=1",
                        *args, str(path)], check=True)
    return build


def _build_dsf(path, channels=2, rate=2822400, block=4096):
    """ffmpeg decodes DSD but cannot encode it, so a DSD fixture has to be
    written by hand. A DSF file is three chunks: a DSD header carrying the
    total size and a pointer to the metadata, a fmt descriptor, and the
    1-bit samples. Verified against the real 146MB DSD64 sample this was
    modelled on -- mutagen reads both the same way."""
    data = b"\x69" * (block * channels)
    fmt = struct.pack("<4sQIIIIIIQII", b"fmt ", 52, 1, 0, 2, channels,
                      rate, 1, block * 8, block, 0)
    chunk = struct.pack("<4sQ", b"data", 12 + len(data)) + data
    total = 28 + len(fmt) + len(chunk)
    header = struct.pack("<4sQQQ", b"DSD ", 28, total, 0)   # 0 = no metadata
    path.write_bytes(header + fmt + chunk)


def _build_dff(path, channels=2, rate=2822400, frames=4096):
    """DSDIFF, DSD's other container: big-endian IFF chunks inside FRM8."""
    def chunk(cid, payload):
        pad = b"\x00" if len(payload) % 2 else b""
        return cid + struct.pack(">Q", len(payload)) + payload + pad

    body = (b"DSD "
            + chunk(b"FVER", struct.pack(">I", 0x01050000))
            + chunk(b"PROP", b"SND "
                    + chunk(b"FS  ", struct.pack(">I", rate))
                    + chunk(b"CHNL", struct.pack(">H", channels) + b"SLFTSRGT"))
            + chunk(b"DSD ", b"\x69" * (frames * channels // 8)))
    path.write_bytes(b"FRM8" + struct.pack(">Q", len(body)) + body)


# suffix -> how to produce one
_BUILDERS = {
    ".mp3": _ffmpeg("-c:a", "libmp3lame"), ".flac": _ffmpeg("-c:a", "flac"),
    # Which vorbis and opus encoder exists depends on the ffmpeg build; see
    # make_fixtures for why.
    ".ogg": _ffmpeg(vorbis_args), ".opus": _ffmpeg(opus_args),
    ".m4a": _ffmpeg("-c:a", "aac"), ".mp4": _ffmpeg("-c:a", "aac"),
    ".aiff": _ffmpeg("-c:a", "pcm_s16be"), ".aif": _ffmpeg("-c:a", "pcm_s16be"),
    ".aifc": _ffmpeg("-c:a", "pcm_s16be"), ".wav": _ffmpeg("-c:a", "pcm_s16le"),
    ".dsf": _build_dsf, ".dff": _build_dff,
}


def _one_suffix_per_handler() -> list[str]:
    """Twelve suffixes, nine code paths: .aif/.aifc are the same AIFF
    handler as .aiff, and .mp4 is the same MP4 handler as .m4a. Running the
    whole
    field matrix against an alias re-executes identical code for no new
    coverage, so parametrise over handlers and check the aliasing itself
    once, separately."""
    seen, keep = set(), []
    for suffix in sorted(probe._CONTAINERS):
        handler = probe._CONTAINERS[suffix]
        if handler not in seen:
            seen.add(handler)
            keep.append(suffix)
    return keep


CONTAINERS = _one_suffix_per_handler()


def test_every_known_container_is_covered():
    """If someone adds a container to probe._CONTAINERS, this file must
    learn how to build one, or the new format ships untested."""
    assert set(probe._CONTAINERS) == set(_BUILDERS), (
        "probe._CONTAINERS and this file's _BUILDERS have diverged")


def test_audio_suffixes_match_the_container_map():
    """util.AUDIO_SUFFIXES decides what the scanner picks up and
    probe._CONTAINERS decides what can be tagged. They are separate because
    util.py imports nothing third-party, so they can drift: a format in one
    but not the other is either scanned and then unreadable, or tagged but
    never found."""
    from tagfill.util import AUDIO_SUFFIXES
    assert set(probe._CONTAINERS) == AUDIO_SUFFIXES


def test_aliases_share_one_handler():
    """What the deduplication above assumes. If .aifc ever stops being the
    same handler as .aiff, the matrix must start covering it again."""
    handlers = {suffix: probe._CONTAINERS[suffix] for suffix in _BUILDERS}
    assert handlers[".aif"] == handlers[".aiff"] == handlers[".aifc"]
    assert handlers[".mp4"] == handlers[".m4a"]
    assert len(set(handlers.values())) == len(CONTAINERS)


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    """One pristine file per container, built once for the whole module."""
    if shutil.which("ffmpeg") is None:
        pytest.skip("needs ffmpeg")
    d = tmp_path_factory.mktemp("containers")
    out = {}
    for suffix, build in _BUILDERS.items():
        p = d / f"sample{suffix}"
        build(p)
        out[suffix] = p
    return out


def _fresh(built, suffix, tmp_path):
    dst = tmp_path / f"work{suffix}"
    shutil.copy2(built[suffix], dst)
    return dst


def _value_for(field: str) -> str:
    if field == "tracknumber":
        return "7"
    if field == "date":
        return "1994-03-28"     # every container stores a real date
    return f"value for {field}"


@needs_ffmpeg
@pytest.mark.parametrize("suffix", CONTAINERS)
@pytest.mark.parametrize("field", probe.FIELDS)
def test_field_roundtrips(built, tmp_path, suffix, field):
    p = _fresh(built, suffix, tmp_path)
    value = _value_for(field)

    changed = probe.write(p, {field: value})
    assert [c[0] for c in changed] == [field]
    assert probe.read(p).get(field) == value


@needs_ffmpeg
@pytest.mark.parametrize("suffix", CONTAINERS)
def test_write_is_idempotent_and_refuses_to_clobber(built, tmp_path, suffix):
    p = _fresh(built, suffix, tmp_path)
    probe.write(p, {"grouping": "First"})

    assert probe.write(p, {"grouping": "First"}) == [], "re-write is a no-op"
    assert probe.write(p, {"grouping": "Second"}) == [], "must not clobber"
    assert probe.read(p).get("grouping") == "First"

    assert probe.write(p, {"grouping": "Second"}, overwrite=True) != []
    assert probe.read(p).get("grouping") == "Second"


@needs_ffmpeg
@pytest.mark.parametrize("suffix", CONTAINERS)
def test_write_never_claims_a_change_that_did_not_land(built, tmp_path,
                                                       suffix):
    """A value the container cannot represent must be reported as not
    written. ID3 silently drops a non-numeric date on save; saying it was
    written would put a lie in the journal."""
    p = _fresh(built, suffix, tmp_path)
    changed = probe.write(p, {"date": "sometime in the nineties"})
    stored = probe.read(p).get("date")
    if changed:
        assert stored, "reported as changed, so something must be stored"
    else:
        assert not stored, "reported as unchanged, so nothing may be stored"


@needs_ffmpeg
@pytest.mark.parametrize("suffix", CONTAINERS)
def test_a_write_that_destroys_an_existing_value_is_reported(built, tmp_path,
                                                             suffix):
    """The arm the pristine-file test above cannot reach. With
    overwrite=True, a value the container cannot store does not merely fail
    to land -- ID3 delall()s the frame first, so the good value that was
    there is destroyed. Reporting nothing would hide a real loss from the
    journal, the report and the resume guard alike."""
    p = _fresh(built, suffix, tmp_path)
    probe.write(p, {"date": "1994"})
    assert probe.read(p).get("date") == "1994"

    changed = probe.write(p, {"date": "sometime in the nineties"},
                          overwrite=True)
    stored = probe.read(p).get("date") or ""
    if stored.strip() == "1994":
        assert changed == [], "nothing changed, so nothing to report"
    else:
        assert changed, f"{suffix}: the old value was lost and not reported"
        assert changed[0][1] == "1994", "must report what was lost"
        assert (changed[0][2] or "").strip() == stored.strip(), (
            "must report what the file now holds")


@needs_ffmpeg
@pytest.mark.parametrize("suffix", CONTAINERS)
def test_write_reports_the_stored_value_not_the_requested_one(built, tmp_path,
                                                              suffix):
    """A container may normalise rather than reject: ID3 stores
    "1994-03-28T00:00:00" back as "1994-03-28 00:00:00". Journalling the
    request would record a value the file does not contain, and would leave
    old != new forever, so --overwrite rewrote the same file every run."""
    p = _fresh(built, suffix, tmp_path)
    changed = probe.write(p, {"date": "1994-03-28T00:00:00"})
    stored = probe.read(p).get("date")
    if changed:
        assert changed[0][2] == stored, f"{suffix}: reported != stored"
    # And a second identical write must now be a no-op.
    assert probe.write(p, {"date": "1994-03-28T00:00:00"},
                       overwrite=True) == [], "normalised value must settle"


@needs_ffmpeg
@pytest.mark.skipif(sys.platform == "win32", reason="fcntl is POSIX-only")
def test_fsync_uses_a_writable_handle(tmp_path, monkeypatch):
    """os.fsync on Windows is _commit(), which needs a handle opened for
    writing; a read-only one raises. That failure would land after the temp
    is written and before os.replace, so every tag write would fail on
    Windows while passing here. Linux accepts fsync on a read-only fd, so
    the only way to catch it from Linux is to inspect the descriptor."""
    import fcntl
    import os as _os
    seen = []
    real = _os.fsync

    def spy(fd):
        seen.append(fcntl.fcntl(fd, fcntl.F_GETFL) & _os.O_ACCMODE)
        return real(fd)

    monkeypatch.setattr(probe.os, "fsync", spy)
    p = tmp_path / "t.mp3"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi",
                    "-i", "sine=frequency=440:duration=1",
                    "-c:a", "libmp3lame", "-b:a", "64k", str(p)], check=True)
    probe.write(p, {"artist": "X"})

    assert seen, "fsync was never called"
    assert _os.O_RDONLY not in seen, (
        "fsync got a read-only descriptor; this fails on Windows")


@needs_ffmpeg
@pytest.mark.parametrize("suffix", CONTAINERS)
def test_art_roundtrips_byte_for_byte(built, tmp_path, suffix):
    import io

    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (400, 400), (12, 34, 56)).save(buf, "JPEG", quality=90)
    art = buf.getvalue()

    p = _fresh(built, suffix, tmp_path)
    assert not probe.read(p).has_art
    probe.embed_art(p, art, "image/jpeg")

    got = probe.read_art(p)
    assert got is not None and got[0] == art
    assert probe.read(p).has_art

    probe.remove_art(p)
    assert probe.read_art(p) is None
    assert not probe.read(p).has_art


@needs_ffmpeg
@pytest.mark.parametrize("suffix", CONTAINERS)
def test_delete_fields_clears_what_restore_needs_cleared(built, tmp_path,
                                                         suffix):
    p = _fresh(built, suffix, tmp_path)
    probe.write(p, {"artist": "A", "album": "B"})
    probe.delete_fields(p, ["artist"])

    st = probe.read(p)
    assert st.missing("artist")
    assert st.get("album") == "B", "only the named field may be removed"
