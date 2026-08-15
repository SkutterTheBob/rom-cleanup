# rom_cleanup.py

A single-file Python script that scans a ROMs folder, groups files that are
different releases of the *same* game (regions, revisions, proto/beta
builds, BIOS files, multi-disc/multi-track CD images, CHD vs raw disc
images), and moves everything except the "best" copy into a `.duplicates`
subfolder.

Nothing is ever moved without your explicit say-so: every run is a
**dry-run preview by default**. You only get real file moves with `--apply`.

Requires Python 3.5+ (no third-party dependencies to run the script itself).
`--convert-to-chd` additionally requires `chdman` (from `mame-tools`) to be
installed and on `PATH` — see that section below.

## Quick start

```bash
python3 rom_cleanup.py /path/to/roms/SNES          # preview only
python3 rom_cleanup.py /path/to/roms/SNES --apply   # actually move files
```

## What it does

- **Groups releases by title**, ignoring region/revision/language tags, and
  picks a keeper using (in priority order): not-proto/beta/bad-dump/
  Virtual-Console/Switch-Online → CHD over raw bin/cue/iso → best region
  (configurable) → highest revision → largest file size (tiebreak).
- **Bundles multi-file releases** (`.cue` + all its `.bin` tracks, or a
  multi-disc set) so they're compared as ONE release, not split into
  fake "duplicates" of each other.
- **Routes BIOS files** (anything tagged `[BIOS]`) into `.duplicates/bios/`.
- **Always routes proto/beta builds** to `.duplicates/Proto-Beta/`, even if
  it's the only copy of that title.
- **Cleans up redundant raw disc images** — if a release folder has both a
  `.chd` and leftover `.bin`/`.cue` for the same disc, the raw files move
  to `.duplicates/Redundant-Raw-Disc/` and only the `.chd` is kept.
- **Removes now-empty source folders** — after `--apply` moves files into
  `.duplicates/` (dupes, BIOS, proto/beta, or redundant raw disc images),
  any per-release subfolder they came from that's now completely empty
  gets removed too (cascading up to its parent if that's emptied as a
  result). Never touches `roms_dir` itself, and leaves alone any folder
  that still has something else in it (like the kept `.chd`, or an
  unrelated file).
- **Supports a filter file** (see below) to protect specific titles/releases
  from ever being touched, or to manually pin which release should win.
- **Flattens alphabetical bucket folders** (`--flatten-alpha-dirs`) — moves
  everything out of single-letter `A`-`Z` (or catch-all `#`/`0-9`/`Misc`/
  `[BIOS]`/etc) subfolders directly under the roms folder back up into it,
  then removes the emptied bucket folders. Runs as its own standalone
  operation (dry-run preview by default, `--apply` to actually do it) — it
  does not also run the normal duplicate scan in the same invocation.
- **Cleans "notgame" entries out of ES-DE gamelist.xml files**
  (`--gamelist-clean`) — recursively finds every `gamelist.xml` under the
  folder you point it at (works whether that's a single console folder or
  a top-level ROMs folder with one subfolder per system) and removes any
  `<game>` entry whose block contains `ZZZ(notgame)` — the marker some
  libretro cores' auto-generated setup/config entries carry — so they
  don't show up as playable games in ES-DE. Also runs standalone, respects
  `--apply`, and validates the XML is well-formed before and after editing
  so a malformed file is skipped rather than risking a bad write. Before
  overwriting a `gamelist.xml`, its pre-clean content is backed up to a
  hidden `.rom-cleanup-gamelist-xml.bak` right next to it (overwritten on each re-run
  that actually changes something, so it always holds the most recent
  original).
- **Converts bin/cue CD images to CHD** (`--convert-to-chd`) — finds every
  `.cue` file under the folder you point it at and converts it to `.chd`
  via `chdman createcd` (requires `chdman`, which ships with `mame-tools`,
  on `PATH`, or pass `--chdman-path`). If the `.cue` wasn't already
  directly in that folder — e.g. it's sitting in its own per-release
  subfolder alongside its `.bin` tracks — the resulting `.chd` is moved up
  into the folder itself, alongside the rest of the roms. Skips a `.cue`
  whose `.chd` already exists, so re-runs are cheap and resumable. Leaves
  the original `.bin`/`.cue` files in place; run the normal duplicate scan
  with `--apply` afterward and it'll automatically route them into
  `.duplicates/Redundant-Raw-Disc/` (it already detects a `.chd` alongside
  raw disc files for the same release). Also runs standalone and respects
  `--apply`. Before handing a `.cue` to `chdman`, checks that every file it
  references actually exists under that exact name — cue sheets are often
  authored on a case-insensitive filesystem (Windows) and then moved to a
  case-sensitive one (Linux), where e.g. `FILE "GAME.BIN"` silently fails
  to match an on-disk `game.bin`. When there's exactly one case-insensitive
  match, it's renamed into place to match what the `.cue` expects (only
  with `--apply`); when it's missing entirely or ambiguous, that `.cue` is
  reported and skipped instead of being handed to `chdman`, whose own
  error message for this doesn't clearly name the actual missing file.
- **Groups multi-disc games into ES-DE/RetroArch's `.m3u` layout**
  (`--make-m3u`) — finds disc-tagged `.chd` releases (e.g.
  `Game (USA) (Disc 1).chd`, `(Disc 2).chd`) and, for any title with 2+
  discs, moves them all into a subfolder named after the release directly
  under `roms_dir`, alongside an `.m3u` playlist (same name as the
  folder) listing each disc in filename order — the layout ES-DE and
  RetroArch expect to show and launch a multi-disc game as a single
  entry. Discs are ordered numerically (`Disc 10` sorts after `Disc 2`,
  not before it). A lone disc-tagged file with no siblings, or two files
  claiming the same disc number, are left untouched — the latter is
  flagged for manual review rather than guessed at. Skips releases
  already grouped with an up-to-date `.m3u` (re-checks its content, so
  adding a disc later is picked up on the next run), and cleans up any
  source folder left empty by the move. CHD only — run
  `--convert-to-chd` first for multi-disc sets still in `.bin`/`.cue`
  form. Also runs standalone and respects `--apply`.
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
| `--flatten-alpha-dirs` | Move files out of single-letter `A`-`Z` (or `#`/`0-9`/`Misc`/`[BIOS]`/etc) bucket subfolders directly under `roms_dir`, then remove those folders |
| `--gamelist-clean` | Remove `<game>` entries containing `ZZZ(notgame)` from every `gamelist.xml` found under `roms_dir` (backs each changed file up to `.rom-cleanup-gamelist-xml.bak` first) |
| `--convert-to-chd` | Convert every `.cue` found under `roms_dir` to `.chd` via `chdman`, moving the result up into `roms_dir` if it was nested in a subfolder |
| `--chdman-path PATH` | Path to the `chdman` executable, if it's not on `PATH` (default: look up `chdman` on `PATH`) |
| `--make-m3u` | Group disc-tagged `.chd` releases with 2+ discs into a per-release subfolder under `roms_dir` with an `.m3u` playlist |
| `--version` | Print the script version and exit |

## Installing chdman (for `--convert-to-chd`)

`chdman` is part of MAME's tools, packaged separately as `mame-tools` on
most Linux distros:

```bash
# Debian / Ubuntu
sudo apt install mame-tools

# Fedora
sudo dnf install mame-tools

# Arch (AUR)
yay -S mame-tools
```

- **macOS**: `brew install mame` (bundles `chdman`).
- **Windows**: no official package manager entry — download a MAME Windows
  build from [mamedev.org](https://www.mamedev.org/release.html), which
  includes `chdman.exe`, and either put it on `PATH` or point
  `--chdman-path` at it directly.

If `chdman` isn't on `PATH`, pass its location explicitly:

```bash
python3 rom_cleanup.py /path/to/roms/PSX --convert-to-chd --chdman-path /opt/mame/chdman --apply
```

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

After `--make-m3u --apply`, a multi-disc release ends up like this:

```
your-roms-folder/
└── Final Fantasy VII (USA)/
    ├── Final Fantasy VII (USA).m3u
    ├── Final Fantasy VII (USA) (Disc 1).chd
    ├── Final Fantasy VII (USA) (Disc 2).chd
    └── Final Fantasy VII (USA) (Disc 3).chd
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
