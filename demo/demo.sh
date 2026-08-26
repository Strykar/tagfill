#!/usr/bin/env bash
# Scripted walkthrough against demo/Music -- real album metadata, silent
# audio. Needs a MusicBrainz contact, which their policy requires:
#   TAGFILL_CONTACT=you@example.org demo/demo.sh
set -e
cd "$(dirname "$0")/.."
: "${TAGFILL_CONTACT:?set TAGFILL_CONTACT to an email for the MusicBrainz user agent}"

W=$(mktemp -d); CFG="$W/tagfill.toml"
printf '[collection]\nroot = "%s/demo/Music"\n[musicbrainz]\ncontact = "%s"\n' \
  "$PWD" "$TAGFILL_CONTACT" > "$CFG"
trap 'rm -rf "$W"' EXIT

say()   { printf '\n\033[1;36m$ %s\033[0m\n' "$*"; }
note()  { printf '\033[2m%s\033[0m\n' "$*"; }
tags()  { python - "$1" <<'PY'
import sys, glob
sys.path.insert(0, ".")
from tagfill import probe
from pathlib import Path
f = sorted(glob.glob(sys.argv[1]))[0]
st = probe.read(Path(f))
for k in ("artist", "title", "album", "albumartist", "date", "genre", "tracknumber"):
    print(f"    {k:12} {st.get(k) or '\033[31m-- missing --\033[0m'}")
art = probe.read_art(Path(f))
print(f"    {'cover art':12} " + (f"{len(art[0])//1024} KB" if art else "\033[31m-- missing --\033[0m"))
PY
}

say "du -sh demo/Music && find demo/Music -type f | wc -l"
du -sh demo/Music | cut -f1 | tr -d '\n'; printf '  across '; find demo/Music -type f | wc -l
note "  Real album metadata, silent audio. Durations are the real ones, so"
note "  the duration gate behaves exactly as it does on a real library."
sleep 2

say "# what one track looks like now"
tags 'demo/Music/Portishead - Dummy/*.ogg'
sleep 2

say "tagfill --report"
tagfill --config "$CFG" --workdir "$W" --report 2>&1 | sed -n '7,14p'
sleep 2

say "tagfill mb --apply --backup-tags"
note "  MusicBrainz, then iTunes, then Discogs. Every candidate has to match"
note "  the folder's track lengths before a byte is written."
tagfill --config "$CFG" --workdir "$W" mb --apply --backup-tags 2>&1 | tail -6
sleep 2

say "# the same track"
tags 'demo/Music/Portishead - Dummy/*.ogg'
sleep 2

say "tagfill --report"
tagfill --config "$CFG" --workdir "$W" --report 2>&1 | sed -n '7,20p'
sleep 2

say "tagfill restore    # every change is reversible"
tagfill --config "$CFG" --workdir "$W" restore 2>&1 | tail -2
tags 'demo/Music/Portishead - Dummy/*.ogg'
