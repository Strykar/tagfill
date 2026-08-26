"""Every text handle names its encoding, and a non-ASCII collection
survives a full round trip.

External review: not one text-mode open() in the persistence layer passed
encoding=, so they all took the platform default -- which on Windows
CPython is the ANSI code page, not UTF-8 (PEP 686 changes that in 3.15).
journal.py writes json.dumps(ensure_ascii=False) on every decision, so the
first path or tag outside the local code page raises UnicodeEncodeError
mid-stage on a collection that works perfectly on Linux. Reads mojibake
the same way in reverse.

The Windows CI job could not see it: there was not one non-ASCII character
anywhere in the suite. ("Phoenix" in the demo corpus survives only because
the oe ligature happens to sit at 0x9C in cp1252.)

Two tests, because either alone leaves the hole open: a drift guard so a
new open() cannot land without an encoding, and a real round trip through
census -> journal -> report in Devanagari and Japanese.
"""

import csv
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

import tagfill
from tagfill import config, probe
from tagfill.journal import Journal, Record, ReviewQueue
from tagfill.stages import Context, census, report

PKG = Path(tagfill.__file__).parent

# open(...) up to the first close paren, which is enough to see the mode
# and the kwargs. Binary modes are exempt: bytes have no encoding.
_OPEN = re.compile(r"\bopen\(([^)]*)\)", re.S)
_BINARY = re.compile(r'"[rwax]\+?b"')


def test_no_text_open_relies_on_the_platform_default():
    offenders = []
    for src in sorted(PKG.rglob("*.py")):
        text = src.read_text(encoding="utf-8")
        for m in _OPEN.finditer(text):
            args = m.group(1)
            if args.startswith(("io.BytesIO", "io.StringIO")):
                continue
            if _BINARY.search(args) or "encoding=" in args:
                continue
            line = text[:m.start()].count("\n") + 1
            # Image.open and probe._open are not builtins.
            if re.search(r"(Image|probe)\.open\($",
                         text[:m.end()].rsplit("open(", 1)[0] + "open("):
                continue
            offenders.append(f"{src.relative_to(PKG.parent)}:{line}")
    assert not offenders, (
        "text-mode open() without encoding=, which is the ANSI code page on "
        "Windows: " + ", ".join(offenders))


NON_ASCII = [
    ("हिन्दी गाने", "लता मंगेशकर", "आजा रे"),
    ("日本の音楽", "坂本龍一", "戦場のメリークリスマス"),
    ("Русский рок", "Кино", "Группа крови"),
]

needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None,
                                  reason="needs ffmpeg")


def _ctx(tmp_path):
    cfg = config.Config()
    cfg.root, cfg.workdir = tmp_path / "Music", tmp_path / "work"
    cfg.root.mkdir(parents=True)
    return Context(cfg=cfg, journal=Journal(cfg.workdir),
                   review=ReviewQueue(cfg.workdir))


def _real_mp3(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi",
                    "-i", "sine=frequency=440:duration=1",
                    "-c:a", "libmp3lame", "-b:a", "64k", str(path)],
                   check=True)


@needs_ffmpeg
@pytest.mark.parametrize("album,artist,title", NON_ASCII)
def test_a_non_ascii_collection_round_trips(tmp_path, album, artist, title):
    """The whole chain on one file whose name and tags are outside ASCII:
    probe write, census write, census read, journal write, journal read,
    report CSV write."""
    ctx = _ctx(tmp_path)
    rel = f"{artist} - {album}/01 {title}.mp3"
    track = ctx.root / rel
    _real_mp3(track)
    probe.write(track, {"artist": artist, "album": album, "title": title})

    census.run(ctx)
    ctx.journal.append(Record(stage="mb", path=rel, action="reject",
                              field="release",
                              evidence={"album": album, "artist": artist}))
    report.run(ctx)

    row = next(r for r in census.load(ctx) if r["path"] == rel)
    assert (row["artist"], row["album"], row["title"]) == (artist, album, title)

    raw = (ctx.workdir / "journal.jsonl").read_text(encoding="utf-8")
    logged = [json.loads(x) for x in raw.splitlines()]
    assert any(r["evidence"].get("album") == album
               for r in logged if r.get("evidence"))

    with open(ctx.workdir / "report" / "unresolved.csv", newline="",
              encoding="utf-8") as f:
        assert any(r["path"] == rel for r in csv.DictReader(f))


@pytest.mark.parametrize("album,artist,title", NON_ASCII)
def test_the_review_queue_survives_non_ascii(tmp_path, album, artist, title):
    """The review queue is the one file a human opens in a spreadsheet, so
    it is the one most likely to be blamed on the wrong thing when the
    encoding is wrong."""
    ctx = _ctx(tmp_path)
    rel = f"{artist} - {album}/01 {title}.mp3"
    ctx.review.add({"path": rel, "stage": "filename",
                    "proposed_artist": artist, "proposed_title": title,
                    "confidence": "0.5", "reason": "low confidence"})

    with open(ctx.review.path, newline="", encoding="utf-8") as f:
        stored = list(csv.DictReader(f))
    assert stored[0]["proposed_artist"] == artist
    assert stored[0]["proposed_title"] == title
    assert stored[0]["path"] == rel

    # And the read-back path the CLI actually uses.
    accepted_src = ctx.review.path.read_text(encoding="utf-8").replace(
        ",low confidence,", ",low confidence,y")
    ctx.review.path.write_text(accepted_src, encoding="utf-8")
    assert ReviewQueue.load_accepted(ctx.review.path)[0]["path"] == rel


def test_the_example_config_is_written_as_utf8(tmp_path, monkeypatch):
    """ASCII today, and one commented example away from not being."""
    from tagfill import cli
    monkeypatch.chdir(tmp_path)
    assert cli.main(["init"]) == 0
    assert (tmp_path / "tagfill.toml").read_text(encoding="utf-8") \
        == config.EXAMPLE
