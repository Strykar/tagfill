"""Regression test: a single transient AcoustID request failure must not
abort the rest of the batch.

Found live, on the real collection: request #14 of a 20-file --limit batch
raised (a dropped connection or truncated chunked response — re-issuing the
identical request seconds later returned a clean 200, so it wasn't a bad
fingerprint or a bad file). The handler was `except Exception: break`,
which silently left files 14-20 completely unprocessed with no journal
record at all — indistinguishable from "tagfill hasn't gotten to
these yet" versus "tagfill tried and gave up".
"""

import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

requests_mock = pytest.importorskip("requests_mock")

from tagfill import config
from tagfill.journal import Journal, ReviewQueue
from tagfill.stages import Context, acoustid


def _ctx(tmp_path, root):
    cfg = config.Config()
    cfg.root = root
    cfg.workdir = tmp_path
    cfg.acoustid_api_key = "testkey"
    return Context(cfg=cfg, journal=Journal(cfg.workdir),
                  review=ReviewQueue(cfg.workdir), apply=False,
                  overwrite=False, limit=None, subpath=None,
                  backup_tags=False,
                  from_review=None)


@pytest.fixture
def two_file_root(tmp_path):
    root = tmp_path / "music"
    root.mkdir()
    for name in ("a.mp3", "b.mp3"):
        (root / name).write_bytes(b"not real audio, _fingerprint is stubbed")
    return root


def _write_census(ctx, root):
    import csv

    from tagfill.stages import census
    ctx.workdir.mkdir(parents=True, exist_ok=True)
    with open(ctx.workdir / "census.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=census.COLUMNS)
        w.writeheader()
        for name in ("a.mp3", "b.mp3"):
            row = dict.fromkeys(census.COLUMNS, "")
            row.update(path=name, container="mp3", size=100, mtime="0",
                      duration="200")
            w.writerow(row)


@pytest.mark.skipif(shutil.which("fpcalc") is None, reason="needs fpcalc on PATH")
def test_one_failed_request_does_not_abort_remaining_files(
        tmp_path, two_file_root, monkeypatch):
    ctx = _ctx(tmp_path, two_file_root)
    _write_census(ctx, two_file_root)
    monkeypatch.setattr(acoustid, "_fingerprint", lambda p: (200, "stubfp"))

    with requests_mock.Mocker() as m:
        m.post("https://api.acoustid.org/v2/lookup",
              [{"exc": ConnectionError("dropped")},
               {"json": {"results": []}}])
        acoustid.run(ctx)

    import json
    with open(ctx.workdir / "journal.jsonl", encoding="utf-8") as f:
        seen_paths = {json.loads(line).get("path") for line in f}
    assert seen_paths == {"a.mp3", "b.mp3"}, (
        "both files must have a journal record; the second file must not "
        "be silently dropped because the first file's request failed")


def test_failed_request_is_journaled_not_silently_swallowed():
    src = (Path(__file__).resolve().parents[1]
           / "tagfill" / "stages" / "acoustid.py")
    text = src.read_text(encoding="utf-8")
    assert 'action="skip"' in text and '"reason": "lookup failed"' in text
    # the exact bug: a bare `break` inside the per-file try/except that
    # would abort the whole remaining batch on one transient failure
    except_block = text.split("except Exception as e:")[1].split("continue")[0]
    assert "break" not in except_block
