#!/usr/bin/env python3
"""Build a synthetic test corpus: real album metadata, silent audio.

The tool matches a folder by comparing its track durations against a
release's tracklist, so a corpus of arbitrary-length files would match
nothing and demonstrate nothing. These files carry the *real* durations of
real releases, fetched from MusicBrainz, but the audio is digital silence
at 8 kHz mono -- a four-minute track is a few KB rather than 40 MB, and no
copyrighted audio is redistributed. Only artist/title/album are tagged;
everything the tool is supposed to fill is deliberately left empty.

    python demo/make_corpus.py --contact you@example.org
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

# Well-catalogued releases with complete duration data. Kept small and
# varied so the corpus exercises several containers.
RELEASES = [
    ("Moby", "Play", "flac"),
    ("Daft Punk", "Homework", "mp3"),
    ("Portishead", "Dummy", "ogg"),
    ("Massive Attack", "Mezzanine", "m4a"),
]

ENCODER = {
    "mp3": ["-c:a", "libmp3lame", "-b:a", "8k"],
    "flac": ["-c:a", "flac"],
    "ogg": ["-c:a", "libvorbis", "-q:a", "-1"],
    "m4a": ["-c:a", "aac", "-b:a", "8k"],
}


def silent(path: Path, seconds: float, fmt: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-f", "lavfi",
         "-i", "anullsrc=r=8000:cl=mono", "-t", f"{seconds:.3f}",
         *ENCODER[fmt], str(path)],
        check=True)


def safe(name: str) -> str:
    return "".join(c if c.isalnum() or c in " -_.,'&()" else "_" for c in name)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--contact", required=True,
                    help="email for the MusicBrainz user agent (their policy)")
    ap.add_argument("--out", type=Path,
                    default=Path(__file__).parent / "Music")
    args = ap.parse_args()

    import musicbrainzngs as mb
    mb.set_useragent("tagfill-corpus", "1.0", args.contact)

    total = 0
    for artist, album, fmt in RELEASES:
        res = mb.search_releases(artist=artist, release=album, limit=5)
        chosen = None
        for cand in res.get("release-list", []):
            rel = mb.get_release_by_id(
                cand["id"], includes=["recordings"])["release"]
            media = rel.get("medium-list", [])
            if len(media) != 1:
                continue                      # keep the corpus single-disc
            tracks = media[0].get("track-list", [])
            if tracks and all(t.get("recording", {}).get("length")
                              for t in tracks):
                chosen = (rel, tracks)
                break
        if not chosen:
            print(f"  !! no complete tracklist for {artist} - {album}")
            continue

        rel, tracks = chosen
        folder = args.out / safe(f"{artist} - {album}")
        for i, t in enumerate(tracks, 1):
            rec = t["recording"]
            secs = int(rec["length"]) / 1000.0
            name = f"{i:02d} {safe(rec['title'])}.{fmt}"
            silent(folder / name, secs, fmt)
            total += 1

        # Tag only what a real under-tagged collection would already have.
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        from tagfill import probe
        for i, t in enumerate(tracks, 1):
            rec = t["recording"]
            p = folder / f"{i:02d} {safe(rec['title'])}.{fmt}"
            probe.write(p, {"artist": artist, "album": rel["title"],
                            "title": rec["title"]})
        print(f"  {len(tracks):3} x {fmt:4}  {artist} - {rel['title']}")

    size = sum(f.stat().st_size for f in args.out.rglob("*") if f.is_file())
    print(f"\n{total} files, {size / 1024:.0f} KB total")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
