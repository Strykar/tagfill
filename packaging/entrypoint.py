"""PyInstaller's Analysis() target.

Pointing PyInstaller straight at tagfill/cli.py runs it as a
standalone top-level script with no package context, so cli.py's relative
imports ("from . import config") fail with "attempted relative import
with no known parent package". Importing tagfill as a package here,
with the repo root on pathex, avoids that.
"""

import sys

from tagfill.cli import main

if __name__ == "__main__":
    sys.exit(main())
