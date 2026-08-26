"""Stage 1: wav2flac.

WAV has no good home for tags; FLAC is lossless, so conversion costs nothing
but time. Verification is deliberately doubled:

1. `flac -V --best` decodes as it encodes and compares against the input —
   the tool's own verification primitive.
2. Independently, md5 of `ffmpeg -i original -f s32le -` must equal md5 of
   the same decode of the result. The sample format is pinned so bit-depth
   differences cannot mask a mismatch.

The second check exists so a bug in the first cannot pass silently.

`flac` has no encoder for float-format WAV (`pcm_f32le`/`pcm_f64le`) at all —
it fails immediately with "unsupported format type 3", on otherwise perfectly
good audio. Found on a real 96kHz/float32 Pink Floyd rip: 11/11 files hit this
and none are corrupt. For those, and only those, this stage requantizes
float -> 24-bit int via ffmpeg first, then flac-encodes the intermediate. That
requantization is genuinely lossy at the LSB level, so the exact-md5 check
does not apply to this path; instead both decodes are compared as float64 PCM
with a tolerance of one 24-bit quantization step, streamed in chunks so a
400MB high-res file is never fully loaded into memory. Every requantized file
is journaled with evidence={"requantized": "f32->s24"} so this is auditable,
never silent.

Existing ID3 chunks in the WAV (tags and embedded art) migrate to the FLAC.
Originals move to <workdir>/quarantine/wav/ preserving relative path.
Nothing is ever deleted.
"""

from __future__ import annotations

import array
import hashlib
import shutil
import subprocess
import sys

from .. import probe
from ..journal import Record
from . import Context, guarded_write

_PCM_TOLERANCE = 1.0 / (1 << 22)  # ~ one 24-bit quantization step


def _decoded_md5(path) -> str:
    proc = subprocess.Popen(
        ["ffmpeg", "-v", "error", "-i", str(path), "-f", "s32le", "-"],
        stdout=subprocess.PIPE)
    h = hashlib.md5()
    assert proc.stdout is not None
    for chunk in iter(lambda: proc.stdout.read(1 << 20), b""):
        h.update(chunk)
    if proc.wait() != 0:
        raise RuntimeError(f"ffmpeg decode failed for {path}")
    return h.hexdigest()


def _sample_fmt(path) -> str:
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a:0",
         "-show_entries", "stream=sample_fmt", "-of",
         "default=nw=1:nk=1", str(path)],
        capture_output=True, text=True)
    return r.stdout.strip()


def _pcm_close_enough(path_a, path_b, eps=_PCM_TOLERANCE,
                      chunk_bytes=1 << 20) -> bool:
    """Stream-compare two files' decoded PCM as float64 samples within a
    tolerance, never holding more than chunk_bytes of either decode in memory
    at once. Used only for the float-WAV requantization path, where an exact
    byte comparison would always fail on LSB rounding."""
    pa = subprocess.Popen(
        ["ffmpeg", "-v", "error", "-i", str(path_a), "-f", "f64le", "-"],
        stdout=subprocess.PIPE)
    pb = subprocess.Popen(
        ["ffmpeg", "-v", "error", "-i", str(path_b), "-f", "f64le", "-"],
        stdout=subprocess.PIPE)
    assert pa.stdout is not None and pb.stdout is not None
    la = lb = b""
    eof_a = eof_b = False
    ok = True
    try:
        while ok and not (eof_a and eof_b):
            if not eof_a:
                c = pa.stdout.read(chunk_bytes)
                if c:
                    la += c
                else:
                    eof_a = True
            if not eof_b:
                c = pb.stdout.read(chunk_bytes)
                if c:
                    lb += c
                else:
                    eof_b = True
            n = min(len(la), len(lb))
            n -= n % 8
            if not n:
                continue
            arr_a, arr_b = array.array("d"), array.array("d")
            arr_a.frombytes(la[:n])
            arr_b.frombytes(lb[:n])
            if sys.byteorder != "little":
                arr_a.byteswap()
                arr_b.byteswap()
            if any(abs(x - y) > eps for x, y in zip(arr_a, arr_b, strict=False)):
                ok = False
            la, lb = la[n:], lb[n:]
        if ok and (la or lb):
            ok = False  # unequal total sample count
    finally:
        pa.stdout.close()
        pb.stdout.close()
        if pa.wait() != 0 or pb.wait() != 0:
            ok = False
    return ok


def _encode_float_wav(src, dst, tmp_dir) -> tuple[bool, dict]:
    """Requantize float PCM to 24-bit int, flac-encode that, then verify the
    whole pipeline end to end against the original. Returns (ok, evidence)."""
    tmp_dir.mkdir(parents=True, exist_ok=True)
    intermediate = tmp_dir / (src.stem + ".s24.wav")
    try:
        r = subprocess.run(
            ["ffmpeg", "-v", "error", "-y", "-i", str(src),
             "-c:a", "pcm_s24le", str(intermediate)],
            capture_output=True, text=True)
        if r.returncode != 0:
            return False, {"reason": "ffmpeg requantize failed",
                          "stderr": r.stderr[-500:]}

        r = subprocess.run(["flac", "-V", "--best", "-s", "-o", str(dst),
                            str(intermediate)], capture_output=True, text=True)
        if r.returncode != 0:
            return False, {"reason": "flac -V failed on requantized audio",
                          "stderr": r.stderr[-500:]}

        if not _pcm_close_enough(src, dst):
            return False, {"reason": "PCM mismatch beyond requantization "
                                     "tolerance"}
        return True, {"requantized": "f32->s24"}
    finally:
        intermediate.unlink(missing_ok=True)


def run(ctx: Context) -> None:
    if shutil.which("flac") is None or shutil.which("ffmpeg") is None:
        ctx.say("convert: needs `flac` and `ffmpeg` on PATH; skipping")
        return
    from . import census
    wavs = [r for r in census.load(ctx)
            if r["container"] == "wav" and not r["issue"]]
    quarantine = ctx.workdir / "quarantine" / "wav"
    tmp_dir = ctx.workdir / "tmp" / "convert"
    done = 0
    for row in wavs:
        if ctx.limit and done >= ctx.limit:
            break
        src = ctx.root / row["path"]
        if not src.exists() or not ctx.within_scope(src):
            continue
        dst = src.with_suffix(".flac")
        if dst.exists():
            ctx.journal.append(Record(stage="convert", path=row["path"],
                                      action="skip",
                                      evidence={"reason": "flac exists"}))
            continue
        if not ctx.apply:
            ctx.journal.append(Record(stage="convert", path=row["path"],
                                      action="propose", field="container",
                                      old="wav", new="flac"))
            done += 1
            continue

        fmt = _sample_fmt(src)
        if fmt.startswith(("flt", "dbl")):
            ok, evidence = _encode_float_wav(src, dst, tmp_dir)
            if not ok:
                dst.unlink(missing_ok=True)
                ctx.journal.append(Record(stage="convert", path=row["path"],
                                          action="reject", evidence=evidence))
                continue
        else:
            r = subprocess.run(["flac", "-V", "--best", "-s", "-o", str(dst),
                                str(src)], capture_output=True, text=True)
            if r.returncode != 0:
                dst.unlink(missing_ok=True)
                ctx.journal.append(Record(stage="convert", path=row["path"],
                                          action="reject",
                                          evidence={"reason": "flac -V failed",
                                                    "stderr": r.stderr[-500:]}))
                continue
            if _decoded_md5(src) != _decoded_md5(dst):
                dst.unlink(missing_ok=True)
                ctx.journal.append(Record(stage="convert", path=row["path"],
                                          action="reject",
                                          evidence={"reason":
                                                    "PCM md5 mismatch"}))
                continue
            evidence = {}

        # Migrate tags and art from the WAV. Failing is survivable (the
        # original is quarantined, not deleted) but must be recorded, or
        # the user gets an untagged FLAC with nothing saying why.
        try:
            tags = probe.read(src)
        except probe.ProbeError as e:
            tags = None
            ctx.journal.append(Record(
                stage="convert", path=row["path"], action="skip",
                field="tags",
                evidence={"reason": f"could not read tags to migrate: {e}"}))
        if tags is not None:
            values = {f: tags.get(f) for f in probe.FIELDS if tags.get(f)}
            if values:
                guarded_write(ctx, "convert", row["path"], probe.write,
                              dst, values)
            try:
                art = probe.read_art(src) if tags.has_art else None
            except probe.ProbeError as e:
                art = None
                ctx.journal.append(Record(
                    stage="convert", path=row["path"], action="skip",
                    field="art",
                    evidence={"reason": f"could not read art to migrate: {e}"}))
            if art:
                guarded_write(ctx, "convert", row["path"], probe.embed_art,
                              dst, art[0], art[1])

        qdst = quarantine / row["path"]
        qdst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(qdst))
        ctx.journal.record_write("convert", ctx.root, dst, "container",
                                 "wav", "flac",
                                 evidence={"quarantined": str(qdst), **evidence})
        done += 1
    ctx.say(f"convert: {done} wav handled "
            f"({'applied' if ctx.apply else 'dry run'})")
    if ctx.apply and done:
        from . import census as c
        c.run(ctx)  # converted files re-enter the census for later stages
