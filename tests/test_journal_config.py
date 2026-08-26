"""Tests for journal (resume guard, post-apply snapshot), review queue,
config loading and util. Pure python, runnable without mutagen."""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tagfill import config
from tagfill.journal import Journal, Record, ReviewQueue
from tagfill.util import (
    RateLimiter,
    is_appledouble,
    iter_audio,
    norm,
    similarity,
)


def test_journal_roundtrip_and_resume_guard():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "coll"
        root.mkdir()
        f = root / "a.mp3"
        f.write_bytes(b"x" * 100)

        j = Journal(Path(td) / "work")
        assert not j.already_done("s", root, f)
        j.record_write("s", root, f, "artist", None, "X")
        # Fresh Journal instance re-reads the file: post-apply snapshot must
        # match the current state, so the guard fires.
        j2 = Journal(Path(td) / "work")
        assert j2.already_done("s", root, f)
        # Any modification defeats the guard.
        f.write_bytes(b"y" * 101)
        j3 = Journal(Path(td) / "work")
        assert not j3.already_done("s", root, f)


def test_dry_run_records_have_no_snapshot():
    with tempfile.TemporaryDirectory() as td:
        j = Journal(Path(td))
        j.append(Record(stage="s", path="a.mp3", action="propose",
                        field="artist", new="X"))
        j2 = Journal(Path(td))
        assert (("s", "a.mp3") not in j2._load_applied()), \
            "a propose must never satisfy the resume guard"


def test_review_queue_accept_parsing():
    with tempfile.TemporaryDirectory() as td:
        rq = ReviewQueue(Path(td))
        rq.add({"path": "a.mp3", "stage": "filename",
                "proposed_artist": "A", "proposed_title": "T",
                "confidence": 0.4})
        rq.add({"path": "b.mp3", "stage": "filename",
                "proposed_artist": "B", "proposed_title": "U",
                "confidence": 0.3})
        text = rq.path.read_text().replace(
            "a.mp3,filename,A,T,,0.4,,", "a.mp3,filename,A,T,,0.4,,y")
        rq.path.write_text(text)
        accepted = ReviewQueue.load_accepted(rq.path)
        assert [r["path"] for r in accepted] == ["a.mp3"]


def test_config_defaults_and_example_parse():
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "tagfill.toml"
        p.write_text(config.EXAMPLE)
        cfg = config.load(p)
        assert cfg.art_min_px == 300
        assert cfg.mb_rate_s == 1.0
        assert cfg.itunes_art_sizes == [1200, 600]
        assert cfg.crate_globs == []
    # No config at all still yields workable defaults.
    cfg = config.Config()
    assert cfg.filename_confidence == 0.70


def test_default_workdir_is_per_user_state_not_the_cwd(monkeypatch):
    """The workdir holds backup/tags.jsonl (the undo path) and
    quarantine/wav (originals a conversion replaced), so it must land in a
    per-user state dir -- never the directory you happen to be standing in,
    and never a cache dir, which the XDG spec says may be deleted at any
    time without consequence."""
    monkeypatch.setattr(config.sys, "platform", "linux")
    monkeypatch.setenv("XDG_STATE_HOME", "/xdg/state")
    assert config.default_workdir() == Path("/xdg/state/tagfill")

    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    wd = config.default_workdir()
    assert wd.is_absolute()
    assert wd.parts[-3:] == (".local", "state", "tagfill")


def test_default_workdir_per_platform(monkeypatch):
    monkeypatch.setattr(config.sys, "platform", "win32")
    monkeypatch.setenv("LOCALAPPDATA", r"C:\Users\x\AppData\Local")
    assert config.default_workdir().name == "tagfill"
    assert "AppData" in str(config.default_workdir())

    monkeypatch.setattr(config.sys, "platform", "darwin")
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    assert config.default_workdir().parts[-3:] == (
        "Library", "Application Support", "tagfill")


def test_iter_audio_excludes():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "a.mp3").write_bytes(b"x")
        (root / "._a.mp3").write_bytes(b"\x00" * 4096)   # AppleDouble
        (root / ".hidden").mkdir()
        (root / ".hidden" / "b.mp3").write_bytes(b"x")
        (root / "work").mkdir()
        (root / "work" / "c.mp3").write_bytes(b"x")
        (root / "skipme").mkdir()
        (root / "skipme" / "d.mp3").write_bytes(b"x")
        got = [p.name for p in iter_audio(root, excludes=["skipme/*"],
                                          workdir=root / "work")]
        assert got == ["a.mp3"], got
    assert is_appledouble(Path("._x.wav"))


def test_similarity_and_norm():
    assert norm("Hello,   WORLD!") == "hello world"
    assert similarity("Quietus Korero", "quietus korero") == 1.0
    assert similarity("abc", "xyz") < 0.4


def test_rate_limiter_monotonic():
    import time
    rl = RateLimiter(0.05)
    t0 = time.monotonic()
    rl.wait()
    rl.wait()
    # Windows' timer granularity is ~15ms, so allow a little slack.
    assert time.monotonic() - t0 >= 0.04

def test_config_globs_match_regardless_of_separator():
    """Globs are written with forward slashes and matched on as_posix(),
    so the result does not depend on the host.

    An earlier version asserted that matching str(rel) *fails* on Windows.
    That was wrong: fnmatch runs both sides through os.path.normcase,
    which on Windows maps / to a backslash, so the naive match would have
    worked there. The assertion only looked right because it ran on Linux,
    where posixpath.normcase is the identity."""
    import fnmatch
    from pathlib import PureWindowsPath

    from tagfill.stages.filename import crate_grouping

    rel = PureWindowsPath("DJ Pool/Crate A/track.mp3")
    assert fnmatch.fnmatch(rel.as_posix(), "DJ Pool/*")
    assert crate_grouping(Path("DJ Pool/Crate A/track.mp3"),
                          ["DJ Pool/*"]) == "Crate A"


def test_output_encoding_survives_an_unprintable_filename():
    """On Windows, redirected output encodes with the legacy ANSI code page,
    and cp1252 cannot represent Japanese, Cyrillic, Devanagari or emoji --
    printing such a path raised UnicodeEncodeError and killed the run."""
    import io

    from tagfill.cli import _make_output_encoding_safe

    sample = "坂本龍一 - Merry Christmas Mr. Lawrence.flac"
    legacy = io.TextIOWrapper(io.BytesIO(), encoding="cp1252", errors="strict")
    try:
        print(sample, file=legacy)
        raise AssertionError("precondition: cp1252 was supposed to refuse this")
    except UnicodeEncodeError:
        pass

    real_out, real_err = sys.stdout, sys.stderr
    try:
        sys.stdout = io.TextIOWrapper(io.BytesIO(), encoding="cp1252",
                                      errors="strict")
        sys.stderr = io.TextIOWrapper(io.BytesIO(), encoding="cp1252",
                                      errors="strict")
        _make_output_encoding_safe()
        print(sample)                      # must not raise
        assert sys.stdout.encoding.lower().replace("-", "") == "utf8"
    finally:
        sys.stdout, sys.stderr = real_out, real_err

def test_recheck_is_honoured_by_every_stage_that_uses_the_resume_guard():
    """--recheck lives on the shared parser, so every stage subcommand
    accepts it and its help promises a general escape hatch from the
    resume guard. A stage that calls already_done() without consulting
    ctx.recheck silently ignores the flag it advertises."""
    import re
    from pathlib import Path as P
    stages = P(__file__).resolve().parents[1] / "tagfill" / "stages"
    offenders = []
    for f in sorted(stages.glob("*.py")):
        text = f.read_text()
        for m in re.finditer(r"already_done\(", text):
            window = text[max(0, m.start() - 200):m.start()]
            if "ctx.recheck" not in window:
                line = text[:m.start()].count("\n") + 1
                offenders.append(f"{f.name}:{line}")
    assert not offenders, (
        "these call already_done() without honouring --recheck: "
        + ", ".join(offenders))

if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"ok {name}")
