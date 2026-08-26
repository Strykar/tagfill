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
