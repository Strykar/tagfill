"""The journal grows without bound and is parsed in full on every run.

External review: append-only is the right durability model, but a single
dry run over 30k under-tagged files emits tens of thousands of propose
records, and users iterating on thresholds re-run dry runs. _load_applied
and report.py then json.loads every line before any work starts.

Three fixes, cheapest first: skip the parse on a substring test, hold the
write handle open for the run instead of reopening per record, and a
`tagfill compact` that keeps only the latest apply per (stage, path) --
the applies are what the resume guard reads, and nothing else is
load-bearing afterwards.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tagfill import cli
from tagfill.journal import Journal, Record


def _fill(j, applies=3, proposes=50):
    for i in range(proposes):
        j.append(Record(stage="mb", path=f"a/{i}.mp3", action="propose",
                        field="release"))
    for i in range(applies):
        for _run in range(3):       # the same file, three sessions
            j.append(Record(stage="mb", path=f"a/{i}.mp3", action="apply",
                            field="genre", new="Techno", size=1,
                            mtime=1.0, sha1_head="x"))


def test_compact_keeps_the_latest_apply_per_file(tmp_path):
    j = Journal(tmp_path)
    _fill(j)
    before, after = j.compact()

    assert before == 59
    assert after == 3, "one apply per (stage, path), proposes dropped"
    lines = j.path.read_text(encoding="utf-8").splitlines()
    assert {json.loads(x)["path"] for x in lines} == {
        "a/0.mp3", "a/1.mp3", "a/2.mp3"}
    assert all(json.loads(x)["action"] == "apply" for x in lines)


def test_the_resume_guard_still_works_after_compacting(tmp_path):
    """The whole point of keeping the applies."""
    root = tmp_path / "Music"
    root.mkdir()
    f = root / "track.mp3"
    f.write_bytes(b"audio bytes")
    j = Journal(tmp_path / "work")
    j.record_write("mb", root, f, "genre", None, "Techno")
    j.append(Record(stage="mb", path="track.mp3", action="propose"))
    assert j.already_done("mb", root, f)

    j.compact()

    assert Journal(tmp_path / "work").already_done("mb", root, f)


def test_compacting_an_absent_journal_is_not_an_error(tmp_path):
    assert Journal(tmp_path).compact() == (0, 0)


def test_a_corrupt_line_does_not_survive_or_crash_compaction(tmp_path):
    j = Journal(tmp_path)
    j.append(Record(stage="mb", path="a.mp3", action="apply", field="genre",
                    size=1, mtime=1.0, sha1_head="x"))
    j.close()
    with open(j.path, "a", encoding="utf-8") as f:
        f.write('{"action": "apply" broken\n')
    before, after = j.compact()
    assert (before, after) == (2, 1)


def test_the_cli_exposes_it(tmp_path, capsys):
    (tmp_path / "Music").mkdir()
    j = Journal(tmp_path / "work")
    _fill(j)
    j.close()

    rc = cli.main(["--music-dir", str(tmp_path / "Music"),
                   "--workdir", str(tmp_path / "work"), "compact"])

    assert rc == 0
    assert "59 records -> 3" in capsys.readouterr().out


def test_loading_applies_skips_parsing_proposals(tmp_path, monkeypatch):
    """The substring test is most of the cost of loading a large journal,
    so it has to actually skip the parse."""
    j = Journal(tmp_path)
    _fill(j, applies=1, proposes=20)
    j.close()

    parsed = []
    real = json.loads
    monkeypatch.setattr(json, "loads",
                        lambda s, *a, **k: (parsed.append(s), real(s))[1])
    Journal(tmp_path)._load_applied()

    assert len(parsed) == 3, "only the apply lines were parsed"


def test_the_write_handle_is_not_reopened_per_record(tmp_path, monkeypatch):
    import builtins
    j = Journal(tmp_path)
    opens = []
    real = builtins.open
    monkeypatch.setattr(builtins, "open",
                        lambda *a, **k: (opens.append(a[0]), real(*a, **k))[1])
    for i in range(10):
        j.append(Record(stage="mb", path=f"{i}.mp3", action="propose"))
    assert len(opens) == 1
    j.close()
