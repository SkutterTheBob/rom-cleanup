# Changelog

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
