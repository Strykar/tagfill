"""Every run says which config it loaded and which root it is about to
touch.

External review: config.load(None) tries ./tagfill.toml first, and nothing
in the output said so. A tagfill.toml sitting inside a downloaded album
folder -- the exact place someone cd's into to try the tool -- silently
redefined root and workdir for that invocation. Combined with root once
defaulting to cwd, an --apply could operate somewhere the user never
intended. Same trust problem that made git grow safe.directory.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tagfill import cli, config


def test_a_cwd_config_is_announced(tmp_path, monkeypatch, capsys):
    music = tmp_path / "Music"
    music.mkdir()
    cfg = tmp_path / "tagfill.toml"
    cfg.write_text(f'[collection]\nroot = "{music.as_posix()}"\n',
                   encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    cli.main(["--workdir", str(tmp_path / "work"), "census"])

    out = capsys.readouterr().out
    assert str(cfg.resolve()) in out, "the config that was loaded"
    assert str(music.resolve()) in out, "the root it points at"


def test_with_no_config_it_says_so(tmp_path, monkeypatch, capsys):
    music = tmp_path / "Music"
    music.mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("APPDATA", raising=False)

    cli.main(["--music-dir", str(music), "--workdir", str(tmp_path / "w"),
              "census"])

    assert "(defaults)" in capsys.readouterr().out


def test_config_path_is_recorded_on_the_config(tmp_path):
    cfg = tmp_path / "tagfill.toml"
    cfg.write_text('[collection]\nroot = "~/Music"\n', encoding="utf-8")
    assert config.load(cfg).config_path == cfg.resolve()


def test_the_default_config_has_no_path(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("APPDATA", raising=False)
    assert config.load(None).config_path is None


def test_the_workdir_is_owner_only(tmp_path):
    """backup/tags.jsonl holds every tag tagfill has seen and the full
    bytes of every cover it replaced."""
    import os

    from tagfill.journal import Journal
    work = tmp_path / "work"
    Journal(work)
    if os.name != "nt":
        assert work.stat().st_mode & 0o777 == 0o700


def test_an_existing_workdir_is_left_as_the_user_set_it(tmp_path):
    import os

    from tagfill.journal import Journal
    work = tmp_path / "work"
    work.mkdir()
    if os.name != "nt":
        work.chmod(0o755)
        Journal(work)
        assert work.stat().st_mode & 0o777 == 0o755
