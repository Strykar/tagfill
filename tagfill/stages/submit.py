"""Stage 9: acoustid-submit. Optional, opt-in, off by default.

During stage 5, some fingerprints match AcoustID with an **empty** recordings
list: the fingerprint is known but linked to no MusicBrainz recording. Once
later stages establish artist and title for such a file, that pairing can be
submitted back so the next person's lookup succeeds.

Two dependencies this stage is honest about:

1. Submission requires an AcoustID **user** key (from your acoustid.org
   account) in addition to the application key. Set `$ACOUSTID_USER_KEY`.
2. Linking to a MusicBrainz *recording* requires the recording to exist in
   MusicBrainz. For catalogue that is not there (much Beatport-only
   electronic music), this stage emits `report/mb-additions.csv` — a worklist
   of tracks worth adding to MusicBrainz — rather than pretending metadata
   submission alone closes the loop.
"""

from __future__ import annotations

import csv
import json
import os

from . import Context


def run(ctx: Context) -> None:
    jpath = ctx.workdir / "journal.jsonl"
    if not jpath.exists():
        ctx.say("submit: no journal; run stage 5 first")
        return
    candidates = {}
    with open(jpath) as f:
        for line in f:
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            ev = d.get("evidence") or {}
            if (d.get("stage") == "acoustid"
                    and ev.get("reason") == "empty recordings"):
                candidates[d["path"]] = ev
    if not candidates:
        ctx.say("submit: no empty-recording matches in the journal")
        return

    from . import census
    rows = {r["path"]: r for r in census.load(ctx)}
    ready, worklist = [], []
    for path, ev in candidates.items():
        r = rows.get(path)
        if not r or r["issue"]:
            continue
        if r["artist"] and r["title"]:
            ready.append((path, r, ev))
        else:
            worklist.append(path)

    report_dir = ctx.workdir / "report"
    report_dir.mkdir(parents=True, exist_ok=True)
    with open(report_dir / "mb-additions.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["path", "artist", "title", "note"])
        for path, r, _ in ready:
            w.writerow([path, r["artist"], r["title"],
                        "fingerprint known to AcoustID, recording likely "
                        "missing from MusicBrainz"])

    user_key = os.environ.get("ACOUSTID_USER_KEY", "")
    app_key = ctx.cfg.acoustid_key
    if not (user_key and app_key):
        ctx.say(f"submit: {len(ready)} pairings ready; set $ACOUSTID_USER_KEY "
                "(account key) to submit. Worklist -> report/mb-additions.csv")
        return
    if not ctx.apply:
        ctx.say(f"submit: would submit {len(ready)} pairings (dry run)")
        return

    import subprocess

    import requests

    from ..journal import Record
    n = 0
    for path, r, ev in ready:
        full = ctx.root / path
        res = subprocess.run(["fpcalc", "-json", str(full)],
                             capture_output=True, text=True)
        if res.returncode != 0:
            continue
        fp = json.loads(res.stdout)
        try:
            resp = requests.post("https://api.acoustid.org/v2/submit", data={
                "client": app_key, "user": user_key, "format": "json",
                "duration.0": int(fp["duration"]),
                "fingerprint.0": fp["fingerprint"],
                "track.0": r["title"], "artist.0": r["artist"],
            }, timeout=30)
            ok = resp.json().get("status") == "ok"
        except Exception:
            ok = False
        ctx.journal.append(Record(stage="submit", path=path,
                                  action="apply" if ok else "reject",
                                  evidence={"acoustid": ev.get("acoustid"),
                                            "submitted": ok}))
        n += ok
    ctx.say(f"submit: {n}/{len(ready)} submitted; MusicBrainz worklist -> "
            "report/mb-additions.csv")
