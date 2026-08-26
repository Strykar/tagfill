"""Regression test: itunes.py must not treat a folder as one album.

Found live on the real collection: "Downloaded_by_MediaHuman" is a flat
dump of auto-downloaded YouTube rips, one file per unrelated song, no
per-album subfolders. Real content sitting side by side in that one
directory: Daft Punk - Random Access Memories, Deadmau5 - Faxing Berlin,
John Mayer - Live at the Nokia Theatre, Melissa Etheridge - Like The Way I
Do. The stage grouped by folder alone and used only the first eligible
row's artist/album as the search query for the *entire* folder, then
embedded whatever art it found onto every file in that folder still
needing art.

Checked against the real journal: this had already run in three separate
--apply rounds and been rejected every time (similarity 0.2), by luck —
whichever row happened to sort first produced a weak query. A well-known
album as that first row would have produced a high-confidence match and
embedded its cover onto every unrelated track sharing the directory. This
is the one bug from this whole exercise with real data-corruption risk:
every other bug found either declined too conservatively or crashed loudly,
never silently wrote one song's data onto another song's file.
"""

import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

requests_mock = pytest.importorskip("requests_mock")

from tagfill import config
from tagfill.journal import Journal, ReviewQueue
from tagfill.stages import Context, census, itunes


def _ctx(tmp_path, root):
    cfg = config.Config()
    cfg.root = root
    cfg.workdir = tmp_path
    return Context(cfg=cfg, journal=Journal(cfg.workdir),
                  review=ReviewQueue(cfg.workdir), apply=True,
                  overwrite=False, limit=None, subpath=None,
                  backup_tags=False,
                  from_review=None)


def _write_census(ctx, rows):
    ctx.workdir.mkdir(parents=True, exist_ok=True)
    with open(ctx.workdir / "census.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=census.COLUMNS)
        w.writeheader()
        for path, artist, album in rows:
            row = dict.fromkeys(census.COLUMNS, "")
            row.update(path=path, container="mp3", size=100, mtime="0",
                      artist=artist, album=album, has_art="")
            w.writerow(row)


def _real_jpeg(px: int = 1200) -> bytes:
    """A real image, because the stage decodes it now. This used to serve
    b"fakeartbytes" and assert it got embedded -- which was the bug an
    external review flagged: bytes off a CDN were trusted more than bytes
    off the user's own disk, and went into the file with a MIME type
    sniff_mime had guessed."""
    import io

    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (px, px), (90, 40, 120)).save(buf, "JPEG", quality=80)
    return buf.getvalue()


def _mock_itunes(m, artist, album):
    """A search result strong enough to pass both the similarity and margin
    gates, plus an artwork fetch on a separate domain so the two endpoints
    never collide in the mock."""
    m.get("https://itunes.apple.com/search",
         json={"results": [{"artistName": artist, "collectionName": album,
                            "artworkUrl100": "https://artcdn.example/100x100bb.jpg"}]})
    m.get("https://artcdn.example/1200x1200bb.jpg", content=_real_jpeg(),
         headers={"content-type": "image/jpeg"})


def test_two_different_songs_in_one_folder_get_independent_queries(
        tmp_path, monkeypatch):
    """The core fix: grouping key is (folder, artist, album), not folder."""
    root = tmp_path / "music"
    dump = root / "Downloaded_by_MediaHuman"
    dump.mkdir(parents=True)
    for name in ("Daft Punk - Contact.mp3", "John Mayer - Free Fallin.mp3"):
        (dump / name).touch()

    ctx = _ctx(tmp_path, root)
    _write_census(ctx, [
        ("Downloaded_by_MediaHuman/Daft Punk - Contact.mp3",
         "Daft Punk", "Random Access Memories"),
        ("Downloaded_by_MediaHuman/John Mayer - Free Fallin.mp3",
         "John Mayer", "Live at the Nokia Theatre"),
    ])
    monkeypatch.setattr(itunes.probe, "read_art", lambda p: None)
    monkeypatch.setattr(itunes.probe, "image_min_px", lambda d: 500)
    monkeypatch.setattr(itunes.probe, "embed_art", lambda p, d, m: None)

    with requests_mock.Mocker() as m:
        _mock_itunes(m, "Daft Punk", "Random Access Memories")
        itunes.run(ctx)

    search_urls = [r.url for r in m.request_history if "search?" in r.url]
    terms = {u.split("term=")[1] for u in search_urls}
    assert len(terms) == 2, (
        f"expected 2 distinct search queries (one per song), got {terms}")


def test_a_real_album_folder_still_groups_its_tracks_together(
        tmp_path, monkeypatch):
    """The fix must not break the common case: a real album folder where
    every track shares the same artist+album is still one group, one
    search, art applied to every track."""
    root = tmp_path / "music"
    album_dir = root / "Some Artist - Some Album"
    album_dir.mkdir(parents=True)
    for name in ("01 Track One.mp3", "02 Track Two.mp3", "03 Track Three.mp3"):
        (album_dir / name).touch()

    ctx = _ctx(tmp_path, root)
    _write_census(ctx, [
        (f"Some Artist - Some Album/{n}", "Some Artist", "Some Album")
        for n in ("01 Track One.mp3", "02 Track Two.mp3", "03 Track Three.mp3")
    ])
    embedded = []
    monkeypatch.setattr(itunes.probe, "read_art", lambda p: None)
    monkeypatch.setattr(itunes.probe, "image_min_px", lambda d: 500)
    monkeypatch.setattr(itunes.probe, "embed_art",
                        lambda p, d, m: embedded.append(str(p)))

    with requests_mock.Mocker() as m:
        _mock_itunes(m, "Some Artist", "Some Album")
        itunes.run(ctx)

    search_urls = [r.url for r in m.request_history if "search?" in r.url]
    assert len(search_urls) == 1, "one album, one search query"
    assert len(embedded) == 3, "all three tracks of the same album get art"


def test_grouping_key_is_folder_artist_album_not_folder_alone():
    src = Path(__file__).resolve().parents[1] / "tagfill" / "stages" / "itunes.py"
    text = src.read_text(encoding="utf-8")
    assert 'key = (str(Path(r["path"]).parent), r["artist"], r["album"])' in text
    assert 'rows[0]["artist"], rows[0]["album"]' not in text, (
        "the exact bug: using only the first row's artist/album as the "
        "query for the whole folder")
