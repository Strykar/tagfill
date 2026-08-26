"""Every art-fetch decline must be journaled, even when the tool doesn't
know why beyond "too small" or "fetch failed" — the point is that
--dry-run and any future --html-report can distinguish "no source found"
from "a source was found and declined", instead of a silent identical
`continue`.

Found live: mb.py fetched real Cover Art Archive art for Pink Floyd's "The
Division Bell" (200x311px, confirmed real by an independent fetch and a
PIL dimension check), correctly declined it as undersized, and logged
nothing — indistinguishable from "no art was ever found" without manually
inspecting the on-disk HTTP cache. The same silent-decline shape existed
in itunes.py (both the size check and a missing "no artwork resolved" case),
and a sibling-search gap in art_local.py.
"""

import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

requests_mock = pytest.importorskip("requests_mock")

from tagfill import config
from tagfill.journal import Journal, ReviewQueue
from tagfill.stages import Context, census


def _ctx(tmp_path, root, **overrides):
    cfg = config.Config()
    cfg.root = root
    cfg.workdir = tmp_path
    for k, v in overrides.items():
        setattr(cfg, k, v)
    return Context(cfg=cfg, journal=Journal(cfg.workdir),
                  review=ReviewQueue(cfg.workdir), apply=True,
                  overwrite=False, limit=None, subpath=None,
                  backup_tags=False,
                  from_review=None)


def _write_census(ctx, rows):
    ctx.workdir.mkdir(parents=True, exist_ok=True)
    with open(ctx.workdir / "census.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=census.COLUMNS)
        w.writeheader()
        for row in rows:
            full = dict.fromkeys(census.COLUMNS, "")
            full.update(row)
            w.writerow(full)


def _journal_records(ctx):
    import json
    with open(ctx.workdir / "journal.jsonl") as f:
        return [json.loads(line) for line in f]


def _tiny_jpeg():
    # A real, tiny (10x10) valid JPEG, well under any sane art_min_px.
    import io

    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (10, 10), (200, 60, 40)).save(buf, "JPEG")
    return buf.getvalue()


def _real_mp3(path, seconds=1):
    import shutil as _shutil
    import subprocess
    if _shutil.which("ffmpeg") is None:
        pytest.skip("needs ffmpeg to build a real fixture mp3")
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi",
                    "-i", f"sine=frequency=440:duration={seconds}",
                    "-c:a", "libmp3lame", "-b:a", "64k", str(path)],
                  check=True)


def test_mb_logs_undersized_caa_art_not_silence(tmp_path, monkeypatch):
    from tagfill.sources import musicbrainz as mb_source
    from tagfill.stages import mb

    root = tmp_path / "music"
    (root / "Some Album").mkdir(parents=True)
    _real_mp3(root / "Some Album" / "01 Track.mp3", seconds=1)
    ctx = _ctx(tmp_path, root, mb_contact="test@example.org")
    _write_census(ctx, [{"path": "Some Album/01 Track.mp3", "container": "mp3",
                        "size": 1, "mtime": "0", "artist": "Some Artist",
                        "album": "Some Album", "duration": "200"}])

    class FakeMB:
        @staticmethod
        def set_useragent(*a, **k):
            pass

        @staticmethod
        def search_releases(**k):
            return {"release-list": [{"id": "abc123"}]}

        @staticmethod
        def get_release_by_id(rid, includes=None):
            return {"release": {
                "id": rid, "title": "Some Album",
                "medium-list": [{"track-list": [
                    {"recording": {"length": 200000}}]}],
            }}

    monkeypatch.setitem(sys.modules, "musicbrainzngs", FakeMB())
    monkeypatch.setattr(mb_source, "_http_cached",
                        lambda *a, **k: _tiny_jpeg())

    mb.run(ctx)

    recs = [r for r in _journal_records(ctx) if r.get("field") == "art"]
    assert recs, "an art decision (accept or reject) must be journaled"
    assert recs[0]["action"] == "reject"
    assert recs[0]["evidence"]["reason"] == "undersized"


def test_itunes_logs_undersized_art(tmp_path, monkeypatch):
    from tagfill.stages import itunes

    root = tmp_path / "music"
    (root / "Some Album").mkdir(parents=True)
    _real_mp3(root / "Some Album" / "01 Track.mp3", seconds=1)
    ctx = _ctx(tmp_path, root)
    _write_census(ctx, [{"path": "Some Album/01 Track.mp3", "container": "mp3",
                        "size": 1, "mtime": "0", "artist": "Some Artist",
                        "album": "Some Album"}])

    with requests_mock.Mocker() as m:
        m.get("https://itunes.apple.com/search",
             json={"results": [{"artistName": "Some Artist",
                               "collectionName": "Some Album",
                               "artworkUrl100": "https://art.example/100x100bb.jpg"}]})
        m.get("https://art.example/1200x1200bb.jpg", content=_tiny_jpeg())
        m.get("https://art.example/600x600bb.jpg", content=_tiny_jpeg())
        itunes.run(ctx)

    recs = [r for r in _journal_records(ctx)
           if r.get("stage") == "itunes" and r.get("action") == "reject"]
    assert any(r["evidence"].get("reason") == "undersized" for r in recs)


def test_art_local_logs_undersized_sibling_art(tmp_path):
    from tagfill.stages import art_local

    root = tmp_path / "music"
    album = root / "Some Album"
    album.mkdir(parents=True)

    import shutil as _shutil
    import subprocess

    from mutagen.id3 import APIC
    from mutagen.mp3 import MP3
    if _shutil.which("ffmpeg") is None:
        pytest.skip("needs ffmpeg to build a real fixture mp3")
    tagged = album / "01 Tagged.mp3"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi",
                    "-i", "sine=frequency=440:duration=1",
                    "-c:a", "libmp3lame", "-b:a", "64k", str(tagged)],
                  check=True)
    audio = MP3(tagged)
    if audio.tags is None:
        audio.add_tags()
    audio.tags.add(APIC(encoding=3, mime="image/jpeg", type=3,
                        desc="", data=_tiny_jpeg()))
    audio.save()
    untagged = album / "02 Untagged.mp3"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi",
                    "-i", "sine=frequency=440:duration=1",
                    "-c:a", "libmp3lame", "-b:a", "64k", str(untagged)],
                  check=True)

    ctx = _ctx(tmp_path, root)
    _write_census(ctx, [
        {"path": "Some Album/01 Tagged.mp3", "container": "mp3",
         "size": 1, "mtime": "0", "has_art": "1", "art_min_px": "10"},
        {"path": "Some Album/02 Untagged.mp3", "container": "mp3",
         "size": 1, "mtime": "0", "has_art": ""},
    ])

    art_local.run(ctx)

    recs = [r for r in _journal_records(ctx) if r.get("stage") == "art-local"]
    assert any(r["action"] == "reject"
              and "sibling art under" in r["evidence"].get("reason", "")
              for r in recs)
