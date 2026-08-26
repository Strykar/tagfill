"""Configuration.

Everything that was specific to the original collection this tool was designed
against (which subtree is a DJ pool, where quarantined originals go, every
threshold) lives here rather than in code. `tagfill init` writes a
commented example.
"""

from __future__ import annotations

import os
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path


def default_workdir() -> Path:
    """Per-user state directory, not the current working directory.

    XDG_STATE_HOME rather than XDG_CACHE_HOME, because the workdir holds
    `backup/tags.jsonl` and `quarantine/wav` — the undo path and the
    originals a WAV conversion replaced. The XDG spec says cache contents
    can be deleted at any time without consequence, which is exactly what
    isn't true here; state is the spec's slot for "persists between runs,
    not precious enough to be data, not config".
    """
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or "~/AppData/Local"
    elif sys.platform == "darwin":
        base = "~/Library/Application Support"
    else:
        base = os.environ.get("XDG_STATE_HOME") or "~/.local/state"
    return Path(os.path.expanduser(base)) / "tagfill"


EXAMPLE = """\
# tagfill configuration. All paths may be absolute or relative to this file.

[collection]
root = "~/Music"            # the collection. Nothing outside it is ever touched.
# workdir = ""              # journal, census, reports, caches, backups,
                            # quarantine. Everything the tool produces.
                            # Defaults to your per-user state dir:
                            #   ~/.local/state/tagfill    (Linux/BSD)
                            #   %LOCALAPPDATA%\\tagfill    (Windows)
                            #   ~/Library/Application Support/tagfill (macOS)
exclude = []                # globs relative to root, e.g. ["Incoming/*"]
extra_tags = ["genre", "tracknumber"]  # beyond artist/title/album/etc, which
                                 # of these to fetch and write when a source
                                 # has them. Track number is only ever written
                                 # when a source's match proves local file
                                 # order equals track order; remove either
                                 # entry (or set []) to never touch that field.

[thresholds]
art_min_px = 300            # reject art below this on the short edge
filename_confidence = 0.70  # below this, filename parses go to the review queue
mb_duration_tolerance_s = 4.0
mb_vector_pass_fraction = 0.90
itunes_min_margin = 0.15    # top candidate must beat the runner-up by this

[musicbrainz]
# MusicBrainz requires a real user agent with contact info. Stage `mb` refuses
# to run until you fill this in. https://musicbrainz.org/doc/MusicBrainz_API
app = "tagfill"
contact = ""                # e.g. "you@example.org"
rate_s = 1.0                # min seconds between requests (1.0 = MB's limit)

[acoustid]
# Register a free application key at https://acoustid.org/new-application
# Read from (in order): this value, $ACOUSTID_API_KEY, key_file.
api_key = ""
key_file = "~/.config/acoustid.key"
min_score = 0.85
rate_s = 0.34               # acoustid.org asks for <= 3 req/s

[itunes]
art_sizes = [1200, 600]     # tried in order via the artworkUrlNNN rewrite

[crates]
# Folders that are playlists, not releases. Writing album=<folder> would
# manufacture fake multi-artist albums, so the folder name goes to the
# `grouping` tag instead and album stays empty. Globs relative to root.
globs = []                  # e.g. ["DJ Pool/*"]
"""


@dataclass
class Config:
    # No default. cwd is always a directory, so defaulting to it meant
    # `tagfill run --apply` in the wrong terminal walked whatever you were
    # standing in -- and even a read-only census overwrote the shared
    # workdir's baseline for your real collection.
    root: Path | None = None
    workdir: Path = field(default_factory=default_workdir)
    exclude: list[str] = field(default_factory=list)
    extra_tags: list[str] = field(
        default_factory=lambda: ["genre", "tracknumber"])

    art_min_px: int = 300
    filename_confidence: float = 0.70
    mb_duration_tolerance_s: float = 4.0
    mb_vector_pass_fraction: float = 0.90
    itunes_min_margin: float = 0.15

    mb_app: str = "tagfill"
    mb_contact: str = ""
    mb_rate_s: float = 1.0
    acoustid_rate_s: float = 0.34   # acoustid.org asks for <= 3 req/s

    acoustid_api_key: str = ""
    acoustid_key_file: str = "~/.config/acoustid.key"
    acoustid_min_score: float = 0.85

    itunes_art_sizes: list[int] = field(default_factory=lambda: [1200, 600])

    crate_globs: list[str] = field(default_factory=list)

    config_dir: Path = Path(".")

    def resolve_path(self, p: str | Path) -> Path:
        p = Path(os.path.expanduser(str(p)))
        return p if p.is_absolute() else (self.config_dir / p).resolve()

    @property
    def acoustid_key(self) -> str:
        if self.acoustid_api_key:
            return self.acoustid_api_key
        if os.environ.get("ACOUSTID_API_KEY"):
            return os.environ["ACOUSTID_API_KEY"]
        kf = Path(os.path.expanduser(self.acoustid_key_file))
        if kf.is_file():
            return kf.read_text(encoding="utf-8").strip()
        return ""


def _config_locations() -> list[Path]:
    """Where to look when no --config was given.

    ~/.config works on Windows but is not where anyone looks for it, so
    %APPDATA%\\tagfill\\tagfill.toml comes first there. The dotfile stays in
    the list either way: someone syncing a home directory across both
    should not have to keep two.
    """
    here = []
    appdata = os.environ.get("APPDATA")
    if appdata:
        here.append(Path(appdata) / "tagfill" / "tagfill.toml")
    here.append(Path("~/.config/tagfill.toml").expanduser())
    return here


def load(path: Path | None) -> Config:
    cfg = Config()
    if path is None:
        for candidate in (Path("tagfill.toml"), *_config_locations()):
            if candidate.is_file():
                path = candidate
                break
    if path is None:
        return cfg
    with open(path, "rb") as f:
        data = tomllib.load(f)
    cfg.config_dir = Path(path).resolve().parent

    coll = data.get("collection", {})
    if "root" in coll:
        cfg.root = cfg.resolve_path(coll["root"])
    if "workdir" in coll:
        cfg.workdir = cfg.resolve_path(coll["workdir"])
    cfg.exclude = list(coll.get("exclude", []))
    if "extra_tags" in coll:
        cfg.extra_tags = list(coll["extra_tags"])

    th = data.get("thresholds", {})
    for key in ("art_min_px", "filename_confidence", "mb_duration_tolerance_s",
                "mb_vector_pass_fraction", "itunes_min_margin"):
        if key in th:
            setattr(cfg, key, th[key])

    mb = data.get("musicbrainz", {})
    cfg.mb_app = mb.get("app", cfg.mb_app)
    cfg.mb_contact = mb.get("contact", cfg.mb_contact)
    cfg.mb_rate_s = float(mb.get("rate_s", cfg.mb_rate_s))
    ac = data.get("acoustid", {})
    cfg.acoustid_rate_s = float(ac.get("rate_s", cfg.acoustid_rate_s))
    cfg.acoustid_api_key = ac.get("api_key", cfg.acoustid_api_key)
    cfg.acoustid_key_file = ac.get("key_file", cfg.acoustid_key_file)
    cfg.acoustid_min_score = float(ac.get("min_score", cfg.acoustid_min_score))

    it = data.get("itunes", {})
    cfg.itunes_art_sizes = list(it.get("art_sizes", cfg.itunes_art_sizes))

    cfg.crate_globs = list(data.get("crates", {}).get("globs", []))
    return cfg
