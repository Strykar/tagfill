"""Regression test for the float-PCM WAV conversion path.

`flac` has no encoder for float-format WAV at all: it fails immediately with
"unsupported format type 3" on otherwise perfectly good audio. Found on a
real 96kHz/float32 collection (11/11 files of one album), none corrupt.
convert.py requantizes float -> 24-bit int via ffmpeg first, then flac-encodes
that, verifying the whole pipeline end to end with a tolerance-based PCM
compare instead of the exact-md5 check used for the integer-WAV path.

Needs ffmpeg and flac on PATH; skips cleanly otherwise.
"""

import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

HAVE_TOOLS = shutil.which("ffmpeg") is not None and shutil.which("flac") is not None
needs_tools = pytest.mark.skipif(not HAVE_TOOLS, reason="needs ffmpeg + flac")


@pytest.fixture
def float_wav(tmp_path):
    p = tmp_path / "tone_f32.wav"
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-f", "lavfi",
         "-i", "sine=frequency=440:duration=1", "-c:a", "pcm_f32le", str(p)],
        check=True)
    return p


@needs_tools
def test_float_wav_is_detected(float_wav):
    from tagfill.stages.convert import _sample_fmt
    assert _sample_fmt(float_wav).startswith("flt")


@needs_tools
def test_int_wav_is_not_flagged_float(tmp_path):
    from tagfill.stages.convert import _sample_fmt
    p = tmp_path / "tone_s16.wav"
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-f", "lavfi",
         "-i", "sine=frequency=440:duration=1", "-c:a", "pcm_s16le", str(p)],
        check=True)
    fmt = _sample_fmt(p)
    assert not fmt.startswith("flt") and not fmt.startswith("dbl")


@needs_tools
def test_float_wav_converts_and_verifies(float_wav, tmp_path):
    """This is the case that used to hard-fail: flac -V on a float WAV
    directly errors out with 'unsupported format type 3'."""
    from tagfill.stages.convert import _encode_float_wav
    dst = tmp_path / "tone.flac"
    ok, evidence = _encode_float_wav(float_wav, dst, tmp_path / "tmp")
    assert ok, evidence
    assert evidence == {"requantized": "f32->s24"}
    assert dst.exists()


@needs_tools
def test_pcm_close_enough_catches_wrong_content(tmp_path):
    from tagfill.stages.convert import _pcm_close_enough
    a = tmp_path / "a.wav"
    b = tmp_path / "b.wav"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi",
                    "-i", "sine=frequency=440:duration=1",
                    "-c:a", "pcm_f32le", str(a)], check=True)
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi",
                    "-i", "sine=frequency=880:duration=1",
                    "-c:a", "pcm_f32le", str(b)], check=True)
    assert _pcm_close_enough(a, a) is True
    assert _pcm_close_enough(a, b) is False


@needs_tools
def test_pcm_close_enough_catches_length_mismatch(float_wav, tmp_path):
    from tagfill.stages.convert import _pcm_close_enough
    short = tmp_path / "short.wav"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(float_wav),
                    "-t", "0.5", "-c", "copy", str(short)], check=True)
    assert _pcm_close_enough(float_wav, short) is False


@needs_tools
def test_intermediate_wav_is_cleaned_up(float_wav, tmp_path):
    from tagfill.stages.convert import _encode_float_wav
    tmp_dir = tmp_path / "tmp"
    dst = tmp_path / "tone.flac"
    ok, _ = _encode_float_wav(float_wav, dst, tmp_dir)
    assert ok
    assert list(tmp_dir.glob("*.wav")) == []
