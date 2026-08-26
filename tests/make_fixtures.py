"""Synthesize a fixture collection covering every container and defect class.

No real music: two-second sine tones via ffmpeg, art via PIL. The layout
mirrors the defect classes the stages claim to cover:

    fixtures/
      Tagged Album/            two mp3s fully tagged, no art, cover.jpg sidecar
      Partial Album/           two flacs, same album, one with art one without
      DJ Pool/Some Crate/      untagged flac `Artist - Title [Label]`,
                               untagged wav with a Camelot/BPM prefix
      Singles/                 tagged aiff with art (the finding-1 regression),
                               art-missing ogg, zero-byte mp3

Run: python tests/make_fixtures.py [dest]   (default: tests/fixtures)
"""

from __future__ import annotations

import io
import subprocess
import sys
from pathlib import Path


def _tone(dest: Path, codec_args: list[str], freq: int = 440) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-f", "lavfi",
         "-i", f"sine=frequency={freq}:duration=2", *codec_args, str(dest)],
        check=True)


def _vorbis_args() -> list[str]:
    """Homebrew's ffmpeg ships without libvorbis, so the macOS CI job could
    not build the ogg fixture at all. ffmpeg's own vorbis encoder is there
    instead; it is marked experimental and refuses anything but stereo,
    which is why -ac 2 and -strict -2 come with it. Either way the file is
    a two-second sine tone."""
    encoders = subprocess.run(["ffmpeg", "-v", "error", "-encoders"],
                              capture_output=True, text=True, check=True).stdout
    if "libvorbis" in encoders:
        return ["-c:a", "libvorbis"]
    if " vorbis " in encoders:
        return ["-ac", "2", "-c:a", "vorbis", "-strict", "-2"]
    raise SystemExit("this ffmpeg has no vorbis encoder, libvorbis or "
                     "otherwise, so the ogg fixture cannot be built")


def _art_bytes(px: int = 600) -> bytes:
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (px, px), (200, 60, 40)).save(buf, "JPEG", quality=90)
    return buf.getvalue()


def build(root: Path) -> None:
    from mutagen.aiff import AIFF
    from mutagen.flac import FLAC
    from mutagen.id3 import APIC, TALB, TIT2, TPE1
    from mutagen.mp3 import MP3
    from mutagen.oggvorbis import OggVorbis

    root.mkdir(parents=True, exist_ok=True)
    art = _art_bytes()

    # Tagged Album: mp3s with tags, no art, sidecar cover.jpg
    for i, title in enumerate(["Opening", "Closing"], 1):
        p = root / "Tagged Album" / f"0{i} {title}.mp3"
        _tone(p, ["-c:a", "libmp3lame", "-b:a", "128k"], 330 + i * 110)
        audio = MP3(p)
        if audio.tags is None:
            audio.add_tags()
        audio.tags.add(TPE1(encoding=3, text=["Fixture Artist"]))
        audio.tags.add(TIT2(encoding=3, text=[title]))
        audio.tags.add(TALB(encoding=3, text=["Tagged Album"]))
        audio.save()
    (root / "Tagged Album" / "cover.jpg").write_bytes(art)

    # Partial Album: two flacs, one has art
    for i, (title, with_art) in enumerate(
            [("First", True), ("Second", False)], 1):
        p = root / "Partial Album" / f"0{i} {title}.flac"
        _tone(p, ["-c:a", "flac"], 300 + i * 90)
        audio = FLAC(p)
        audio["artist"], audio["title"] = ["Partial Artist"], [title]
        audio["album"] = ["Partial Album"]
        audio.save()
        if with_art:
            sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
            from tagfill import probe
            probe.embed_art(p, art, "image/jpeg")

    # DJ Pool crate: untagged, parseable filenames
    _tone(root / "DJ Pool" / "Some Crate" /
          "Dysmorph - Quietus Korero [Fixture Recs].flac", ["-c:a", "flac"], 520)
    _tone(root / "DJ Pool" / "Some Crate" /
          "2A - 128 - 03 Kliment Serial Spider.wav",
          ["-c:a", "pcm_s16le"], 480)

    # Singles: the finding-1 regression AIFF, fully tagged with art
    p = root / "Singles" / "Aiff Artist - Fully Tagged.aiff"
    _tone(p, ["-c:a", "pcm_s16be"], 550)
    audio = AIFF(p)
    audio.add_tags()
    audio.tags.add(TPE1(encoding=3, text=["Aiff Artist"]))
    audio.tags.add(TIT2(encoding=3, text=["Fully Tagged"]))
    audio.tags.add(TALB(encoding=3, text=["Aiff Album"]))
    audio.tags.add(APIC(encoding=3, mime="image/jpeg", type=3,
                        desc="Front cover", data=art))
    audio.save()

    # Art-missing ogg, tagged
    p = root / "Singles" / "Ogg Artist - No Art.ogg"
    _tone(p, _vorbis_args(), 600)
    audio = OggVorbis(p)
    audio["artist"], audio["title"] = ["Ogg Artist"], ["No Art"]
    audio.save()

    # Zero-byte casualty
    z = root / "Singles" / "corrupted.mp3"
    z.parent.mkdir(parents=True, exist_ok=True)
    z.touch()

    # AppleDouble stub that a raw find would miscount
    (root / "Singles" / "._corrupted.mp3").write_bytes(b"\x00" * 4096)

    print(f"fixtures built under {root}")


if __name__ == "__main__":
    build(Path(sys.argv[1]) if len(sys.argv) > 1 else
          Path(__file__).parent / "fixtures")
