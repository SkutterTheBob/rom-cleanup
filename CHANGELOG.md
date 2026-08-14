# Changelog

## 1.4.0

- Added `--convert-to-chd`: finds every `.cue` file under `roms_dir` and
  converts it to `.chd` via `chdman createcd` (requires `chdman`, from
  `mame-tools`, on `PATH`, or `--chdman-path` to point at it directly). If
  the `.cue` wasn't already directly in `roms_dir` -- e.g. it's sitting in
  its own per-release subfolder alongside its `.bin` tracks -- the
  resulting `.chd` is moved up into `roms_dir` itself, same "flatten up"
  convention as `--flatten-alpha-dirs`. Skips a `.cue` whose `.chd`
  already exists, so re-runs are cheap and resumable. Leaves the original
  `.bin`/`.cue` files in place -- running the normal duplicate scan with
  `--apply` afterward automatically routes them into
  `.duplicates/Redundant-Raw-Disc/`, since it already detects a `.chd`
  alongside raw disc files for the same release. Respects `--apply`
  (dry-run preview by default) and runs standalone. Before handing a
  `.cue` to `chdman`, validates that every file it references exists
  under that exact name; when a cue sheet references a filename that only
  differs by case from what's actually on disk (common when a cue
  authored on a case-insensitive filesystem ends up on a case-sensitive
  one), the on-disk file is renamed into place to match -- with `--apply`
  -- rather than leaving it to `chdman`'s own unhelpful error for this case.
- The normal scan's `--apply` now removes now-empty source folders after
  moving files into `.duplicates/` -- e.g. a per-release subfolder whose
  `.bin`/`.cue` just got routed to `Redundant-Raw-Disc/` after its `.chd`
  was moved up by `--convert-to-chd`. Cascades to the parent folder if
  that's emptied as a result, but never removes `roms_dir` itself or a
  folder that still has something else in it.

## 1.3.0

- Added `--gamelist-clean`: recursively finds every `gamelist.xml` under
  `roms_dir` (ES-DE/EmulationStation style -- works whether `roms_dir` is a
  single console folder or a top-level ROMs folder with one subfolder per
  system) and removes any `<game>` entry whose block contains
  `ZZZ(notgame)`, the marker some libretro cores' auto-generated setup/
  config entries carry, so they don't show up as playable games in ES-DE.
  Respects `--apply` (dry-run preview by default) and runs standalone.
  Validates the file is well-formed XML both before and after editing --
  a malformed `gamelist.xml` is skipped with a warning rather than risking
  a corrupted write. Before overwriting a `gamelist.xml`, its pre-clean
  content is backed up to a hidden `.rom-cleanup-gamelist-xml.bak` right next to it,
  overwritten on each re-run that actually changes something.

## 1.2.0

- Added `--flatten-alpha-dirs`: moves everything out of single-letter A-Z
  (or catch-all `#`/`0-9`/`Misc`/`[BIOS]`/etc) bucket folders directly under
  `roms_dir` up into `roms_dir` itself, then removes the emptied bucket
  folders. Respects `--apply` (dry-run preview by default, like everything
  else). Each bucket entry -- a file, or a whole subfolder such as a
  multi-disc release's own directory -- is moved as one unit, preserving
  nested release structure.

## 1.1.0

- CHD is now preferred over raw disc images (`.bin`/`.cue`/`.iso`/etc) when
  comparing releases of the same title, ranked above region preference.
- Redundant raw disc images alongside an already-present `.chd` in the same
  release are now split out into `.duplicates/Redundant-Raw-Disc/`, even
  when there's only one release for that title (no cross-release comparison
  needed to trigger it).

## 1.0.0

Initial versioned release. Includes:

- Region/revision-based duplicate detection with a configurable region
  priority list, including correct handling of combined region tags
  (e.g. `USA, Korea` scores the same as a plain `USA` release).
- Multi-file release bundling: `.cue` + all `.bin` tracks, and multi-disc
  sets, are treated as one release instead of being split into fake
  "duplicates" of each other.
- `[BIOS]`-tagged files routed to `.duplicates/bios/`.
- Proto/beta builds always routed to `.duplicates/Proto-Beta/`, even when
  it's the only copy of that title.
- `rom_filters.txt` support with `[whitelist]`/`[blacklist]` sections, at
  both whole-title and specific-release granularity (partial-tag/subset
  matching, so you don't need to spell out every tag).
- Hidden `.duplicates` output folder.
- Per-run logging to `.rom_cleanup.log` next to the script (apply-only,
  dry-runs are not logged).
- Per-folder scan marker (`.rom_cleanup_scanned`) recording script version
  and date, with a warning when re-scanning a folder that was last
  processed by a different version.
