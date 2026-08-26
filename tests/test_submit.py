"""Stage 9 posts to a public database, and had no tests at all.

Coverage audit before the 0.1.0 tag: submit.py was the one module at 0%.
It is opt-in, never run by the pipeline, and gated behind an AcoustID
account key -- but "nobody has ever executed this" is not a thing to ship
on a write path aimed at a shared database other people rely on.

The POST itself is mocked. What is actually being pinned is everything
around it: which files are selected, which are diverted to the MusicBrainz
worklist instead, and the three separate reasons it declines to submit.
"""

import csv
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from tagfill import config
from tagfill.journal import Journal, Record, ReviewQueue
from tagfill.stages import Context, StagePrecondition, census, submit


def _ctx(tmp_path, *, apply=False, key=""):
    cfg = config.Config()
    cfg.root, cfg.workdir = tmp_path / "Music", tmp_path / "work"
    cfg.acoustid_api_key = key
    cfg.acoustid_key_file = str(tmp_path / "no-such-key")
    cfg.root.mkdir(parents=True, exist_ok=True)
    return Context(cfg=cfg, journal=Journal(cfg.workdir),
                   review=ReviewQueue(cfg.workdir), apply=apply)


def _census(ctx, rows):
    ctx.workdir.mkdir(parents=True, exist_ok=True)
    with open(ctx.workdir / "census.csv", "w", newline="",
              encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=census.COLUMNS)
        w.writeheader()
        for row in rows:
            full = dict.fromkeys(census.COLUMNS, "")
            full.update(row)
            w.writerow(full)


def _empty_recording(ctx, path):
    ctx.journal.append(Record(
        stage="acoustid", path=path, action="reject",
        evidence={"reason": "empty recordings", "acoustid": "abc-123"}))


def test_no_journal_is_a_precondition_not_a_crash(tmp_path):
    with pytest.raises(StagePrecondition, match="stage 5"):
        submit.run(_ctx(tmp_path))


def test_a_journal_with_nothing_to_submit_says_so(tmp_path, capsys):
    ctx = _ctx(tmp_path)
    ctx.journal.append(Record(stage="mb", path="a.mp3", action="apply",
                              field="genre", new="Techno"))
    submit.run(ctx)
    assert "no empty-recording matches" in capsys.readouterr().out


def test_a_tagged_file_is_ready_and_an_untagged_one_is_worklisted(tmp_path,
                                                                  capsys):
    """The split the whole stage exists for: a fingerprint AcoustID knows
    but has no recording for is submittable only once we know what it is."""
    ctx = _ctx(tmp_path)
    _empty_recording(ctx, "Album/known.mp3")
    _empty_recording(ctx, "Album/unknown.mp3")
    _census(ctx, [
        {"path": "Album/known.mp3", "artist": "A", "title": "T"},
        {"path": "Album/unknown.mp3", "artist": "", "title": ""},
    ])

    submit.run(ctx)

    with open(ctx.workdir / "report" / "mb-additions.csv", newline="",
              encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    assert [r["path"] for r in rows] == ["Album/known.mp3"]
    assert rows[0]["artist"] == "A"
    assert "1 pairings ready" in capsys.readouterr().out


def test_a_file_with_a_census_issue_is_never_submitted(tmp_path):
    ctx = _ctx(tmp_path)
    _empty_recording(ctx, "Album/broken.mp3")
    _census(ctx, [{"path": "Album/broken.mp3", "artist": "A", "title": "T",
                   "issue": "unreadable: HeaderNotFoundError"}])

    submit.run(ctx)

    with open(ctx.workdir / "report" / "mb-additions.csv", newline="",
              encoding="utf-8") as f:
        assert list(csv.DictReader(f)) == []


def test_without_a_user_key_it_reports_and_stops(tmp_path, capsys,
                                                 monkeypatch):
    monkeypatch.delenv("ACOUSTID_USER_KEY", raising=False)
    ctx = _ctx(tmp_path, apply=True, key="app-key")
    _empty_recording(ctx, "a.mp3")
    _census(ctx, [{"path": "a.mp3", "artist": "A", "title": "T"}])

    submit.run(ctx)

    out = capsys.readouterr().out
    assert "ACOUSTID_USER_KEY" in out
    assert "1 pairings ready" in out


def test_with_both_keys_but_no_apply_it_is_a_dry_run(tmp_path, capsys,
                                                     monkeypatch):
    """A stage that posts to someone else's database must not do it just
    because the keys happen to be set."""
    monkeypatch.setenv("ACOUSTID_USER_KEY", "user-key")
    ctx = _ctx(tmp_path, apply=False, key="app-key")
    _empty_recording(ctx, "a.mp3")
    _census(ctx, [{"path": "a.mp3", "artist": "A", "title": "T"}])

    submit.run(ctx)

    out = capsys.readouterr().out
    assert "would submit 1" in out and "dry run" in out


def test_the_journal_records_each_submission_outcome(tmp_path, monkeypatch):
    requests_mock = pytest.importorskip("requests_mock")
    monkeypatch.setenv("ACOUSTID_USER_KEY", "user-key")
    ctx = _ctx(tmp_path, apply=True, key="app-key")
    _empty_recording(ctx, "a.mp3")
    _census(ctx, [{"path": "a.mp3", "artist": "A", "title": "T"}])
    (ctx.root / "a.mp3").write_bytes(b"not really audio")

    class _FakeRun:
        returncode = 0
        stdout = json.dumps({"duration": 200, "fingerprint": "AQAA"})

    monkeypatch.setattr(__import__("subprocess"), "run",
                        lambda *a, **k: _FakeRun())

    with requests_mock.Mocker() as m:
        m.post("https://api.acoustid.org/v2/submit", json={"status": "ok"})
        submit.run(ctx)

    recs = [json.loads(x) for x in
            ctx.journal.path.read_text(encoding="utf-8").splitlines()]
    posted = [r for r in recs if r["stage"] == "submit"]
    assert len(posted) == 1
    assert posted[0]["action"] == "apply"
    assert posted[0]["evidence"]["submitted"] is True


def test_a_rejected_submission_is_journaled_as_such(tmp_path, monkeypatch):
    requests_mock = pytest.importorskip("requests_mock")
    monkeypatch.setenv("ACOUSTID_USER_KEY", "user-key")
    ctx = _ctx(tmp_path, apply=True, key="app-key")
    _empty_recording(ctx, "a.mp3")
    _census(ctx, [{"path": "a.mp3", "artist": "A", "title": "T"}])
    (ctx.root / "a.mp3").write_bytes(b"not really audio")

    class _FakeRun:
        returncode = 0
        stdout = json.dumps({"duration": 200, "fingerprint": "AQAA"})

    monkeypatch.setattr(__import__("subprocess"), "run",
                        lambda *a, **k: _FakeRun())

    with requests_mock.Mocker() as m:
        m.post("https://api.acoustid.org/v2/submit",
               json={"status": "error", "error": {"message": "bad key"}})
        submit.run(ctx)

    recs = [json.loads(x) for x in
            ctx.journal.path.read_text(encoding="utf-8").splitlines()]
    posted = [r for r in recs if r["stage"] == "submit"]
    assert posted[0]["action"] == "reject"
    assert posted[0]["evidence"]["submitted"] is False
