"""PyInstaller's Analysis() target.

Pointing PyInstaller straight at tagfill/cli.py runs it as a
standalone top-level script with no package context, so cli.py's relative
imports ("from . import config") fail with "attempted relative import
with no known parent package". Importing tagfill as a package here,
with the repo root on pathex, avoids that.
"""

import os
import sys
from pathlib import Path

from tagfill.cli import main


def _add_own_directory_to_path() -> None:
    """Make shutil.which() agree with what actually runs.

    The Windows install instructions say to drop ffmpeg.exe, flac.exe and
    fpcalc.exe next to tagfill.exe, and for the subprocess calls that is
    true -- CreateProcess searches the parent application's directory. But
    the stages gate on shutil.which(), which searches PATH and never the
    executable's own directory, so running C:\tagfill\tagfill.exe from
    anywhere else made convert and acoustid announce "needs ... on PATH"
    and skip, while the subprocess they refused to attempt would have
    worked.
    """
    if not getattr(sys, "frozen", False):
        return
    here = str(Path(sys.executable).resolve().parent)
    path = os.environ.get("PATH", "")
    if here not in path.split(os.pathsep):
        os.environ["PATH"] = here + os.pathsep + path


if __name__ == "__main__":
    _add_own_directory_to_path()
    sys.exit(main())
