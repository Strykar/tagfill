# Security

## Reporting

Use **Report a vulnerability** under the repository's Security tab, which
opens a private advisory only the maintainer can see. Please do not open a
public issue for anything exploitable.

If that button is not there, private reporting has not been switched on yet
— open a normal issue saying only that you have a security report and asking
for a private channel, without the details.

## What tagfill does to your files

It writes tags and cover art, in place, to files under the collection root.
It never moves, renames, deletes or reorganises anything. Every write goes to
a temp file beside the target, is fsynced, and is then renamed over it, so a
power cut leaves either the old file or the new one and never a half-written
one. `--backup-tags` snapshots every field and the art bytes before the first
write, and `tagfill restore` puts them back.

Two things it refuses outright:

- **Symlinks.** A write does not go *through* a link, it replaces it: the copy
  and rename would leave a regular file where the link was, the real target
  untouched, and the content forked. Links are skipped by the scan, recorded
  in the census, and refused at the write itself.
- **Anything outside the collection root.** Paths from `--from-review` and
  from `backup/tags.jsonl` are checked against the root before use, because
  both are files a human can edit.

## Trust model for the files it parses

Music files that are missing artist and title are, in practice, downloads of
uncertain provenance, and `acoustid` decodes exactly those. So the question
of what parses hostile input matters.

**Tag parsing is pure Python.** mutagen has no native code, so the memory
safety surface of reading a crafted MP3 or FLAC header is effectively nil.
The realistic ceiling is denial of service: a huge embedded picture, or a
file that makes a parser allocate. Cover art is capped at 32 MB and a picture
block that does not parse is journalled as an issue on that file, not raised
into the run.

**The native surface is elsewhere**, and it is real:

| Component | Reached by | Handling |
| --- | --- | --- |
| Pillow | every image tagfill embeds | decoded before embedding, format allowlist, size cap |
| ffmpeg / flac | `convert` only, off unless `--convert-wav` | subprocess; failures are journalled, not fatal |
| fpcalc (chromaprint) | `acoustid` only, needs an API key | subprocess |

Network art is decoded, not sniffed. A CDN returning an HTML error page, a
WEBP, or a truncated JPEG is refused rather than embedded with a guessed MIME
type for every phone and car head unit downstream to parse.

## Data that leaves your machine

Album and artist names, and track durations, go to MusicBrainz, iTunes and
Discogs. Audio fingerprints (not audio) go to AcoustID, and only when you
have configured a key. Nothing else is sent, and nothing is sent at all under
`--offline` or for the offline stages.

MusicBrainz requires an identifying contact address in the user agent; that
is the one piece of personal data tagfill transmits, and only because you put
it in the config.

## The workdir

`backup/tags.jsonl` holds every tag tagfill has seen plus the full bytes of
every cover it replaced, and `journal.jsonl` holds every path in your
collection. The workdir is created owner-only (0700) on POSIX. On Windows it
inherits the ACLs of `%LOCALAPPDATA%`, which is already per-user.

## Windows specifics

- The released `tagfill.exe` is **not code-signed**, so SmartScreen will warn.
  Each release ships a `SHA256SUMS` next to the binary; check it. The build
  workflow pins its actions by commit SHA and pins the PyInstaller version, so
  the binary does not depend on whatever PyPI served that morning.
- **NTFS ACLs are not preserved across a write.** `copy2` carries mode and
  timestamps but not ACLs, so a file with explicit permissions on a shared
  folder or NAS comes out inheriting the directory's. There is no clean
  portable fix; if that matters for your collection, do not run tagfill
  against it.
- **Alternate data streams are dropped**, including `Zone.Identifier`. Tagging
  a downloaded file removes its mark-of-the-web.
- Real-time antivirus scans every temp file, which means an apply run over a
  large collection effectively re-scans the collection. An exclusion for the
  collection or the workdir during large runs is worth more than any change
  here.
