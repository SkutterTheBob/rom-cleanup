# rom_cleanup.py

A single-file Python script that scans a ROMs folder, groups files that are
different releases of the *same* game (regions, revisions, proto/beta
builds, BIOS files, multi-disc/multi-track CD images, CHD vs raw disc
images), and moves everything except the "best" copy into a `.duplicates`
subfolder.

Nothing is ever moved without your explicit say-so: every run is a
**dry-run preview by default**. You only get real file moves with `--apply`.

Requires Python 3.5+ (no third-party dependencies to run the script itself).

## Quick start

```bash
python3 rom_cleanup.py /path/to/roms/SNES          # preview only
python3 rom_cleanup.py /path/to/roms/SNES --apply   # actually move files
```

## What it does

- **Groups releases by title**, ignoring region/revision/language tags, and
  picks a keeper using (in priority order): not-proto/beta/bad-dump →
  CHD over raw bin/cue/iso → best region (configurable) → highest
  revision → largest file size (tiebreak).
- **Bundles multi-file releases** (`.cue` + all its `.bin` tracks, or a
  multi-disc set) so they're compared as ONE release, not split into
  fake "duplicates" of each other.
- **Routes BIOS files** (anything tagged `[BIOS]`) into `.duplicates/bios/`.
- **Always routes proto/beta builds** to `.duplicates/Proto-Beta/`, even if
  it's the only copy of that title.
- **Cleans up redundant raw disc images** — if a release folder has both a
  `.chd` and leftover `.bin`/`.cue` for the same disc, the raw files move
  to `.duplicates/Redundant-Raw-Disc/` and only the `.chd` is kept.
- **Supports a filter file** (see below) to protect specific titles/releases
  from ever being touched, or to manually pin which release should win.
- **Logs every applied run** to a hidden `.rom_cleanup.log` next to the
  script itself, and stamps each scanned roms folder with the script
  version + date so you're warned if you're re-running an updated script
  against a folder processed by an older version.

## CLI flags

| Flag | Description |
|---|---|
| `roms_dir` | Path to the ROMs folder to scan (positional, required) |
| `--apply` | Actually move files (default: dry-run preview only) |
| `-v`, `--verbose` | Print details for every title, not just ones with duplicates |
| `--dup-dir PATH` | Override the duplicates folder location (default: `<roms_dir>/.duplicates`) |
| `--regions LIST` | Comma-separated region preference, best first (default: `USA,World,Europe,Japan,...`) |
| `--ext LIST` | Comma-separated extensions to consider (default: common ROM/disc formats) |
| `--filter-file PATH` | Override the filter file location (default: `<roms_dir>/rom_filters.txt` if present) |
| `--version` | Print the script version and exit |

## The filter file: `rom_filters.txt`

Drop a `rom_filters.txt` in the roms folder you're scanning and it's
picked up automatically — no flag needed. Format:

```ini
[blacklist]
# Whole-title entry: never touch this game at all
Chrono Trigger

# Release-specific entry: force THIS release to always be treated
# as a duplicate, even if it would otherwise win
Shadow Dancer - The Secret of Shinobi (SEGA Classic Collection)

[whitelist]
# Whole-title entry: restrict this run to ONLY these titles
Mario Kart

# Release-specific entry: pin THIS exact release as the forced
# keeper for its title, overriding the normal scoring
Shadow Dancer - The Secret of Shinobi (World)
```

Rules:
- Blank lines and lines starting with `#` are ignored.
- A line with **no tags** (e.g. `Chrono Trigger`) applies to the whole title.
- A line **with tags** (e.g. `... (World)`) applies only to that specific
  release. You don't need to spell out every tag — just enough to
  identify the release (subset matching).
- Blacklist always wins over whitelist for the same release.
- The bottom of every run's output shows exactly which filter entries
  matched (and flags any that matched nothing — handy for catching typos).

## Folder layout after a scan

```
your-roms-folder/
├── Game A (USA).zip
├── rom_filters.txt              # optional, your filter file
├── .rom_cleanup_scanned         # written after every --apply run
└── .duplicates/
    ├── Game A (Europe).zip      # region/revision duplicates, loose
    ├── bios/
    ├── Proto-Beta/
    └── Redundant-Raw-Disc/
```

## Development

```bash
pip install pytest --break-system-packages
pytest tests/ -v
```

See `tests/test_rom_cleanup.py` — it covers the trickier edge cases this
script has hit in practice: combined region tags (`USA, Korea`), multi-track
CD bundling, CHD vs bin/cue preference, and partial-tag filter matching.

See `CHANGELOG.md` for version history.
