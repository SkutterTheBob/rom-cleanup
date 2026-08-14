# Changelog

## 1.3.1

- Fixed: `(Virtual Console)` and `(Switch Online)` re-releases could win
  over a plain release of the same title purely on the file-size tiebreak
  (these re-releases are often larger than the original cartridge dump
  due to injected emulator code). `virtual console` and `switch online`
  are now treated as "bad" tags like proto/beta/demo/pirate/etc -- scored
  below any competing release regardless of size, but still kept if it's
  the only copy of that title on hand (never lose a game entirely).

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
