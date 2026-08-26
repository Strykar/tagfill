"""Format-aware tag read/write. The one place containers are known.

Why this module exists as a chokepoint: `mutagen.File(path, easy=True)` has no
Easy wrapper for AIFF or WAVE. It silently returns the raw object, so
`tags.get("artist")` misses `TPE1` and a fully tagged file reads as untagged.
In the collection this tool was first built against, that bug made 119
perfectly tagged AIFFs look virgin. The containment is: never use `easy=True`,
dispatch on an explicit container map, and assert the class mutagen returns.

Fields: artist, title, album, albumartist, date, grouping, label, genre,
tracknumber. Art is a separate read/embed pair, byte-faithful in both
directions.
"""

from __future__ import annotations

import base64
import contextlib
import io
import os
import shutil
from dataclasses import dataclass
from dataclasses import field as dc_field
from pathlib import Path

from mutagen.aiff import AIFF
from mutagen.dsdiff import DSDIFF
from mutagen.dsf import DSF
from mutagen.flac import FLAC, Picture
from mutagen.id3 import (
    APIC,
    TALB,
    TCON,
    TDRC,
    TIT1,
    TIT2,
    TPE1,
    TPE2,
    TPUB,
    TRCK,
)
from mutagen.mp3 import MP3
from mutagen.mp4 import MP4, MP4Cover, MP4FreeForm
from mutagen.oggopus import OggOpus
from mutagen.oggvorbis import OggVorbis
from mutagen.wave import WAVE

FIELDS = ("artist", "title", "album", "albumartist", "date", "grouping",
         "label", "genre", "tracknumber")

_ID3_FRAMES = {
    "artist": ("TPE1", TPE1), "title": ("TIT2", TIT2), "album": ("TALB", TALB),
    "albumartist": ("TPE2", TPE2), "date": ("TDRC", TDRC),
    "grouping": ("TIT1", TIT1), "label": ("TPUB", TPUB),
    "genre": ("TCON", TCON), "tracknumber": ("TRCK", TRCK),
}
_VORBIS_KEYS = {
    "artist": "ARTIST", "title": "TITLE", "album": "ALBUM",
    "albumartist": "ALBUMARTIST", "date": "DATE", "grouping": "GROUPING",
    "label": "LABEL", "genre": "GENRE", "tracknumber": "TRACKNUMBER",
}
_MP4_KEYS = {
    "artist": "\xa9ART", "title": "\xa9nam", "album": "\xa9alb",
    "albumartist": "aART", "date": "\xa9day", "grouping": "\xa9grp",
    "label": "----:com.apple.iTunes:LABEL",
    "genre": "\xa9gen", "tracknumber": "trkn",
}

# suffix -> (mutagen class, family)
# DSD (.dsf, .dff) carries ID3v2 like MP3 and AIFF do -- DSF keeps it in a
# trailing metadata chunk, DSDIFF in a chunk with the id "ID3 " -- so both
# reuse the id3 family wholesale rather than needing a fourth one.
_CONTAINERS = {
    ".mp3": (MP3, "id3"),
    ".aiff": (AIFF, "id3"), ".aif": (AIFF, "id3"), ".aifc": (AIFF, "id3"),
    ".wav": (WAVE, "id3"),
    ".dsf": (DSF, "id3"), ".dff": (DSDIFF, "id3"),
    ".flac": (FLAC, "vorbis"),
    ".ogg": (OggVorbis, "vorbis"), ".opus": (OggOpus, "vorbis"),
    ".m4a": (MP4, "mp4"), ".mp4": (MP4, "mp4"),
}


class ProbeError(Exception):
    pass


@dataclass
class TagState:
    container: str = ""
    duration: float | None = None
    bitrate: int | None = None
    has_art: bool = False
    fields: dict[str, str | None] = dc_field(default_factory=dict)

    def get(self, name: str) -> str | None:
        return self.fields.get(name)

    def missing(self, name: str) -> bool:
        v = self.fields.get(name)
        return v is None or str(v).strip() == ""


def _open(path: Path):
    suffix = path.suffix.lower()
    if suffix not in _CONTAINERS:
        raise ProbeError(f"unknown container: {suffix}")
    cls, family = _CONTAINERS[suffix]
    if suffix == ".ogg":
        # .ogg may hold Vorbis or Opus; try both, assert what we got.
        for c in (OggVorbis, OggOpus):
            try:
                return c(path), "vorbis"
            except Exception:
                continue
        raise ProbeError("unreadable ogg (neither Vorbis nor Opus)")
    try:
        audio = cls(path)
    except Exception as e:
        raise ProbeError(f"{type(e).__name__}: {e}") from e
    # Finding-1 containment: assert the class, never trust a convenience flag.
    if not isinstance(audio, cls):
        raise ProbeError(f"expected {cls.__name__}, got {type(audio).__name__}")
    return audio, family


def read(path: Path) -> TagState:
    audio, family = _open(path)
    st = TagState(container=path.suffix.lower().lstrip("."))
    info = getattr(audio, "info", None)
    if info is not None:
        st.duration = float(getattr(info, "length", 0.0)) or None
        st.bitrate = getattr(info, "bitrate", None)

    if family == "id3":
        id3 = audio.tags
        for name, (fid, _) in _ID3_FRAMES.items():
            frame = id3.get(fid) if id3 else None
            st.fields[name] = str(frame.text[0]) if frame and frame.text else None
        st.has_art = bool(id3 and id3.getall("APIC"))
    elif family == "vorbis":
        tags = audio.tags or {}
        for name, key in _VORBIS_KEYS.items():
            vals = tags.get(key) or tags.get(key.lower())
            st.fields[name] = str(vals[0]) if vals else None
        if isinstance(audio, FLAC):
            st.has_art = bool(audio.pictures)
        else:
            st.has_art = bool(tags.get("metadata_block_picture"))
    else:  # mp4
        tags = audio.tags or {}
        for name, key in _MP4_KEYS.items():
            vals = tags.get(key)
            if not vals:
                st.fields[name] = None
            elif name == "tracknumber":
                # trkn is a (track, total) integer-pair atom, not text.
                st.fields[name] = str(vals[0][0]) if vals[0][0] else None
            else:
                v = vals[0]
                if isinstance(v, (bytes, MP4FreeForm)):
                    v = bytes(v).decode("utf-8", "replace")
                st.fields[name] = str(v)
        st.has_art = bool(tags.get("covr"))
    return st


def _atomic_save(path: Path, mutate) -> None:
    """Apply `mutate(audio, family)` and save, without ever leaving the
    user's file half-written.

    mutagen edits in place. Verified: adding a 1200px cover to an MP3 keeps
    the same inode and grows the file by 24KB, which means the entire audio
    payload was shifted forward inside the original file. Lose power partway
    through that and the track is corrupt, with no copy to fall back on --
    and this tool's whole premise is that it is safe to point at music you
    care about.

    So the edit happens on a copy alongside the original, gets flushed to
    disk, and only then replaces it via os.replace(), which is atomic on
    POSIX and on Windows for a same-directory rename. A crash at any point
    leaves either the untouched original or the finished new version, never
    a mixture. The temp lives in the same directory because os.replace is
    only atomic within one filesystem.

    What the replacement carries over, measured rather than assumed:
    permission bits and xattrs come free with copy2, but *ownership does
    not* -- a file group-owned by `media` came back owned by the running
    user's primary group, which is a silent permission change on a shared
    or NAS library. So owner and group are restored explicitly; that call
    is a no-op for a file you already own and does the real work under
    sudo or on a secondary group.

    Two costs that cannot be avoided this way. The file is briefly
    duplicated, so the write needs free space equal to its size and a
    writable parent directory -- an unwritable directory that previously
    accepted in-place edits now fails, and guarded_write journals it.

    And a hardlinked file becomes its own inode: nlink 2 -> 1, with the
    other name keeping the old content. For a deduplicated library that
    silently doubles disk use, but where the other link is a seeding
    torrent it is the safer outcome, since the shared copy is left exactly
    as the tracker expects.
    """
    tmp = path.with_name(f".{path.stem}.tagfill-tmp{path.suffix}")
    try:
        st = path.stat()
        shutil.copy2(path, tmp)          # mode, mtime and xattrs; not owner
        with contextlib.suppress(PermissionError, OSError):
            os.chown(tmp, st.st_uid, st.st_gid)   # no-op unless privileged
        audio, family = _open(tmp)
        mutate(audio, family)
        audio.save()
        # "r+b", not "rb": fsync on Windows is _commit(), which needs a
        # writable handle. A read-only one raises before os.replace, so
        # every write would fail there while passing on Linux.
        with open(tmp, "r+b") as fh:
            os.fsync(fh.fileno())        # durable before it becomes the file
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def write(path: Path, values: dict[str, str], overwrite: bool = False
          ) -> list[tuple[str, str | None, str]]:
    """Write fields; returns [(field, old, new)] actually changed.
    Never overwrites a non-empty field unless overwrite=True.

    The return value is what the file really holds afterwards, not what was
    requested. Not every value survives every container: ID3's TDRC is a
    timestamp frame, so a non-numeric date ("Unknown", "199?") is accepted
    by mutagen and then silently dropped on save, while the same string
    round-trips fine in a FLAC comment. Reporting the intent rather than the
    result would put a write in the journal that never happened, and the
    journal is the record everything else trusts. So re-read and report only
    fields that actually landed. Values a container normalises rather than
    drops (ID3 rewriting "1994-03-28T00:00:00" as "1994-03-28 00:00:00")
    still count as written."""
    current = read(path)
    todo = {}
    changed = []
    for name, new in values.items():
        if name not in FIELDS or new is None or str(new).strip() == "":
            continue
        old = current.get(name)
        if old is not None and str(old).strip() and not overwrite:
            continue
        if str(old or "") == str(new):
            continue
        todo[name] = str(new)
        changed.append((name, old, str(new)))
    if not todo:
        return []

    def _mutate(audio, family):
        if audio.tags is None:
            audio.add_tags()
        if family == "id3":
            for name, val in todo.items():
                fid, cls = _ID3_FRAMES[name]
                audio.tags.delall(fid)
                audio.tags.add(cls(encoding=3, text=[val]))
        elif family == "vorbis":
            for name, val in todo.items():
                audio.tags[_VORBIS_KEYS[name]] = [val]
        else:
            for name, val in todo.items():
                key = _MP4_KEYS[name]
                if name == "tracknumber":
                    # trkn is (track, total). read() only surfaces the
                    # track half, so a hardcoded 0 total was unrecoverable
                    # by restore. Keep whatever total the file has.
                    try:
                        track = int(val)
                    except ValueError:
                        continue
                    existing = (audio.tags.get(key) or [(0, 0)])[0]
                    total = existing[1] if len(existing) > 1 else 0
                    audio.tags[key] = [(track, total)]
                elif key.startswith("----"):
                    audio.tags[key] = [MP4FreeForm(val.encode("utf-8"))]
                else:
                    audio.tags[key] = [val]

    _atomic_save(path, _mutate)

    # Report stored-against-old, not intent. A container can drop a value
    # it cannot store (and with overwrite=True destroy the old one), or
    # normalise it -- reporting the request would journal a write that
    # never happened, or one the file does not actually contain.
    after = read(path)
    out = []
    for name, old, _requested in changed:
        stored = str(after.get(name) or "")
        if stored.strip() != str(old or "").strip():
            out.append((name, old, stored))
    return out


def delete_fields(path: Path, names: list[str]) -> None:
    """Used by restore: remove fields that were empty pre-run."""
    def _mutate(audio, family):
        if audio.tags is None:
            return
        for name in names:
            if family == "id3":
                audio.tags.delall(_ID3_FRAMES[name][0])
            elif family == "vorbis":
                for key in (_VORBIS_KEYS[name], _VORBIS_KEYS[name].lower()):
                    if key in audio.tags:
                        del audio.tags[key]
            else:
                audio.tags.pop(_MP4_KEYS[name], None)

    _atomic_save(path, _mutate)


# -- art ---------------------------------------------------------------------

def read_art(path: Path) -> tuple[bytes, str] | None:
    audio, family = _open(path)
    if family == "id3":
        apics = audio.tags.getall("APIC") if audio.tags else []
        if apics:
            return bytes(apics[0].data), apics[0].mime or "image/jpeg"
    elif family == "vorbis":
        if isinstance(audio, FLAC):
            if audio.pictures:
                p = audio.pictures[0]
                return bytes(p.data), p.mime or "image/jpeg"
        else:
            blocks = (audio.tags or {}).get("metadata_block_picture") or []
            if blocks:
                pic = Picture(base64.b64decode(blocks[0]))
                return bytes(pic.data), pic.mime or "image/jpeg"
    else:
        covr = (audio.tags or {}).get("covr") or []
        if covr:
            mime = ("image/png" if covr[0].imageformat == MP4Cover.FORMAT_PNG
                    else "image/jpeg")
            return bytes(covr[0]), mime
    return None


def embed_art(path: Path, data: bytes, mime: str) -> None:
    def _mutate(audio, family):
        if family == "id3":
            if audio.tags is None:
                audio.add_tags()
            audio.tags.delall("APIC")
            audio.tags.add(APIC(encoding=3, mime=mime, type=3,
                                desc="Front cover", data=data))
        elif family == "vorbis":
            pic = Picture()
            pic.type = 3
            pic.mime = mime
            pic.desc = "Front cover"
            pic.data = data
            _set_dimensions(pic, data)
            if isinstance(audio, FLAC):
                audio.clear_pictures()
                audio.add_picture(pic)
            else:
                if audio.tags is None:
                    audio.add_tags()
                audio.tags["metadata_block_picture"] = [
                    base64.b64encode(pic.write()).decode("ascii")]
        else:
            if audio.tags is None:
                audio.add_tags()
            fmt = (MP4Cover.FORMAT_PNG if mime == "image/png"
                   else MP4Cover.FORMAT_JPEG)
            audio.tags["covr"] = [MP4Cover(data, imageformat=fmt)]

    _atomic_save(path, _mutate)


def remove_art(path: Path) -> None:
    def _mutate(audio, family):
        if family == "id3" and audio.tags is not None:
            audio.tags.delall("APIC")
        elif family == "vorbis":
            if isinstance(audio, FLAC):
                audio.clear_pictures()
            # A Vorbis comment block is a list subclass, so its pop() is
            # list.pop(index): a key plus a default raises TypeError.
            elif audio.tags is not None and "metadata_block_picture" in audio.tags:
                del audio.tags["metadata_block_picture"]
        elif audio.tags is not None:
            audio.tags.pop("covr", None)

    _atomic_save(path, _mutate)


def _set_dimensions(pic: Picture, data: bytes) -> None:
    try:
        from PIL import Image
        with Image.open(io.BytesIO(data)) as im:
            pic.width, pic.height = im.size
            pic.depth = 24
    except Exception:
        pass


def image_min_px(data: bytes) -> int | None:
    try:
        from PIL import Image
        with Image.open(io.BytesIO(data)) as im:
            return min(im.size)
    except Exception:
        return None


def sniff_mime(data: bytes) -> str:
    return "image/png" if data[:8] == b"\x89PNG\r\n\x1a\n" else "image/jpeg"


_SIDECAR_NAMES = (
    "cover.jpg", "cover.jpeg", "cover.png",
    "folder.jpg", "folder.jpeg", "folder.png",
    "front.jpg", "front.jpeg", "front.png",
)
_IMAGE_SUFFIXES = (".jpg", ".jpeg", ".png", ".bmp")
_ART_SUBDIR_NAMES = ("cover", "covers", "scan", "scans", "artwork", "art",
                    "booklet", "images", "image")


def _sidecar_in(directory: Path) -> Path | None:
    """One directory, two tiers: a generically-named cover file first, then —
    only when nothing generic matches — a lone unambiguous image. Scene rips
    routinely name the cover after the release itself ("Various Artists -
    Album.jpg"), which no fixed name list can enumerate; requiring the
    image to be the *only* one in the directory keeps that fallback safe —
    two or more untitled images means a real ambiguity, not a guess to make.
    """
    if not directory.is_dir():
        return None
    entries = list(directory.iterdir())
    for name in _SIDECAR_NAMES:
        for candidate in entries:
            if candidate.name.lower() == name:
                return candidate
    images = [p for p in entries
             if p.is_file() and p.suffix.lower() in _IMAGE_SUFFIXES]
    return images[0] if len(images) == 1 else None


def find_sidecar_art(directory: Path) -> Path | None:
    """Cover art next to the audio, checking this directory and — only if
    nothing is found here — every immediately-named-for-art subdirectory one
    level down (Cover/, Scans/, Artwork/, ...). Found live: a 5-disc scene
    rip kept each disc's own cover beside its tracks (handled by the
    single-image fallback above) plus a separate Cover/ subfolder holding
    Front.jpg, Back.jpg, and per-disc art alongside unrelated extras — the
    generic-name tier picks Front.jpg out of that mixed set correctly, and
    the depth is capped at one level so this never turns into a filesystem
    crawl.
    """
    hit = _sidecar_in(directory)
    if hit:
        return hit
    if not directory.is_dir():
        return None
    for sub in sorted(directory.iterdir()):
        if sub.is_dir() and sub.name.lower() in _ART_SUBDIR_NAMES:
            hit = _sidecar_in(sub)
            if hit:
                return hit
    return None


def is_zero_byte(path: Path) -> bool:
    try:
        return path.stat().st_size == 0
    except OSError:
        return True
