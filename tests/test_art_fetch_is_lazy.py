"""External review: mb.py called match.fetch_art() the moment a source
verified -- before the ctx.apply check, and before working out whether
anything in the folder was actually short of art.

SourceMatch documents fetch_art as a callable precisely so an unused match
costs nothing, and every source's own test asserts the laziness holds up to
the point it hands the match back. The orchestrator then spent it anyway:
a dry run over a real collection paid a Cover Art Archive request pair per
matched folder, which is both slow and rude to a free service people are
asked to be polite to.
"""

import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from tagfill import config
from tagfill.journal import Journal, ReviewQueue
from tagfill.sources import SourceMatch
from tagfill.stages import Context, census

pytest.importorskip("mutagen")


def _ctx(tmp_path, *, apply):
    cfg = config.Config()
    cfg.root = tmp_path / "Music"
    cfg.workdir = tmp_path / "work"
    cfg.mb_contact = "test@example.org"
    (cfg.root / "Album").mkdir(parents=True, exist_ok=True)
    ctx = Context(cfg=cfg, journal=Journal(cfg.workdir),
                  review=ReviewQueue(cfg.workdir), apply=apply)
    rows = [{"path": f"Album/0{i}.mp3", "container": "mp3", "size": "1",
             "mtime": "0", "duration": d, "artist": "A", "album": "B",
             "has_art": has_art, "art_min_px": "2000" if has_art else ""}
            for i, (d, has_art) in enumerate([("200", ""), ("210", "")], 1)]
    _census(ctx, rows)
    return ctx


def _census(ctx, rows):
    ctx.workdir.mkdir(parents=True, exist_ok=True)
    with open(ctx.workdir / "census.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=census.COLUMNS)
        w.writeheader()
        for row in rows:
            full = dict.fromkeys(census.COLUMNS, "")
            full.update(row)
            w.writerow(full)


def _stub_source(calls):
    def fetch_art():
        calls.append(1)
        return None

    def search(**_kwargs):
        return SourceMatch(
            id="musicbrainz:x", title="B", date="1994", albumartist="A",
            evidence={"source": "musicbrainz", "order": "positional"},
            fetch_art=fetch_art, genre="Techno"), []
    return search


def _run_mb(ctx, monkeypatch, calls):
    from tagfill.sources import discogs, itunes, musicbrainz
    from tagfill.stages import mb
    monkeypatch.setattr(musicbrainz, "search", _stub_source(calls))
    # Nothing left for the fallbacks to fill, so they must not run either.
    for mod in (itunes, discogs):
        monkeypatch.setattr(mod, "search", lambda **_k: (None, []))
    mb.run(ctx)


def test_a_dry_run_never_fetches_art(tmp_path, monkeypatch):
    calls = []
    _run_mb(_ctx(tmp_path, apply=False), monkeypatch, calls)
    assert calls == []


def test_an_apply_run_that_needs_art_does_fetch_it(tmp_path, monkeypatch):
    calls = []
    _run_mb(_ctx(tmp_path, apply=True), monkeypatch, calls)
    assert calls == [1]


def test_an_apply_run_with_art_everywhere_does_not_fetch(tmp_path,
                                                         monkeypatch):
    """The folder is only short of a date, so there is nothing art can fix
    and no reason to spend the request."""
    calls = []
    ctx = _ctx(tmp_path, apply=True)
    with open(ctx.workdir / "census.csv", newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    for r in rows:
        r["has_art"], r["art_min_px"] = "1", "2000"
    _census(ctx, rows)
    _run_mb(ctx, monkeypatch, calls)
    assert calls == []
