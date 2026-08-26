"""Four small Windows behaviours, each wrong in a different way.

External review, all real:
  - time.strftime("%z") is the timezone *name* on the UCRT, so journal
    timestamps read "2026-08-26T17:24:00India Standard Time". Nothing
    parses ts, so it is cosmetic -- but the journal is the record of
    record.
  - restore --only compares the user's argument against a stored POSIX
    path with exact string equality, so a Windows user typing backslashes
    matches nothing.
  - The journal is opened in text mode, so Windows writes CRLF. Harmless
    to JSON, which treats \\r as whitespace, but it makes journals
    diff-noisy across machines.
  - ~/.config/tagfill.toml works on Windows but is not where anyone looks.
"""

import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tagfill import config
from tagfill.backup import restore
from tagfill.journal import Journal, Record


def test_the_journal_timestamp_is_an_iso_offset(tmp_path):
    j = Journal(tmp_path)
    j.append(Record(stage="mb", path="a.mp3", action="propose"))
    ts = json.loads((tmp_path / "journal.jsonl").read_text(
        encoding="utf-8").splitlines()[0])["ts"]
    # The thing the UCRT would have written is not parseable; this is.
    assert datetime.fromisoformat(ts)


def test_the_journal_is_written_with_unix_newlines(tmp_path):
    j = Journal(tmp_path)
    j.append(Record(stage="mb", path="a.mp3", action="propose"))
    j.append(Record(stage="mb", path="b.mp3", action="propose"))
    assert b"\r\n" not in (tmp_path / "journal.jsonl").read_bytes()


def _backup_line(path):
    return json.dumps({"path": "Some Album/01 Track.mp3",
                       "container": "mp3", "fields": {"artist": "A"},
                       "art": None}) + "\n"


def test_restore_only_accepts_a_windows_style_path(tmp_path, monkeypatch):
    """Driven through the win32 branch on purpose: on POSIX a backslash is
    a legal filename character, so translating it there would break
    matches that work today. Testing this with plain PurePath on Linux
    would silently prove nothing, since PurePosixPath leaves the backslash
    alone."""
    monkeypatch.setattr(sys, "platform", "win32")
    root = tmp_path / "Music"
    (root / "Some Album").mkdir(parents=True)
    track = root / "Some Album" / "01 Track.mp3"
    track.write_bytes(b"not really an mp3")
    backup = tmp_path / "tags.jsonl"
    backup.write_text(_backup_line(track), encoding="utf-8")

    # The file is not a real mp3, so nothing is written -- what is being
    # tested is whether the record is selected at all, and an unselected
    # record never reaches the writer.
    from tagfill import backup as backup_mod
    seen = []
    backup_mod.probe = type("P", (), {
        "write": staticmethod(lambda *a, **k: seen.append(a[0])),
        "embed_art": staticmethod(lambda *a, **k: None),
        "remove_art": staticmethod(lambda *a, **k: None),
        "delete_fields": staticmethod(lambda *a, **k: None)})()

    restore(root, backup, only=r"Some Album\01 Track.mp3")
    assert seen, "a backslash path must select the same record as a slash one"


def test_appdata_comes_first_on_a_machine_that_has_one(tmp_path,
                                                       monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path / "AppData" / "Roaming"))
    locations = config._config_locations()
    assert locations[0] == tmp_path / "AppData" / "Roaming" / "tagfill" \
        / "tagfill.toml"
    assert any(".config" in str(p) for p in locations), (
        "the dotfile has to stay in the list for synced home directories")


def test_no_appdata_means_just_the_dotfile(monkeypatch):
    monkeypatch.delenv("APPDATA", raising=False)
    assert all(".config" in str(p) for p in config._config_locations())


def test_restore_only_leaves_a_posix_backslash_name_alone(tmp_path,
                                                          monkeypatch):
    """A file genuinely called "AC\\DC - Track.mp3" on Linux must still be
    selectable by its real name."""
    monkeypatch.setattr(sys, "platform", "linux")
    root = tmp_path / "Music"
    root.mkdir()
    rel = "AC\\DC - Track.mp3"
    (root / rel).write_bytes(b"x")
    backup = tmp_path / "tags.jsonl"
    backup.write_text(json.dumps({"path": rel, "container": "mp3",
                                  "fields": {"artist": "A"}, "art": None})
                      + "\n", encoding="utf-8")
    seen = []
    from tagfill import backup as backup_mod
    monkeypatch.setattr(backup_mod, "probe", type("P", (), {
        "write": staticmethod(lambda *a, **k: seen.append(a[0])),
        "embed_art": staticmethod(lambda *a, **k: None),
        "remove_art": staticmethod(lambda *a, **k: None),
        "delete_fields": staticmethod(lambda *a, **k: None)})())

    restore(root, backup, only=rel)
    assert seen
