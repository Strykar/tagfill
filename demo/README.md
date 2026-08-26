# demo/

`Music/` is a synthetic corpus: **real album metadata, silent audio**. The
durations are the genuine ones fetched from MusicBrainz, so the duration
gate behaves exactly as it does against a real library — but a four-minute
track is a few KB of 8 kHz mono silence rather than 40 MB, and no
copyrighted audio is redistributed. 56 files across four containers
(flac, mp3, ogg, m4a) in about 5 MB.

Only artist, title and album are tagged. Everything tagfill is meant to
fill — albumartist, date, genre, track number, cover art — is deliberately
absent, which is what an under-tagged collection actually looks like.

```
TAGFILL_CONTACT=you@example.org demo/demo.sh
```

`make_corpus.py` regenerates it from MusicBrainz if the releases ever move:

```
python demo/make_corpus.py --contact you@example.org
```

`tagfill.cast` is a recording of the above, playable with
[asciinema](https://asciinema.org): `asciinema play demo/tagfill.cast`.

`tagfill.gif` is that recording rendered for the README, since GitHub
markdown cannot run the player. Regenerate it after re-recording with
[agg](https://github.com/asciinema/agg):

```text
agg --theme asciinema --font-size 16 --idle-time-limit 2.5 --no-loop \
    --last-frame-duration 5 demo/tagfill.cast demo/tagfill.gif
```

`--idle-time-limit 2.5` is what makes it readable: without it each screen
of output flashes past faster than anyone can read it. `--no-loop` because
an animation that repeats forever on a project page is a distraction
nobody asked for.

The README shows `poster.png`, not the GIF, so nothing moves until a
reader chooses to watch it -- GitHub strips `<video>` from markdown, so a
still that links to the animation is the only way to get click-to-play.
Regenerate the poster from whichever frame reads best:

```text
python - <<'EOF'
from PIL import Image, ImageDraw
gif = Image.open("demo/tagfill.gif"); gif.seek(6)
base = Image.blend(gif.convert("RGB"),
                   Image.new("RGB", gif.size, (0, 0, 0)), 0.45)
w, h = base.size; cx, cy, r, t = w // 2, h // 2, 46, 22
d = ImageDraw.Draw(base, "RGBA")
d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=(255, 255, 255, 235))
d.polygon([(cx - t + 6, cy - t), (cx - t + 6, cy + t), (cx + t + 2, cy)],
          fill=(20, 20, 24, 255))
base.save("demo/poster.png", optimize=True)
EOF
```
