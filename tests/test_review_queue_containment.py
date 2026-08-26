"""External review: --from-review reads a CSV a human has been editing, and
accepted rows are written with overwrite=True. The path column went
straight into `ctx.root / row["path"]` with no containment check, so
`../../etc/something.mp3` in that column reached outside the collection --
and "nothing outside your library is ever touched" is the promise the
README leads with.

Not a hostile-input story so much as a copy-paste-a-path-in-a-spreadsheet
one, but the check is two lines either way.
"""

import csv
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

pytest.importorskip("mutagen")

from tagfill import config, probe
from tagfill.journal import Journal, ReviewQueue
from tagfill.stages import Context, filename


def _setup(tmp_path, rel_path):
    root = tmp_path / "Music"
    (root / "Album").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()

    cfg = config.Config()
    cfg.root, cfg.workdir = root, tmp_path / "work"
    queue = tmp_path / "review.csv"
    with open(queue, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=ReviewQueue.FIELDS)
        w.writeheader()
        w.writerow({"path": rel_path, "stage": "filename",
                    "proposed_artist": "Intruder", "proposed_title": "Oops",
                    "proposed_label": "", "confidence": "0.9",
                    "reason": "", "accept": "y"})
    ctx = Context(cfg=cfg, journal=Journal(cfg.workdir),
                  review=ReviewQueue(cfg.workdir), apply=True,
                  from_review=queue)
    return ctx, outside


def _mp3(path, **tags):
    import subprocess
    subprocess.run(["ffmpeg", "-y", "-loglevel", "quiet", "-f", "lavfi",
                    "-i", "anullsrc=r=8000:cl=mono", "-t", "1", str(path)],
                   check=True)
    if tags:
        probe.write(path, tags)


def _skips(ctx):
    with open(ctx.journal.path, encoding="utf-8") as f:
        return [json.loads(line) for line in f
                if json.loads(line)["action"] == "skip"]


def test_a_row_pointing_outside_the_collection_is_refused(tmp_path):
    victim = tmp_path / "outside" / "victim.mp3"
    ctx, _ = _setup(tmp_path, "../outside/victim.mp3")
    _mp3(victim, artist="Original", title="Untouched")

    filename.run(ctx)

    after = probe.read(victim)
    assert after.fields["artist"] == "Original"
    assert after.fields["title"] == "Untouched"
    assert any("outside the collection" in r["evidence"]["reason"]
               for r in _skips(ctx))


def test_an_absolute_path_outside_the_collection_is_refused(tmp_path):
    """`ctx.root / "/etc/x"` is "/etc/x" -- pathlib drops the left side for
    an absolute right side, so this reaches further than the dots do."""
    victim = tmp_path / "outside" / "victim.mp3"
    ctx, _ = _setup(tmp_path, str(victim))
    _mp3(victim, artist="Original", title="Untouched")

    filename.run(ctx)

    assert probe.read(victim).fields["artist"] == "Original"
    assert any("outside the collection" in r["evidence"]["reason"]
               for r in _skips(ctx))


def test_an_ordinary_row_inside_the_collection_still_applies(tmp_path):
    ctx, _ = _setup(tmp_path, "Album/01.mp3")
    target = ctx.root / "Album" / "01.mp3"
    _mp3(target)

    filename.run(ctx)

    assert probe.read(target).fields["artist"] == "Intruder"
