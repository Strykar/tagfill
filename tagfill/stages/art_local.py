"""Stage 2: art-local. No network.

Two free sources, in order:

1. A sidecar image in the same directory (cover/folder/front, jpg/png).
2. Art already embedded in a sibling audio file in the same directory —
   album folders are frequently partially tagged, and this harvests the
   existing answer instead of asking the network for it.

Art policy: "missing" means absent **or below the configured minimum size**.
If this stage embedded whatever low-resolution thumbnail was lying around and
the policy elsewhere is never-overwrite-non-empty, a 200px `cover.jpg` would
permanently block the network stages from supplying real art. So undersized
embedded art still counts as a target, undersized candidates are rejected with
their dimensions recorded, and every embed journals the pixel size so upgrades
are auditable.

Images are validated through PIL. BMP sidecars are re-encoded to JPEG.
"""

from __future__ import annotations

from pathlib import Path

from .. import probe
from ..journal import Record
from . import Context, StagePrecondition, guarded_write


def _load_image(data: bytes, min_px: int) -> tuple[bytes, str, int] | None:
    """Validate; returns (data, mime, min_edge_px) or None if unusable.

    One line, because network art has to get exactly this treatment and
    two validators would drift apart.
    """
    return probe.validate_art(data, min_px)


def row_needs_art(row: dict, art_min_px: int) -> bool:
    """Absent, or present but below threshold. Census-row form.

    This is the single definition of "missing art" and every stage that
    selects targets must use it. Gating on the raw `has_art` column instead
    silently excludes undersized-art files from selection, which makes the
    per-file `needs_art()` guard downstream unreachable and lets one 200px
    thumbnail block every network stage forever.
    """
    if not row.get("has_art"):
        return True
    px = row.get("art_min_px")
    try:
        return int(px) < art_min_px
    except (TypeError, ValueError):
        return False


def needs_art(path, art_min_px: int) -> bool:
    """Absent, or present but below threshold. Filesystem form, used as a
    late re-check once a stage has already selected a target."""
    try:
        art = probe.read_art(path)
    except probe.ProbeError:
        return True             # whatever is in there, it is not usable art
    if art is None:
        return True
    px = probe.image_min_px(art[0])
    return px is not None and px < art_min_px


def run(ctx: Context) -> None:
    if not probe.pillow_available():
        raise StagePrecondition(
            "needs pillow to decide whether an image is usable "
            "(pip install 'tagfill[network]', or just pillow)")
    from . import census
    rows = [r for r in census.load(ctx) if not r["issue"]]
    min_px = ctx.cfg.art_min_px
    targets = [r for r in rows if row_needs_art(r, min_px)]
    done = 0
    sibling_cache: dict[str, list[tuple[Path, tuple | None]]] = {}

    for row in targets:
        if ctx.limit and done >= ctx.limit:
            break
        path = ctx.root / row["path"]
        if not path.exists() or not ctx.within_scope(path):
            continue

        candidate = None
        source = None
        sidecar = probe.find_sidecar_art(path.parent)
        if sidecar:
            loaded = _load_image(sidecar.read_bytes(), min_px)
            if loaded:
                candidate, source = loaded, f"sidecar:{sidecar.name}"
            else:
                ctx.journal.append(Record(
                    stage="art-local", path=row["path"], action="reject",
                    field="art",
                    evidence={"reason": "sidecar unusable or under "
                                        f"{min_px}px", "sidecar": sidecar.name}))
        if candidate is None:
            # Cache what the directory has, then filter out "self" at use
            # time. Excluding self *while* caching makes the answer depend
            # on which file happened to be processed first.
            key = str(path.parent)
            if key not in sibling_cache:
                found = []
                for sib in sorted(path.parent.iterdir()):
                    if sib.suffix.lower() not in probe._CONTAINERS:
                        continue
                    try:
                        art = probe.read_art(sib)
                    except probe.ProbeError:
                        continue
                    if art:
                        found.append((sib, _load_image(art[0], min_px)))
                sibling_cache[key] = found
            usable = next((loaded for sib, loaded in sibling_cache[key]
                          if sib != path and loaded), None)
            if usable:
                candidate, source = usable, "sibling"
            else:
                undersized = next((sib.name for sib, loaded
                                   in sibling_cache[key]
                                   if sib != path and not loaded), None)
                if undersized:
                    # Same silent gap as the sidecar branch above and the
                    # network stages' art fetches: a sibling had real
                    # embedded art, it just didn't clear min_px.
                    ctx.journal.append(Record(
                        stage="art-local", path=row["path"],
                        action="reject", field="art",
                        evidence={"reason": f"sibling art under {min_px}px",
                                  "sibling": undersized}))
        if candidate is None:
            continue

        data, mime, px = candidate
        if not ctx.apply:
            ctx.journal.append(Record(stage="art-local", path=row["path"],
                                      action="propose", field="art",
                                      old=row["art_min_px"] or None,
                                      new=f"{px}px",
                                      evidence={"source": source}))
        else:
            ok, _ = guarded_write(ctx, "art-local", row["path"],
                                  probe.embed_art, path, data, mime)
            if ok:
                ctx.journal.record_write("art-local", ctx.root, path, "art",
                                         row["art_min_px"] or None, f"{px}px",
                                         evidence={"source": source})
        done += 1
    ctx.say(f"art-local: {done} embeds "
            f"({'applied' if ctx.apply else 'proposed'})")
