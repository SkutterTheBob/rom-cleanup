# Changelog

## 1.6.0

- Reworked duplicate-scoring priority order to (after CHD-over-raw-disc
  preference): **effective region** → non-standard-tag status → English-
  tag tiebreak → revision → file size. Effective region combines region
  and confirmed-English-language availability into one ranking:
  - one of the top 2 configured regions (`USA`/`World` by default -- both
    long-standing No-Intro/GoodTools conventions for including English
    content) beats everything else outright;
  - failing that, an explicit `En` language tag (e.g. `(En)` or
    `(En,Fr,De)`) -- confirming English text is actually present -- beats
    a same-or-better *nominal* region that doesn't confirm it, e.g.
    `Game (Japan) (En)` beats a plain `Game (Europe)`, since `Europe`
    alone doesn't guarantee English the way an explicit tag does; a plain
    `Game (World)` still beats it, though, since `World` already implies
    English by convention;
  - otherwise, normal region order (`World` beats `Japan`) -- this still
    wins outright over a same-or-better-tier release carrying a
    non-standard tag, e.g. `Game (World) (Collection of SaGa)` beats a
    plain `Game (Japan)`, since `World` is simply the better tier and no
    plain-`World` release exists to compete with instead.

  Only among releases tied on effective region does non-standard-tag
  status become the deciding factor: instead of a maintained keyword list
  (`virtual console`, `switch online`, ...), a release now loses to an
  otherwise-equal one (same effective region) if it carries ANY tag that
  isn't a recognized region, revision, language, and/or known-neutral
  informational tag (currently just `SGB Enhanced` -- Super Game Boy
  palette/border support; unlike compilation names, this small allowlist
  is deliberately kept, since the set of legitimate technical footnotes is
  small and rarely grows, unlike compilation/service names). This catches
  compilation/service re-release tags automatically -- `Sega Channel`,
  `Disney Classic Games`, `Castlevania Anniversary Collection`, and any
  future one like them -- with nothing to add by hand, while a
  legitimately region-tagged release that also happens to list its
  languages (e.g. `(USA, Europe) (En,Fr,De,Es)`) is correctly treated as a
  normal release. Removes the old `BAD_TAGS` keyword list and
  `has_bad_tag()` entirely -- every keyword it covered already fails the
  new "is this a region/revision/language/neutral tag" check.

  `Game (USA)` is always top priority, followed by `Game (USA) (Rev 1)`;
  a release still wins if it's the only copy of that title on hand, even
  with a non-standard tag.

## 1.5.1

- Changed `--make-m3u`'s layout: the `.m3u` playlist now sits directly in
  `roms_dir` (e.g. `Game (USA).m3u`) instead of inside the release's own
  subfolder, and the disc `.chd` files move into a per-release subfolder
  nested under a single hidden `.chd/` folder in `roms_dir` (e.g.
  `.chd/Game (USA)/`) instead of a plain visible one. The previous
  same-folder layout made ES-DE/RetroArch show two entries for one game
  -- the folder AND the `.m3u` -- since neither frontend actually
  collapses a visible folder + `.m3u` pair into one entry; both do ignore
  dot-prefixed directories when scanning, so hiding the discs' folder is
  what actually gets the single-entry behavior the flag always intended.
  Nesting every release's hidden folder under one shared `.chd/` also
  keeps `roms_dir` itself from filling up with a dot-folder per release.
  A release still sitting in an older layout is auto-migrated into the
  current one (including deleting any now-stale old `.m3u`) the next
  time `--make-m3u --apply` runs.

## 1.5.0

- Added `--make-m3u`: finds disc-tagged `.chd` releases (e.g.
  `Game (USA) (Disc 1).chd`, `(Disc 2).chd`) and, for any title with 2+
  discs, groups them into a subfolder named after the release directly
  under `roms_dir`, with an `.m3u` playlist (same name as the folder)
  listing each disc in order -- the layout ES-DE/RetroArch expect to show
  and launch a multi-disc game as a single entry. Discs are ordered
  numerically, not alphabetically (`Disc 10` sorts after `Disc 2`). A
  lone disc-tagged file with no siblings, or two files claiming the same
  disc number, are left untouched -- the latter flagged for manual
  review rather than guessed at. Skips releases already grouped with an
  up-to-date `.m3u` (content is re-checked, not just existence, so a
  disc added later is picked up on the next run), and cleans up any
  source folder left empty by the move. CHD only -- run
  `--convert-to-chd` first for multi-disc sets still in `.bin`/`.cue`
  form. Respects `--apply` (dry-run preview by default) and runs
  standalone.

## 1.4.1

- Fixed: `(Virtual Console)` and `(Switch Online)` re-releases could win
  over a plain release of the same title purely on the file-size tiebreak
  (these re-releases are often larger than the original cartridge dump
  due to injected emulator code). `virtual console` and `switch online`
  are now treated as "bad" tags like proto/beta/demo/pirate/etc -- scored
  below any competing release regardless of size, but still kept if it's
  the only copy of that title on hand (never lose a game entirely).

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
