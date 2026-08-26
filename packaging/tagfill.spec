# PyInstaller spec for a single-file tagfill executable.
#
# PyInstaller doesn't cross-compile: this must run on the target OS (built
# on windows-latest by .github/workflows/windows-exe.yml, which is the
# reliable way to get a Windows .exe without owning a Windows machine).
#
# mutagen and musicbrainzngs pick their submodules through a runtime format
# registry rather than a static import PyInstaller's analyzer can always
# see fully, so their submodules are collected explicitly. Everything else
# (requests, certifi, pillow) has hooks bundled with PyInstaller itself.
#
# Not bundled: ffmpeg, flac, fpcalc (chromaprint). Those are separate
# binaries the convert and acoustid stages shell out to -- redistributing
# them is a separate licensing/maintenance decision, so for now they stay
# a documented prerequisite (see README's Install section).

import os

from PyInstaller.utils.hooks import collect_submodules

repo_root = os.path.abspath(os.path.join(SPECPATH, ".."))
entry = os.path.join(SPECPATH, "entrypoint.py")

hiddenimports = collect_submodules("mutagen") + collect_submodules("musicbrainzngs")

a = Analysis(
    [entry],
    pathex=[repo_root],
    hiddenimports=hiddenimports,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="tagfill",
    console=True,
)
