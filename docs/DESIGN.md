# tagfill design

Tags a music collection in place. Your folder structure is something to preserve, not something to fix, so tagfill writes tags and embedded art into files where they already sit and never moves, renames, or deletes anything. That single constraint is why beets isn't the backbone here: beets wants to own the layout.

## Layout

| File | What's in it |
| --- | --- |
| `cli.py` | argparse dispatch, dry-run/apply plumbing, backup hook |
| `config.py` | TOML load, defaults, per-user workdir resolution |
| `journal.py` | `Record`, `Journal` (append-only + resume guard), `ReviewQueue` |
| `probe.py` | the only module that knows containers; sidecar art search |
| `backup.py` | tag snapshot + restore |
| `util.py` | `iter_audio`, `RateLimiter`, `similarity`, `sha1_head` |
| `sources/__init__.py` | `SourceMatch`, `gate()`, `_unordered_match()` |
| `sources/musicbrainz.py` | primary; catalogue search + Cover Art Archive |
| `sources/itunes.py` | fallback; duration vector from `lookup?entity=song` |
| `sources/discogs.py` | last resort; community-typed durations, often absent |
| `stages/__init__.py` | `STAGES` registry + `Context` |
| `stages/census.py` | measure; defines the file universe |
| `stages/convert.py` | WAV to FLAC, doubly verified, quarantines originals |
| `stages/art_local.py` | embed art already on disk |
| `stages/filename.py` | parse `Artist - Title (Mix)`; Camelot/BPM stripper |
| `stages/mb.py` | orchestrates `sources/` in order, applies the first match |
| `stages/acoustid.py` | fingerprint files still missing artist or title |
| `stages/itunes.py` | art-only text match (not `sources/itunes.py`'s job) |
| `stages/report.py` | unresolved-with-reasons, reacquire list, fix-rate table |
| `stages/submit.py` | opt-in: give findings back to AcoustID / MusicBrainz |

Two `itunes.py` files, deliberately. `sources/itunes.py` is a duration-verified metadata source inside the `mb` stage; `stages/itunes.py` is a later art-only pass that matches on text similarity with a margin threshold. Same API, different jobs, different confidence bars.

## Design

**Stages are independent passes** over `census.csv`, ordered cheapest-and-most-certain first, each runnable alone and each resumable. `STAGES` in `stages/__init__.py` is `(number, cli_name, module, needs_network)`; `cli.py` imports by module name and calls `run(ctx)` with a `Context` carrying config, journal, and the run flags.

**One of those stages, `mb.py`, is an orchestrator rather than a worker.** It does the folder grouping, compilation detection and duration-vector building once, then walks a list of `(search_fn, extra_kwargs)` — one entry per module in `sources/` — passing every source the same common arguments plus its own extras, and stops at the first `SourceMatch` returned. No matching logic lives in the stage, and no source knows the others exist.

`STAGES` skips 7. A SoundCloud stage held that slot and was rejected for v0.1: reaching it meant scraping, since SoundCloud publishes no unauthenticated search API, and its single-track results can't clear a gate built to verify a folder against a release tracklist. The number is left free rather than renumbering the stages around it.

**`census.py` decides the file universe once.** Always a full unscoped walk, even when `--path` is given: `census.csv` is shared ground truth, and scoping it there would silently truncate the file every other stage reads. `--path` restricts what *consuming* stages act on.

**`probe.py` is the container chokepoint.** Explicit suffix → (mutagen class, family) dispatch, then an `isinstance` assertion on what mutagen handed back. Never `easy=True`: it has no Easy wrapper for AIFF or WAVE, silently returns the raw object, and a fully tagged file then reads as untagged. `probe.FIELDS` is the complete set tagfill will ever write, which is what makes the restore guarantee scopeable.

**Nothing is written on the strength of a file extension.** The extension only decides which mutagen class to try; that class then parses the real container and raises if the bytes disagree, which is how a text file named `.mp3` becomes `issue = "unreadable: HeaderNotFoundError"` in the census instead of a write target. Every stage skips rows carrying an `issue`. The check is not merely a census-time snapshot either: `probe.write()` re-opens and re-validates the file at write time, so a census that has gone stale — file swapped, truncated, replaced — cannot authorise a bad write. Fields are written through an explicit per-family frame map (`_ID3_FRAMES`, `_VORBIS_KEYS`, `_MP4_KEYS`), never by handing mutagen an arbitrary key, so a container that has no home for a field simply has no entry for it.

**Validating is not the same as succeeding**, so every write goes through `guarded_write()`. A file can be read-only, a disk can fill, a volume can vanish mid-run. Those raise from `audio.save()` well after validation passed, and an unguarded call takes the whole stage down with it — found live with `chmod 444` on the second of three files in an album: traceback, exit 1, and the writable third file never processed. The helper journals the failure as a skip with its reason and lets the batch continue, so one unwritable file costs that file alone and `report.py` can still say what happened to it.

**Album matching is gated on the duration vector, never on text.** `sources/__init__.gate()` requires equal track count, then ≥`pass_fraction` of tracks within `tolerance` seconds — positionally first, falling back to an order-independent greedy match at the *same* threshold. Wrong-edition and same-name-different-album false positives are the dominant failure mode of artist+album search, and this is the only thing standing between a plausible match and a wrong write. The unordered tier exists because folders whose filenames carry no track number sort alphabetically; it uses the same pass fraction rather than a stricter one, because demanding every track match rejects correct releases that genuinely have one divergent track on a given pressing.

**Track number rides on that evidence.** `mb.py` writes it only when `match.evidence["order"] == "positional"` — an unordered match proves the album, not which local file is which track. Guessing there would write a wrong number where an empty field is honest.

**Multi-artist folders are treated as compilations**, with the album name resolved by plurality vote over existing tags (real compilations carry stragglers still tagged with their original album). Per-track artist is never touched; only `albumartist` is, from the source's own credits.

**Every decision is journaled with evidence**, accepts and declines alike. A silent `continue` on a declined candidate is indistinguishable from never having looked, which is what makes `report.py` able to say *why* each file is still unresolved.

**Writes are conservative by construction**: dry run unless `--apply`, never overwrite a non-empty field without `--overwrite`, nothing ever deleted (conversion originals go to quarantine). `--backup-tags` snapshots `probe.FIELDS` plus art bytes before a file's first write; `restore` puts them back and deletes fields that were empty pre-run. The resume guard snapshots size/mtime/64KB-head-hash *after* a write — a pre-write snapshot would make every touched file look changed on the next run.

**Art "missing" means absent or below `art_min_px`**, consistently at every selection point, so an early 200px thumbnail can't permanently block a better source under the never-overwrite rule.

**The workdir is per-user state**, not the cwd and deliberately not cache: it holds `backup/tags.jsonl` and `quarantine/wav`, and the XDG contract for cache is that it may be deleted at any time without consequence.

## Extending it

**Add a metadata source.** Write `sources/<name>.py` exposing:

```python
def search(*, album, local, tolerance, pass_fraction, limiter, **_ignored
          ) -> tuple[SourceMatch | None, list[dict]]
```

Take whatever kwargs you need, absorb the rest in `**_ignored` (the orchestrator passes a common set to everyone). Build a candidate duration vector, hand it to `gate()`, and on a pass return a `SourceMatch` whose `fetch_art` is a *callable* — lazily, so a source that loses the race never pays for an art request. Then add one entry to the `sources` list in `stages/mb.py:run()`, with its own `RateLimiter`. Removing a source is deleting the file and that entry. No matching logic lives in the stage.

**Add a stage.** Write `stages/<name>.py` with `run(ctx) -> None`, add a tuple to `STAGES`. Read `census.load(ctx)`, respect `ctx.within_scope()`, `ctx.limit`, and `ctx.apply`, and journal through `ctx.journal`. Use `journal.already_done()` to stay re-runnable.

**Add a container.** One entry in `probe._CONTAINERS`, one in `util.AUDIO_SUFFIXES` (kept separate because `util.py` deliberately imports nothing third-party), and a builder in `test_probe_roundtrip._BUILDERS` — a test fails if those lists diverge, so a new format cannot ship untested. If it reuses an existing tag family that is the whole change: DSD was two lines, since both `.dsf` and `.dff` carry ID3v2. A genuinely new tag format also needs its field-name map alongside `_ID3_FRAMES` / `_VORBIS_KEYS` / `_MP4_KEYS`, and any per-container special-casing — MP4's `trkn` integer-pair atom is the current example — stays inside `probe.py`.

**Add a managed tag.** Append to `probe.FIELDS`, map it per family, add it to `census.COLUMNS`, and have the sources populate it on `SourceMatch`. Fields beyond the core set should be gated on `cfg.extra_tags` the way `genre` and `tracknumber` are.

## Future work

- **`sources/musicbrainz.py:search()` has no end-to-end test.** iTunes and Discogs are covered by `requests_mock` suites; MusicBrainz only gets `_top_genre()` unit tests and a grep-based regression guard, because `musicbrainzngs` isn't a plain-`requests` client so `requests_mock` can't intercept it. The pattern to copy is `test_decision_logging.py`, which injects a fake module via `monkeypatch.setitem(sys.modules, ...)`.
- **`gate()` has no minimum track count.** A one-track folder passes on any candidate release whose single track falls within `tolerance`, so the verification that justifies every write contributes nothing at n=1. Not currently reachable by accident — a lone file only reaches the lookup if it already carries both artist and album, and the real 1822-file collection produced zero such matches — but the guard is missing rather than deliberate. Decide whether n=1 should require a stricter tolerance, an exact title match, or simply be skipped.
- **One collection root, and one workdir shared by all of them.** Since the workdir moved to a fixed per-user location, pointing `--music-dir` at a second collection reuses the first one's `census-baseline.csv`, journal and resume guard — and the guard keys on paths *relative to root*, so `Album/01.mp3` in one collection looks already-handled in the other. Accepted for now: pass `--workdir` per collection if you have more than one.
- **One collection root, by construction.** `census.csv`, `journal.jsonl`, `backup/tags.jsonl` and the report CSVs all key on paths relative to `cfg.root`, so several roots would need either a root-id in every key or a workdir each. Today the answers are a common parent plus `exclude` globs, or separate `--workdir`s. Worth doing properly only if someone actually has music on two volumes.
- **Nothing has run on Windows beyond CI's `--version` and `--report`.** Two known unknowns: paths over 260 characters fail unless the machine has `LongPathsEnabled`, and on a case-insensitive filesystem the journal's resume guard keys on the exact relative-path string, so the same file reached with different casing would look like two files.
- **`_open()`'s `isinstance` check cannot fire.** `cls(path)` returns an instance of `cls` or raises, so the assertion after it is tautological — the real container validation is the constructor raising, not this. It reads like a guard and isn't one. Either drop it or replace it with something that can actually fail.
- **`Context.extras` is declared and never read.** Either a hook point for stage-specific options or dead weight; decide before something starts depending on it.
- **The submit stage is untested against the live AcoustID endpoint.** It builds the pairings and refuses to POST without `$ACOUSTID_USER_KEY`, but nobody has run the write path.
- **The journal is a structured log nothing consumes but `report.py`.** It carries enough evidence for an HTML diff view of a dry run against an apply.
- **Network stages have no recorded-fixture tests** against real captured responses; current coverage is hand-written mocks, which may drift from what the APIs actually return.
