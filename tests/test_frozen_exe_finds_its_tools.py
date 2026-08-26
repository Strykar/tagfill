"""External review: the Windows install section says to put ffmpeg.exe,
flac.exe and fpcalc.exe next to tagfill.exe. CreateProcess does search the
parent application's directory, so the subprocess calls would find them --
but convert.py and acoustid.py gate on shutil.which(), which searches PATH
and never the executable's own directory. Run C:\\tagfill\\tagfill.exe from
any other working directory and both stages skip with "needs ... on PATH",
having refused to attempt a call that would have succeeded.

CI's smoke test only runs --version and --report, so it cannot see this.
"""

import os
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "packaging"))

from entrypoint import _add_own_directory_to_path


def test_a_frozen_exe_puts_its_own_directory_on_path(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "tagfill.exe"))
    monkeypatch.setenv("PATH", "/somewhere/else")

    _add_own_directory_to_path()

    assert os.environ["PATH"].split(os.pathsep)[0] == str(tmp_path)


def test_which_then_finds_a_neighbouring_tool(tmp_path, monkeypatch):
    """The whole point: shutil.which() has to agree with CreateProcess."""
    tool = tmp_path / ("ffmpeg.exe" if os.name == "nt" else "ffmpeg")
    tool.write_text("#!/bin/sh\n", encoding="utf-8")
    tool.chmod(0o755)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "tagfill.exe"))
    monkeypatch.setenv("PATH", str(tmp_path / "nothing-here"))
    monkeypatch.chdir(tmp_path.parent)

    assert shutil.which("ffmpeg") is None
    _add_own_directory_to_path()
    assert shutil.which("ffmpeg") == str(tool)


def test_running_from_source_leaves_path_alone(monkeypatch):
    monkeypatch.delattr(sys, "frozen", raising=False)
    monkeypatch.setenv("PATH", "/only/this")
    _add_own_directory_to_path()
    assert os.environ["PATH"] == "/only/this"


def test_it_does_not_stack_duplicates(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "tagfill.exe"))
    monkeypatch.setenv("PATH", str(tmp_path))
    _add_own_directory_to_path()
    assert os.environ["PATH"] == str(tmp_path)
