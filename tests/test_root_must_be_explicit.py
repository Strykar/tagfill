"""External review, release blocker: Config.root defaulted to Path("."),
and the only check was is_dir() -- which the current directory always
passes. `tagfill run --apply` in the wrong terminal walked whatever you
were standing in, and even a read-only census overwrote the shared
workdir's baseline for your real collection with a scan of somewhere else.

A collection root is not the kind of thing to guess at.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tagfill import cli, config


def test_config_has_no_default_root():
    assert config.Config().root is None


def test_a_command_with_no_root_refuses(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    assert cli.main(["census"]) == 1
    assert "no collection root" in capsys.readouterr().out


def test_report_with_no_root_refuses_too(tmp_path, monkeypatch, capsys):
    """--report is read-only, but it still writes a census and a baseline
    into the workdir."""
    monkeypatch.chdir(tmp_path)
    assert cli.main(["--report"]) == 1
    assert "no collection root" in capsys.readouterr().out


def test_music_dir_is_enough(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "Music").mkdir()
    assert cli.main(["--music-dir", str(tmp_path / "Music"),
                     "--workdir", str(tmp_path / "w"), "--report"]) == 0


def test_a_root_that_is_not_a_directory_still_says_so(tmp_path, monkeypatch,
                                                     capsys):
    monkeypatch.chdir(tmp_path)
    assert cli.main(["--music-dir", str(tmp_path / "nope"), "census"]) == 1
    assert "not a directory" in capsys.readouterr().out
