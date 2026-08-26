"""Tests for genre/tracknumber: the config-gated extra fields written
alongside date/albumartist/album.

MP4's "trkn" atom is not text like every other field mutagen handles here
-- it's a (track, total) integer-pair atom, so probe.py special-cases it
on both read and write. That's the riskiest new code path; the ID3 and
Vorbis fields reuse the existing generic string-tag machinery already
covered by test_probe.py.

Track number is only ever written when a source match's gate() evidence
proves positional order -- an unordered match confirms the album but not
which local file is which track, so mb.py must not write it in that case.
"""

import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

HAVE_FFMPEG = shutil.which("ffmpeg") is not None
needs_ffmpeg = pytest.mark.skipif(not HAVE_FFMPEG, reason="needs ffmpeg")

from tagfill import config, probe
from tagfill.journal import Journal, ReviewQueue
from tagfill.sources import SourceMatch
from tagfill.stages import Context, census
from tagfill.stages import mb as mb_stage


def _make_audio(path: Path, container: str):
    args = ["ffmpeg", "-v", "error", "-y", "-f", "lavfi",
           "-i", "sine=frequency=440:duration=1"]
    if container == "m4a":
        args += ["-c:a", "aac", "-b:a", "64k"]
    else:
        args += ["-c:a", "libmp3lame", "-b:a", "64k"]
    args.append(str(path))
    subprocess.run(args, check=True)



@needs_ffmpeg
def test_mp4_tracknumber_survives_as_integer_not_string(tmp_path):
    """trkn is a (track, total) integer-pair atom -- writing "7" and
    reading it back as the string "7" only proves the special-case
    round-trips; this also checks the underlying atom is the tuple form,
    not a text frame someone could break by "simplifying" it later."""
    path = tmp_path / "track.m4a"
    _make_audio(path, "m4a")
    probe.write(path, {"tracknumber": "7"})

    from mutagen.mp4 import MP4
    audio = MP4(path)
    assert audio.tags["trkn"] == [(7, 0)]


def _ctx(tmp_path, root, extra_tags):
    cfg = config.Config()
    cfg.root = root
    cfg.workdir = tmp_path / "work"
    cfg.mb_contact = "test@example.org"
    cfg.extra_tags = extra_tags
    return Context(cfg=cfg, journal=Journal(cfg.workdir),
                  review=ReviewQueue(cfg.workdir), apply=True)


def _write_census(ctx, paths):
    ctx.workdir.mkdir(parents=True, exist_ok=True)
    with open(ctx.workdir / "census.csv", "w", newline="") as f:
        import csv
        w = csv.DictWriter(f, fieldnames=census.COLUMNS)
        w.writeheader()
        for p, duration in paths:
            full = dict.fromkeys(census.COLUMNS, "")
            full.update(path=p, container=Path(p).suffix.lstrip("."),
                       size=1, mtime="0", duration=str(duration),
                       artist="Some Artist", album="Some Album")
            w.writerow(full)


def _fake_match(order):
    return SourceMatch(id="mb:x", title="Some Album", date="2020",
                       albumartist="Some Artist", genre="Rock",
                       evidence={"order": order})


@needs_ffmpeg
def test_positional_match_writes_tracknumber(tmp_path, monkeypatch):
    root = tmp_path / "music"
    (root / "Some Album").mkdir(parents=True)
    _make_audio(root / "Some Album" / "01.mp3", "mp3")
    _make_audio(root / "Some Album" / "02.mp3", "mp3")
    ctx = _ctx(tmp_path, root, ["genre", "tracknumber"])
    _write_census(ctx, [("Some Album/01.mp3", 1.0),
                        ("Some Album/02.mp3", 1.0)])

    monkeypatch.setattr(mb_stage.musicbrainz, "search",
                        lambda **k: (_fake_match("positional"), []))
    monkeypatch.setattr(mb_stage.itunes, "search", lambda **k: (None, []))
    monkeypatch.setattr(mb_stage.discogs, "search", lambda **k: (None, []))
    mb_stage.run(ctx)

    st1 = probe.read(root / "Some Album" / "01.mp3")
    st2 = probe.read(root / "Some Album" / "02.mp3")
    assert st1.get("tracknumber") == "1"
    assert st2.get("tracknumber") == "2"
    assert st1.get("genre") == "Rock"


@needs_ffmpeg
def test_unordered_match_does_not_write_tracknumber(tmp_path, monkeypatch):
    root = tmp_path / "music"
    (root / "Some Album").mkdir(parents=True)
    _make_audio(root / "Some Album" / "01.mp3", "mp3")
    ctx = _ctx(tmp_path, root, ["genre", "tracknumber"])
    _write_census(ctx, [("Some Album/01.mp3", 1.0)])

    monkeypatch.setattr(mb_stage.musicbrainz, "search",
                        lambda **k: (_fake_match("unordered"), []))
    monkeypatch.setattr(mb_stage.itunes, "search", lambda **k: (None, []))
    monkeypatch.setattr(mb_stage.discogs, "search", lambda **k: (None, []))
    mb_stage.run(ctx)

    st = probe.read(root / "Some Album" / "01.mp3")
    assert st.get("tracknumber") is None, (
        "an unordered match proves the album but not which local file is "
        "which track -- writing a track number here would be a guess")
    assert st.get("genre") == "Rock"


@needs_ffmpeg
def test_empty_extra_tags_disables_both_fields(tmp_path, monkeypatch):
    root = tmp_path / "music"
    (root / "Some Album").mkdir(parents=True)
    _make_audio(root / "Some Album" / "01.mp3", "mp3")
    ctx = _ctx(tmp_path, root, [])
    _write_census(ctx, [("Some Album/01.mp3", 1.0)])

    monkeypatch.setattr(mb_stage.musicbrainz, "search",
                        lambda **k: (_fake_match("positional"), []))
    monkeypatch.setattr(mb_stage.itunes, "search", lambda **k: (None, []))
    monkeypatch.setattr(mb_stage.discogs, "search", lambda **k: (None, []))
    mb_stage.run(ctx)

    st = probe.read(root / "Some Album" / "01.mp3")
    assert st.get("genre") is None
    assert st.get("tracknumber") is None
    assert st.get("album") == "Some Album"  # unrelated fields still write


@needs_ffmpeg
def test_a_blank_field_is_filled_from_a_later_verified_source(tmp_path,
                                                              monkeypatch):
    """First-match-wins made the winner's gaps permanent. Found live on a
    5-CD "Rhythm Is A Dancer" set: MusicBrainz matched all five discs and
    carried no genre tag on any of them, so 100 files kept an empty genre
    while iTunes had one available. The identifying source still owns
    identity -- only blanks get filled, and only from a source that
    cleared the duration gate on this same folder itself."""
    root = tmp_path / "music"
    (root / "Album").mkdir(parents=True)
    _make_audio(root / "Album" / "01.mp3", "mp3")
    ctx = _ctx(tmp_path, root, ["genre", "tracknumber"])
    _write_census(ctx, [("Album/01.mp3", 1.0)])

    winner = SourceMatch(id="mb:x", title="Some Album", date="",
                         albumartist="", genre="",      # every gap open
                         evidence={"order": "positional", "source": "mb"})
    filler = SourceMatch(id="itunes:9", title="Some Album", date="1994",
                         albumartist="Some Artist", genre="Electronic",
                         evidence={"source": "itunes"})

    monkeypatch.setattr(mb_stage.musicbrainz, "search",
                        lambda **k: (winner, []))
    monkeypatch.setattr(mb_stage.itunes, "search", lambda **k: (filler, []))
    monkeypatch.setattr(mb_stage.discogs, "search", lambda **k: (None, []))
    mb_stage.run(ctx)

    st = probe.read(root / "Album" / "01.mp3")
    assert st.get("genre") == "Electronic", "blank genre must be filled"
    assert st.get("date") == "1994"
    assert st.get("albumartist") == "Some Artist"
    assert st.get("album") == "Some Album", "identity stays with the winner"
    assert winner.evidence["genre_from"] == "itunes", "provenance recorded"


@needs_ffmpeg
def test_a_later_source_never_overwrites_a_field_the_winner_had(
        tmp_path, monkeypatch):
    root = tmp_path / "music"
    (root / "Album").mkdir(parents=True)
    _make_audio(root / "Album" / "01.mp3", "mp3")
    ctx = _ctx(tmp_path, root, ["genre", "tracknumber"])
    _write_census(ctx, [("Album/01.mp3", 1.0)])

    winner = SourceMatch(id="mb:x", title="Real Album", date="1990",
                         albumartist="Real Artist", genre="Jazz",
                         evidence={"order": "positional", "source": "mb"})
    other = SourceMatch(id="itunes:9", title="Wrong Album", date="2020",
                        albumartist="Wrong Artist", genre="Polka",
                        evidence={"source": "itunes"})

    monkeypatch.setattr(mb_stage.musicbrainz, "search",
                        lambda **k: (winner, []))
    monkeypatch.setattr(mb_stage.itunes, "search", lambda **k: (other, []))
    monkeypatch.setattr(mb_stage.discogs, "search", lambda **k: (None, []))
    mb_stage.run(ctx)

    st = probe.read(root / "Album" / "01.mp3")
    assert st.get("genre") == "Jazz"
    assert st.get("date") == "1990"
    assert st.get("albumartist") == "Real Artist"
    assert st.get("album") == "Real Album"


@needs_ffmpeg
def test_mp4_track_total_survives_a_tracknumber_write(tmp_path):
    """trkn is (track, total). Writing a hardcoded 0 for the total
    destroyed it, and since read() only surfaces the track half,
    backup/restore could not put it back: (5, 12) came back (5, 0)."""
    from mutagen.mp4 import MP4
    path = tmp_path / "t.m4a"
    _make_audio(path, "m4a")
    a = MP4(path)
    if a.tags is None:
        a.add_tags()
    a.tags["trkn"] = [(5, 12)]
    a.save()

    probe.write(path, {"tracknumber": "9"}, overwrite=True)
    assert MP4(path).tags["trkn"] == [(9, 12)], "the total must be preserved"


def test_gate_reports_which_candidate_vector_matched():
    """mb.py needs it: musicbrainz appends a whole-release concatenation
    after the per-medium vectors, and a match on that one is positional
    across the release, not within the folder."""
    from tagfill.sources import gate
    per_medium_a = [10.0, 20.0]
    per_medium_b = [30.0, 40.0]
    concat = per_medium_a + per_medium_b
    ev = gate(concat, [per_medium_a, per_medium_b, concat], 1.0, 0.9)
    assert ev["vector"] == 2, "matched the concatenation, index 2"
    ev = gate(per_medium_b, [per_medium_a, per_medium_b, concat], 1.0, 0.9)
    assert ev["vector"] == 1


@needs_ffmpeg
def test_no_track_number_from_a_concatenated_multi_medium_match(
        tmp_path, monkeypatch):
    """A flattened 2-CD folder matching the concatenated vector would have
    had disc 2 track 1 written as tracknumber=13."""
    root = tmp_path / "music"
    (root / "Album").mkdir(parents=True)
    _make_audio(root / "Album" / "01.mp3", "mp3")
    ctx = _ctx(tmp_path, root, ["genre", "tracknumber"])
    _write_census(ctx, [("Album/01.mp3", 1.0)])

    m = SourceMatch(id="mb:x", title="Some Album", date="2020",
                    albumartist="Some Artist", genre="Rock",
                    evidence={"order": "positional", "multi_medium": True})
    monkeypatch.setattr(mb_stage.musicbrainz, "search", lambda **k: (m, []))
    monkeypatch.setattr(mb_stage.itunes, "search", lambda **k: (None, []))
    monkeypatch.setattr(mb_stage.discogs, "search", lambda **k: (None, []))
    mb_stage.run(ctx)

    st = probe.read(root / "Album" / "01.mp3")
    assert st.get("tracknumber") is None, (
        "position in a concatenated release is not position in this folder")
    assert st.get("genre") == "Rock", "everything else still writes"
