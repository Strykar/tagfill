"""Stage 5: AcoustID.

Only files still lacking artist or title after the offline stages. `fpcalc
-json -length 120` computes the fingerprint; a plain HTTPS POST to the v2
lookup endpoint resolves it. No client library is needed.

A result is accepted only when its score clears the configured minimum AND it
carries a non-empty recordings list. Fingerprints that AcoustID knows but has
no MusicBrainz recording for are common with electronic music sold outside
the mainstream catalogue (Beatport et al.); those are recorded separately
rather than counted as failures, because stage 9 can submit the pairing back
once other stages establish artist/title.

Register a free application key at https://acoustid.org/new-application.
"""

from __future__ import annotations

import json
import shutil
import subprocess

from ..journal import Record
from ..util import CAPTURE_TEXT, RateLimiter, is_mb_placeholder
from . import Context, StagePrecondition, guarded_write


def _fingerprint(path) -> tuple[int, str] | None:
    r = subprocess.run(["fpcalc", "-json", "-length", "120", str(path)],
                       **CAPTURE_TEXT)
    if r.returncode != 0:
        return None
    try:
        d = json.loads(r.stdout)
        return int(d["duration"]), d["fingerprint"]
    except (json.JSONDecodeError, KeyError, ValueError):
        return None


def run(ctx: Context) -> None:
    key = ctx.cfg.acoustid_key
    if not key:
        raise StagePrecondition(
            "no API key (config [acoustid], $ACOUSTID_API_KEY, or key_file). "
            "https://acoustid.org/new-application")
    if shutil.which("fpcalc") is None:
        raise StagePrecondition("needs `fpcalc` (chromaprint) on PATH")
    try:
        import requests
    except ImportError:
        raise StagePrecondition("pip install requests") from None

    from .. import probe
    from . import census
    rows = [r for r in census.load(ctx) if not r["issue"]
            and (not r["artist"] or not r["title"])]
    done = 0
    limiter = RateLimiter(ctx.cfg.acoustid_rate_s)
    for row in rows:
        if ctx.limit and done >= ctx.limit:
            break
        path = ctx.root / row["path"]
        if not path.exists() or not ctx.within_scope(path):
            continue
        if not ctx.recheck and ctx.journal.already_done("acoustid",
                                                        ctx.root, path):
            continue
        fp = _fingerprint(path)
        if not fp:
            ctx.journal.append(Record(stage="acoustid", path=row["path"],
                                      action="skip",
                                      evidence={"reason": "fpcalc failed"}))
            continue
        duration, fingerprint = fp
        try:
            limiter.wait()
            r = requests.post("https://api.acoustid.org/v2/lookup", data={
                "client": key, "format": "json",
                "meta": "recordings releasegroups",
                "duration": duration, "fingerprint": fingerprint,
            }, timeout=30)
            data = r.json()
        except Exception as e:
            # One transient hiccup must not cost the rest of the batch.
            # Re-issuing the identical request for the file that tripped
            # this returned a clean 200, so the query itself was fine.
            ctx.journal.append(Record(stage="acoustid", path=row["path"],
                                      action="skip",
                                      evidence={"reason": "lookup failed",
                                                "error": str(e)}))
            continue
        results = sorted(data.get("results", []),
                         key=lambda x: x.get("score", 0), reverse=True)
        best = results[0] if results else None
        if not best or best.get("score", 0) < ctx.cfg.acoustid_min_score:
            ctx.journal.append(Record(
                stage="acoustid", path=row["path"], action="reject",
                evidence={"reason": "no result above score",
                          "best_score": best.get("score") if best else None}))
            done += 1
            continue
        recordings = best.get("recordings") or []
        if not recordings:
            # Known fingerprint, no linked recording: stage 9 material.
            ctx.journal.append(Record(
                stage="acoustid", path=row["path"], action="skip",
                evidence={"reason": "empty recordings",
                          "acoustid": best.get("id"),
                          "score": best.get("score"),
                          "fingerprint_duration": duration}))
            done += 1
            continue
        rec = recordings[0]
        artist = "; ".join(a.get("name", "") for a in rec.get("artists", [])
                           if not is_mb_placeholder(a.get("name"))) or None
        title = rec.get("title")
        if is_mb_placeholder(title):
            title = None
        evidence = {"acoustid": best.get("id"), "score": best.get("score"),
                    "recording": rec.get("id")}
        if not artist and not title:
            ctx.journal.append(Record(
                stage="acoustid", path=row["path"], action="reject",
                evidence={**evidence,
                         "reason": "recording has only MB placeholder "
                                   "artist/title"}))
            done += 1
            continue
        values = {"artist": artist, "title": title}
        if not ctx.apply:
            for f, v in values.items():
                if v:
                    ctx.journal.append(Record(stage="acoustid",
                                              path=row["path"],
                                              action="propose", field=f,
                                              old=row[f] or None, new=v,
                                              evidence=evidence))
        else:
            _ok, changed = guarded_write(ctx, "acoustid", row["path"],
                                        probe.write, path, values,
                                        overwrite=ctx.overwrite)
            for f, old, new in (changed or []):
                ctx.journal.record_write("acoustid", ctx.root, path, f,
                                         old, new, evidence=evidence)
        done += 1
    ctx.say(f"acoustid: {done} files handled "
            f"({'applied' if ctx.apply else 'dry run'})")
