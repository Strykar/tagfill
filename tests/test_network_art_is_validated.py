"""Bytes off a CDN get the same scrutiny as bytes off the user's disk.

External review: art_local fully opened local sidecar images, checked the
format and re-encoded BMPs. The mb and itunes stages embedded whatever the
network returned after image_min_px (a header read) and sniff_mime (a
two-byte magic check that calls anything not-PNG a JPEG). So an HTML error
page with plausible dimensions, a WEBP, or a malformed JPEG went into the
user's files with a lying MIME -- to be parsed later by every phone,
player and car head unit that renders art, most with far worse image
parsers than Pillow. The trust ordering was backwards.

Image.open only reads a header, so the local validator was weaker than it
read too. im.load() is what makes this a decode.
"""

import io
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

pytest.importorskip("PIL")

from PIL import Image

from tagfill import probe


def _img(fmt="JPEG", px=1000, **kw):
    buf = io.BytesIO()
    Image.new("RGB", (px, px), (10, 120, 90)).save(buf, fmt, **kw)
    return buf.getvalue()


def test_a_real_jpeg_passes_through_untouched():
    data = _img("JPEG")
    out, mime, px = probe.validate_art(data, 500)
    assert out == data and mime == "image/jpeg" and px == 1000


def test_a_real_png_passes_through_untouched():
    data = _img("PNG")
    out, mime, _px = probe.validate_art(data, 500)
    assert out == data and mime == "image/png"


def test_an_html_error_page_is_refused():
    """The realistic one: a CDN 404 body, served with 200."""
    body = b"<!doctype html><html><body>Not Found</body></html>" * 40
    assert probe.validate_art(body, 0) is None


def test_arbitrary_bytes_are_refused():
    assert probe.validate_art(b"fakeartbytes", 0) is None
    assert probe.validate_art(b"", 0) is None


def test_a_truncated_jpeg_is_refused_by_the_decode():
    """Its header is intact and its dimensions look fine, so a header-only
    check accepts it. This is what im.load() is for."""
    data = _img("JPEG")
    truncated = data[:len(data) // 3]
    with Image.open(io.BytesIO(truncated)) as im:
        assert min(im.size) == 1000, "the header still parses; that is the trap"
    assert probe.validate_art(truncated, 500) is None


@pytest.mark.parametrize("fmt", ["BMP", "WEBP", "GIF"])
def test_other_real_formats_are_re_encoded_to_jpeg(fmt):
    out, mime, _px = probe.validate_art(_img(fmt), 500)
    assert mime == "image/jpeg"
    with Image.open(io.BytesIO(out)) as im:
        assert im.format == "JPEG"


def test_something_too_small_is_refused_at_the_threshold():
    assert probe.validate_art(_img("JPEG", px=100), 500) is None
    assert probe.validate_art(_img("JPEG", px=100), 0) is not None


def test_absurdly_large_art_is_refused_without_decoding_it():
    """Bounds what a hostile or broken source can make mutagen hold in
    memory, and what ends up inside every copy of the track."""
    assert probe.validate_art(b"\xff\xd8\xff" + b"\x00"
                              * probe.MAX_ART_BYTES, 0) is None


def test_the_local_and_network_paths_use_the_same_validator():
    """Two validators would drift, and the weaker one would be the one
    facing the network."""
    art_local = (Path(__file__).resolve().parents[1] / "tagfill" / "stages"
                 / "art_local.py").read_text(encoding="utf-8")
    assert "probe.validate_art" in art_local
    for stage in ("mb.py", "itunes.py"):
        src = (Path(__file__).resolve().parents[1] / "tagfill" / "stages"
               / stage).read_text(encoding="utf-8")
        assert "probe.validate_art" in src, f"{stage} skips validation"
        assert "probe.sniff_mime" not in src, (
            f"{stage} still guesses the MIME type instead of decoding")
