#!/usr/bin/env python3
"""
rom_cleanup.py

Scans a ROMs folder, groups files that appear to be the same game
(different regions/revisions/versions/etc.), keeps the "best" copy in
place, and moves the rest into a "duplicates" subfolder.

Works with common No-Intro / GoodTools / TOSEC style naming, e.g.:
    Super Game (USA).zip
    Super Game (Europe) (Rev 1).zip
    Super Game (Japan) (Beta).zip
    Super Game (World) (En,Fr,De).zip

USAGE
    python3 rom_cleanup.py /path/to/roms                # dry run (default)
    python3 rom_cleanup.py /path/to/roms --apply         # actually move files
    python3 rom_cleanup.py /path/to/roms --apply -v      # verbose

    # customize region preference order (best first):
    python3 rom_cleanup.py /path/to/roms --regions "USA,World,Europe,Japan"

    # only look at specific extensions:
    python3 rom_cleanup.py /path/to/roms --ext .zip,.nes,.sfc

By default nothing is moved -- it just prints what it WOULD do.
Pass --apply to actually move files. This is intentional so you can
review the grouping before anything happens to your files.
"""

import argparse
import datetime
import difflib
import json
import os
import re
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from collections import defaultdict, namedtuple

__version__ = "1.11.0"

# ---- Tag parsing -----------------------------------------------------

# Matches groups like "(USA)", "(Rev 1)", "[b]", "(En,Fr,De)"
TAG_RE = re.compile(r"\(([^()]*)\)|\[([^\[\]]*)\]")

DEFAULT_REGION_PRIORITY = [
    "usa", "world", "europe", "japan", "asia", "australia", "brazil",
    "canada", "china", "france", "germany", "italy", "korea",
    "netherlands", "spain", "sweden", "taiwan", "uk", "unknown",
]

# Tags like "Track 01", "Disc 2", "CD1", "Side A" identify one PIECE of a
# multi-file release (e.g. a .cue + many .bin tracks, or a multi-disc game).
# These files are companions, not competing duplicates, so this tag is
# ignored when deciding whether two files are "the same release".
PART_TAG_RE = re.compile(
    r"^(track|disc|disk|cd|side|part)\s*[0-9]+$", re.IGNORECASE
)
PART_TAG_RE_ALT = re.compile(r"^side\s*[a-d]$", re.IGNORECASE)

# Tags that mean "this is a proto/beta build" -- these always get moved to
# duplicates, even if it's the ONLY copy of that title (unlike other
# non-standard tags -- see has_non_standard_tag -- which only matter when
# choosing between multiple releases).
PROTO_BETA_RE = re.compile(r"^(proto|prototype|beta)\b", re.IGNORECASE)


def is_proto_beta_tag(tag):
    return bool(PROTO_BETA_RE.match(tag.strip()))


def is_program_tag(tag):
    return tag.strip().lower() == "program"


def is_part_tag(tag):
    t = tag.strip()
    return bool(PART_TAG_RE.match(t) or PART_TAG_RE_ALT.match(t))

ROM_EXTENSIONS_DEFAULT = {
    ".zip", ".7z", ".rar", ".nes", ".sfc", ".smc", ".gba", ".gb", ".gbc",
    ".n64", ".z64", ".v64", ".md", ".gen", ".bin", ".cue", ".iso",
    ".chd", ".ngp", ".ngc", ".pce", ".ws", ".wsc", ".a26", ".a52",
    ".a78", ".col", ".int", ".vec", ".32x", ".gg", ".sms", ".nds",
    ".3ds", ".cia",
}

# Raw/uncompressed disc-image formats that CHD (Compressed Hunks of Data)
# supersedes. When a release's files include .chd, these are redundant
# leftovers and get moved out; when comparing two different releases of
# the same title, a CHD release is preferred over a raw-format release.
RAW_DISC_EXTENSIONS = {
    ".bin", ".cue", ".iso", ".img", ".mds", ".mdf", ".ccd", ".sub",
    ".toc", ".nrg", ".gdi",
}

# Folder names that some ROM sets use to alphabetically bucket titles into
# subfolders (e.g. "SNES/A/Aladdin (USA).zip"). --flatten-alpha-dirs moves
# everything out of folders matching this and removes the folder itself.
ALPHA_BUCKET_LETTER_RE = re.compile(r"^[a-z]$", re.IGNORECASE)
ALPHA_BUCKET_CATCHALL_NAMES = {
    "#", "0-9", "09", "misc", "other", "numbers", "symbols", "non-alpha",
    "bios", "[bios]",
}


def is_alpha_bucket_dirname(name):
    return bool(ALPHA_BUCKET_LETTER_RE.match(name)) or name.strip().lower() in ALPHA_BUCKET_CATCHALL_NAMES


# If --filter-file isn't given, look for this filename inside roms_dir.
DEFAULT_FILTER_FILENAME = "rom_filters.txt"

# Hidden log file written next to this script itself (not the roms folder),
# recording a timestamped line for every run of the script.
LOG_FILENAME = ".rom_cleanup.log"

# Hidden marker written INSIDE each scanned roms_dir, recording which
# script version last applied changes there -- lets us warn if the script
# has been updated since this folder was last processed.
SCAN_MARKER_FILENAME = ".rom_cleanup_scanned"


def extract_tags(filename_no_ext):
    """Return (base_title, list_of_tag_strings)."""
    tags = []
    for m in TAG_RE.finditer(filename_no_ext):
        tag = m.group(1) if m.group(1) is not None else m.group(2)
        tags.append(tag.strip())
    base = TAG_RE.sub("", filename_no_ext)
    base = re.sub(r"\s{2,}", " ", base).strip()
    base = base.rstrip(" -_")
    return base, tags


def normalize_title(title):
    """Loose normalization so minor punctuation/case differences still group."""
    t = title.lower()
    t = re.sub(r"[^a-z0-9]+", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def parse_revision(tags):
    for t in tags:
        m = re.match(r"rev\s*([0-9]+)", t.strip(), re.IGNORECASE)
        if m:
            return int(m.group(1))
        m = re.match(r"v\s*([0-9]+(\.[0-9]+)?)", t.strip(), re.IGNORECASE)
        if m:
            try:
                return float(m.group(1))
            except ValueError:
                pass
    return 0


def region_rank(tags, region_priority):
    """Lower is better. Unknown region gets a large-but-finite rank.

    Handles combined region tags like "USA, Korea" or "USA/Europe" by
    splitting on commas/slashes and taking the best (lowest-rank) region
    found among the parts -- a "USA, Korea" release should rank the same
    as a plain "USA" release, not fall through to "unknown".
    """
    best = None
    for tag in tags:
        for part in re.split(r"[,/]", tag):
            part = part.strip().lower()
            if part in region_priority:
                idx = region_priority.index(part)
                if best is None or idx < best:
                    best = idx
    if best is None:
        return len(region_priority)  # no recognized region tag at all
    return best


def is_recognized_region_tag(tag, region_priority):
    """True if every comma/slash-separated part of this tag is a
    recognized region (e.g. plain "USA", or a combined tag like
    "USA, Europe") -- mirrors the parsing region_rank() does.
    """
    parts = [p.strip().lower() for p in re.split(r"[,/]", tag)]
    return bool(parts) and all(p in region_priority for p in parts)


# ISO 639-1 two-letter codes commonly used in No-Intro/GoodTools-style
# language tags, e.g. "(En)" or a combined list "(En,Fr,De,Es)". Unlike
# compilation/collection names (open-ended, can't be fully listed), this
# is a small, stable, well-known vocabulary -- safe to recognize outright
# as a legitimate tag rather than treating it as non-standard.
LANGUAGE_CODES = {
    "en", "fr", "de", "es", "it", "nl", "pt", "sv", "no", "da", "fi",
    "zh", "ja", "ko", "pl", "ru", "cs", "hu", "sk", "tr", "ar", "el",
    "he", "ro", "bg", "hr", "et", "lv", "lt", "sl", "uk",
}


def is_language_tag(tag):
    """True if every comma/slash-separated part of this tag is a
    recognized language code (e.g. plain "En", or a combined list like
    "En,Fr,De,Es").
    """
    parts = [p.strip().lower() for p in re.split(r"[,/]", tag)]
    return bool(parts) and all(p in LANGUAGE_CODES for p in parts)


# Informational/technical tags that are legitimate and shouldn't penalize
# a release, even though they aren't a region/revision/language tag --
# e.g. "SGB Enhanced" (the game supports Super Game Boy palette/border
# features when played on an original Game Boy). Unlike compilation/
# service names (deliberately left unlisted -- see has_non_standard_tag,
# which catches those generically since the set is open-ended), this is
# a small, rarely-growing set of known-safe footnotes worth naming
# explicitly.
NEUTRAL_TAGS = {
    "sgb enhanced",
}


def is_neutral_tag(tag):
    return tag.strip().lower() in NEUTRAL_TAGS


def has_non_standard_tag(tags, region_priority):
    """True if this release carries any tag beyond a plain region,
    revision, language list, and/or known-neutral informational tag --
    e.g. a compilation/collection re-release (Virtual Console, Switch
    Online, Sega Channel, an anniversary/classics collection, ...), a bad
    dump, a proto/beta leftover, or any other marker. "Game (USA)" is
    always top priority, followed by "Game (USA) (Rev 1)"; anything else
    sorts behind an otherwise-equal "clean" release, but a release like
    this still wins if it's the ONLY copy of that title on hand (never
    lose a game entirely just because the one copy in hand happens to
    carry an extra tag).
    """
    for tag in tags:
        if is_recognized_region_tag(tag, region_priority):
            continue
        if parse_revision([tag]) != 0:
            continue
        if is_language_tag(tag):
            continue
        if is_neutral_tag(tag):
            continue
        return True
    return False


def language_rank(tags):
    """Lower is better. 0 if any tag indicates English-language support
    (e.g. "En" or "En,Fr,De"), else 1. Used by effective_region_rank(),
    and as a final tertiary tiebreaker in score_release() for the rare
    case of two releases tied even after that.
    """
    for tag in tags:
        parts = [p.strip().lower() for p in re.split(r"[,/]", tag)]
        if "en" in parts:
            return 0
    return 1


def effective_region_rank(tags, region_priority):
    """Lower is better. Combines region and confirmed-English-language
    availability into a single ranking, as (tier, region_rank):

      tier 0: the release's region is one of the top 2 configured
              regions (USA/World by default) -- both long-standing
              No-Intro/GoodTools conventions for including English
              content, so nothing else needs to outrank them.
      tier 1: not a top-2 region, but the release carries an explicit
              "En" language tag (or a list including it) -- confirms
              English text is actually present even though the nominal
              region alone doesn't guarantee it. E.g. a Japan release
              explicitly tagged "(En)" beats a plain "(Europe)" release,
              since "Europe" alone doesn't confirm English the way an
              explicit "(En)" tag does -- but a plain "(World)" release
              (tier 0) still beats it, since World already implies
              English by convention without needing an explicit tag.
      tier 2: everything else, ranked by the normal region_rank() order.

    The region_rank is kept as the second element so releases within the
    same tier still sort by their underlying region (e.g. two different
    tier-1 "(En)"-tagged releases, or two tier-2 releases with neither).
    """
    rrank = region_rank(tags, region_priority)
    if rrank < 2:
        return (0, rrank)
    if language_rank(tags) == 0:
        return (1, rrank)
    return (2, rrank)


def disc_format_rank(file_list):
    """Lower is better. A release containing a .chd is preferred over one
    made of raw disc-image files (.bin/.cue/.iso/etc) for the same game.
    Non-disc formats (cartridge ROMs like .zip/.nes) are neutral, since
    they never compete against CD-format releases anyway.
    """
    exts = {os.path.splitext(f)[1].lower() for f, _ in file_list}
    if ".chd" in exts:
        return 0
    if exts & RAW_DISC_EXTENSIONS:
        return 1
    return 0


def score_release(release_tags, total_size, region_priority, fmt_rank=0):
    """Lower score = better / preferred release.

    release_tags: the shared non-part tags for this release (region,
    revision, beta/proto flags, etc. -- NOT track/disc numbers).
    total_size: combined size in bytes of every file belonging to this
    release (e.g. all tracks of a multi-bin CD image).
    fmt_rank: disc_format_rank() for this release -- CHD beats raw bin/cue.
    """
    non_standard = 1 if has_non_standard_tag(release_tags, region_priority) else 0
    eff_tier, eff_rrank = effective_region_rank(release_tags, region_priority)
    lang_rank = language_rank(release_tags)
    rev = parse_revision(release_tags)
    # Prefer: CHD over raw disc images, effective region -- a top-2
    # configured region (USA/World by default), else a confirmed-English
    # ("En" tag) release, else normal region order (see
    # effective_region_rank) -- region/revision/language-only tags over
    # anything else at the SAME effective region tier (e.g. a "(World)
    # (Collection of SaGa)" release still beats a plain "(Japan)"
    # release, since World is a better tier and no plain-World release
    # exists to compete with instead), a final English-tag tiebreak for
    # the rare case two releases are otherwise still tied, higher
    # revision, larger total size (final tiebreak)
    return (fmt_rank, eff_tier, eff_rrank, non_standard, lang_rank, -rev, -total_size)


SECTION_RE = re.compile(r"^\[\s*(whitelist|blacklist|reject)\s*\]\s*$", re.IGNORECASE)


def parse_filter_line(line):
    """Parse one filter-file entry line.

    Returns (title_key, tag_set_or_None):
    - A plain title like "Chrono Trigger" -> (title_key, None): applies to
      the whole game, at title granularity.
    - A specific release like "Shadow Dancer (World)" -> (title_key,
      frozenset({"world"})): applies to any release whose own tags
      CONTAIN this set -- so you only need to type enough tags to
      distinguish the release, not its full tag list. E.g. writing just
      "(SEGA Classic Collection)" will still match a release actually
      tagged "(USA, Europe) (SEGA Classic Collection)".
    Track/disc-piece tags (Track 01, Disc 2, ...) are ignored here too,
    same as when parsing actual filenames. A trailing file extension is
    tolerated and stripped if present (e.g. pasting the full filename
    "Streets of Rage II (Japan, Europe) (En,Ja).7z" straight out of a
    directory listing still matches correctly) -- filter entries are
    meant to be the bare title/release name, but this is an easy mistake
    to make and silently produced a non-matching title otherwise.
    """
    stripped, ext = os.path.splitext(line)
    if ext.lower() in ROM_EXTENSIONS_DEFAULT:
        line = stripped

    base_title, tags = extract_tags(line)
    title_key = normalize_title(base_title) or normalize_title(line)
    if not tags:
        return title_key, None
    tag_set = frozenset(t.lower() for t in tags if not is_part_tag(t))
    return title_key, tag_set


def load_filter_file(path):
    """Parse a flat text file with [whitelist], [blacklist], and/or
    [reject] sections.

    Example:
        [whitelist]
        Chrono Trigger
        Shadow Dancer - The Secret of Shinobi (World)

        [blacklist]
        Some Beta Build
        Shadow Dancer - The Secret of Shinobi (SEGA Classic Collection)

        [reject]
        Arcade Legends Sega Mega Drive

    A line with no region/tag info (e.g. "Chrono Trigger") applies to the
    WHOLE title. A line that includes tags (e.g. "... (World)") targets
    any release whose tags contain those given (you don't need to spell
    out every tag -- just enough to identify the release), letting you
    pin which version should be kept, or force a specific version to
    always be treated as a duplicate.

    [reject] is different from [blacklist]: a release-specific [blacklist]
    entry only matters when it has competing releases to lose a comparison
    to, so it can't do anything for a release that's the ONLY copy of its
    title on hand -- e.g. a made-up compilation/utility title with no
    other release to compete against. [reject] always routes a matching
    file to .duplicates/Rejected/, with no duplicate comparison involved
    at all, and takes precedence over blacklist/whitelist scoping for that
    title.

    Blank lines and lines starting with # are ignored. Lines before the
    first section header are ignored.

    Returns a dict with six entries:
        {
          "whitelist_titles":   {title_key: line},
          "whitelist_releases": [(title_key, frozenset_of_tags, line), ...],
          "blacklist_titles":   {title_key: line},
          "blacklist_releases": [(title_key, frozenset_of_tags, line), ...],
          "reject_titles":      {title_key: line},
          "reject_releases":    [(title_key, frozenset_of_tags, line), ...],
        }
    """
    result = {
        "whitelist_titles": {}, "whitelist_releases": [],
        "blacklist_titles": {}, "blacklist_releases": [],
        "reject_titles": {}, "reject_releases": [],
    }
    current = None

    with open(path, "r", encoding="utf-8") as f:
        for raw_line in f:
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            m = SECTION_RE.match(line)
            if m:
                current = m.group(1).lower()
                continue
            if current is None:
                continue  # entry before any section header -- ignore

            title_key, tag_set = parse_filter_line(line)
            if not title_key:
                continue

            if tag_set is None:
                result["{0}_titles".format(current)][title_key] = line
            else:
                result["{0}_releases".format(current)].append((title_key, tag_set, line))

    return result


def resolve_filter_file(roms_dir, filter_file_arg):
    """Resolve and load the rom_filters.txt filter file for roms_dir, per
    the CLI's shared convention: an explicit filter_file_arg must exist
    (hard error if not); otherwise <roms_dir>/rom_filters.txt is used
    automatically if present. Warns if the resolved file has no
    [whitelist]/[blacklist]/[reject] entries at all.

    Returns (parsed, filter_file_used): parsed is the same dict
    load_filter_file() returns (all six keys empty if no filter file was
    found), and filter_file_used is the path actually used, or None.
    """
    filter_path = filter_file_arg or os.path.join(roms_dir, DEFAULT_FILTER_FILENAME)
    empty = {
        "whitelist_titles": {}, "whitelist_releases": [],
        "blacklist_titles": {}, "blacklist_releases": [],
        "reject_titles": {}, "reject_releases": [],
    }

    if filter_file_arg and not os.path.isfile(filter_path):
        print("Error: filter file not found: {0}".format(filter_path), file=sys.stderr)
        sys.exit(1)

    if not os.path.isfile(filter_path):
        return empty, None

    parsed = load_filter_file(filter_path)
    if not any([parsed["whitelist_titles"], parsed["whitelist_releases"],
                parsed["blacklist_titles"], parsed["blacklist_releases"],
                parsed["reject_titles"], parsed["reject_releases"]]):
        print("Warning: filter file '{0}' has no [whitelist]/[blacklist]/"
              "[reject] entries -- nothing will be filtered.".format(filter_path),
              file=sys.stderr)
    return parsed, filter_path


def walk_excluding_dup_dir(roms_dir, dup_dir):
    """os.walk(roms_dir), never descending into dup_dir.

    Every whole-tree scan in the tool needs this: dup_dir holds content a
    prior run already decided doesn't belong in the active library
    (duplicates, BIOS, proto/beta, redundant raw disc images, rejects --
    see plan_duplicate_scan). Without this exclusion, an operation that
    scans the whole tree treats that quarantined content as live library
    content again -- e.g. --convert-to-chd converting a .cue sitting in
    .duplicates/Redundant-Raw-Disc/ and placing a brand-new .chd back in
    roms_dir, silently resurrecting a release a prior scan had already
    rejected.

    dup_dir must already be an absolute path (every caller in this tool
    computes it that way from the start).
    """
    for root, dirs, files in os.walk(roms_dir):
        dirs[:] = [d for d in dirs if os.path.join(root, d) != dup_dir]
        if os.path.abspath(root) == dup_dir:
            continue
        yield root, dirs, files


def unique_dest_path(dest_dir, filename, also_avoid=None):
    """Avoid collisions when moving files with the same name into duplicates/.

    also_avoid: optional set of destination paths already claimed by other
    moves planned in this same run (but not yet performed, so they don't
    exist on disk yet) -- treated as taken too.
    """
    also_avoid = also_avoid or set()
    dest = os.path.join(dest_dir, filename)
    if not os.path.exists(dest) and dest not in also_avoid:
        return dest
    stem, ext = os.path.splitext(filename)
    i = 1
    while True:
        candidate = os.path.join(dest_dir, "{0} ({1}){2}".format(stem, i, ext))
        if not os.path.exists(candidate) and candidate not in also_avoid:
            return candidate
        i += 1


def remove_now_empty_dirs(roms_dir, dirs):
    """For each directory in dirs, remove it if every file that used to be
    in it has since been moved out (e.g. into .duplicates/) and it's now
    empty -- e.g. a per-release subfolder (multi-disc set, or a CD image
    whose .cue/.bin became redundant after --convert-to-chd) left behind
    once its contents are gone. If removing a directory leaves ITS parent
    empty too, that gets removed as well, continuing upward -- but this
    never removes roms_dir itself or anything above it, even if it
    technically ends up empty of everything but .duplicates/.

    Returns the set of directories actually removed.
    """
    roms_dir_abs = os.path.abspath(roms_dir)
    to_check = {os.path.abspath(d) for d in dirs}
    removed = set()

    while to_check:
        d = to_check.pop()
        if d in removed or d == roms_dir_abs or not d.startswith(roms_dir_abs + os.sep):
            continue
        try:
            if os.path.isdir(d) and not os.listdir(d):
                os.rmdir(d)
                removed.add(d)
                to_check.add(os.path.dirname(d))
        except OSError:
            pass

    return removed


def plan_flatten_alpha_dirs(roms_dir, dup_dir):
    """Find single-letter (or catch-all, e.g. "#"/"0-9"/"Misc"/"[BIOS]") bucket
    folders directly under roms_dir and build the list of moves needed to
    flatten their contents up into roms_dir.

    Each entry directly inside a bucket folder -- a file, or a whole
    subfolder such as a multi-disc release's own directory -- is moved as
    one unit, so nested release structure is preserved. Only direct
    children of roms_dir are considered; nested bucket folders deeper in
    the tree are left alone.

    Returns (moves, bucket_dirs):
        moves:       [(src_path, dest_path), ...]
        bucket_dirs: [bucket_dir_path, ...] to remove once emptied
    """
    moves = []
    bucket_dirs = []
    reserved = set()

    for entry in sorted(os.listdir(roms_dir)):
        full = os.path.join(roms_dir, entry)
        if not os.path.isdir(full) or os.path.abspath(full) == dup_dir:
            continue
        if not is_alpha_bucket_dirname(entry):
            continue

        bucket_dirs.append(full)
        for sub_entry in sorted(os.listdir(full)):
            src = os.path.join(full, sub_entry)
            dest = unique_dest_path(roms_dir, sub_entry, also_avoid=reserved)
            reserved.add(dest)
            moves.append((src, dest))

    return moves, bucket_dirs


def flatten_alpha_dirs(roms_dir, dup_dir, apply):
    """Print (and, if apply, perform) the moves from plan_flatten_alpha_dirs,
    then remove the emptied bucket folders. Returns (moved_count, removed_count).
    """
    moves, bucket_dirs = plan_flatten_alpha_dirs(roms_dir, dup_dir)
    if not bucket_dirs:
        return 0, 0

    print("\nAlphabetical bucket folders found ({0}): {1}".format(
        len(bucket_dirs), ", ".join(os.path.basename(d) for d in bucket_dirs)))
    for src, dest in moves:
        print("  [FLATTEN] {0}  ->  {1}".format(
            os.path.relpath(src, roms_dir), os.path.relpath(dest, roms_dir)))

    if not apply:
        print("\nDRY RUN -- would move {0} item(s) out of {1} bucket folder(s) "
              "and remove them. Re-run with --apply to do it.".format(
                  len(moves), len(bucket_dirs)))
        return len(moves), 0

    moved = 0
    for src, dest in moves:
        try:
            shutil.move(src, dest)
            moved += 1
        except OSError as e:
            print("  ERROR moving {0} -> {1}: {2}".format(src, dest, e), file=sys.stderr)

    removed = 0
    for d in bucket_dirs:
        try:
            os.rmdir(d)
            removed += 1
        except OSError as e:
            print("  Warning: could not remove {0}: {1}".format(d, e), file=sys.stderr)

    print("\nFlattened {0}/{1} item(s), removed {2}/{3} bucket folder(s).".format(
        moved, len(moves), removed, len(bucket_dirs)))
    return moved, removed


# ---- gamelist.xml cleanup ---------------------------------------------

GAMELIST_FILENAME = "gamelist.xml"

# ES-DE/EmulationStation marker used by some libretro cores' auto-generated
# entries (BIOS setup menus, core config screens, etc.) that aren't actual
# games -- these get tagged with this string somewhere in their <game>
# block so they can be filtered out of the game list.
GAMELIST_NOTGAME_MARKER = "ZZZ(notgame)"

GAMELIST_GAME_BLOCK_RE = re.compile(r"<game\b[^>]*>.*?</game>", re.DOTALL | re.IGNORECASE)

# Hidden backup written alongside each gamelist.xml right before it's
# overwritten, holding the pre-clean content. Overwritten (not versioned)
# on every re-run, so it only ever holds the most recent original.
GAMELIST_BACKUP_FILENAME = ".rom-cleanup-gamelist-xml.bak"


def find_gamelists(roms_dir, dup_dir):
    """Recursively find every gamelist.xml under roms_dir -- covers both
    pointing this at a single console folder (gamelist.xml directly inside)
    and at a top-level ROMs folder containing one console subfolder per
    system, each with its own gamelist.xml. Never descends into dup_dir
    (see walk_excluding_dup_dir).
    """
    found = []
    for root, dirs, files in walk_excluding_dup_dir(roms_dir, dup_dir):
        for fname in files:
            if fname.lower() == GAMELIST_FILENAME:
                found.append(os.path.join(root, fname))
    return sorted(found)


def _gamelist_entry_label(block):
    """Best-effort human-readable label for a <game> block, for preview
    output -- prefers <name>, falls back to <path>, then a generic marker.
    """
    m = re.search(r"<name>(.*?)</name>", block, re.DOTALL | re.IGNORECASE)
    if m and m.group(1).strip():
        return m.group(1).strip()
    m = re.search(r"<path>(.*?)</path>", block, re.DOTALL | re.IGNORECASE)
    if m and m.group(1).strip():
        return m.group(1).strip()
    return "(unnamed entry)"


def plan_gamelist_clean(path):
    """Find <game>...</game> blocks in this gamelist.xml containing the
    "notgame" marker and build the cleaned file content with those blocks
    removed.

    Validates the file is well-formed XML both before touching it and
    after removing the matched blocks, so a malformed or unexpectedly
    structured gamelist.xml is left untouched rather than risking a
    corrupted write. Raises xml.etree.ElementTree.ParseError if either
    check fails.

    Returns (matches, cleaned_text): matches is the list of raw block
    strings that matched (for preview), empty if none did; cleaned_text is
    None when there's nothing to remove.
    """
    with open(path, "r", encoding="utf-8") as f:
        original = f.read()

    ET.fromstring(original)  # raises ParseError if not well-formed

    matches = []

    def _strip_if_notgame(m):
        block = m.group(0)
        if GAMELIST_NOTGAME_MARKER in block:
            matches.append(block)
            return ""
        return block

    cleaned = GAMELIST_GAME_BLOCK_RE.sub(_strip_if_notgame, original)
    if not matches:
        return [], None

    # Collapse any run of blank/whitespace-only lines left behind by
    # removed blocks (e.g. several consecutive removed entries) down to a
    # single newline, however many were removed in a row.
    cleaned = re.sub(r"\n(?:[ \t]*\n)+", "\n", cleaned)

    ET.fromstring(cleaned)  # sanity-check the edit didn't break the XML
    return matches, cleaned


def gamelist_clean(roms_dir, dup_dir, apply):
    """Find every gamelist.xml under roms_dir and remove <game> entries
    tagged with the ES-DE "notgame" marker, so libretro core setup/config
    entries don't show up as playable games. Returns (files_changed,
    entries_removed).

    Before overwriting a gamelist.xml, its pre-clean content is copied to
    a hidden ".rom-cleanup-gamelist-xml.bak" next to it (overwritten on each re-run, so
    it only ever holds the most recent original).
    """
    gamelists = find_gamelists(roms_dir, dup_dir)
    if not gamelists:
        print("No {0} files found under {1}.".format(GAMELIST_FILENAME, roms_dir))
        return 0, 0

    files_changed = 0
    total_entries = 0

    for path in gamelists:
        try:
            matches, cleaned = plan_gamelist_clean(path)
        except ET.ParseError as e:
            print("  Warning: skipping {0} -- not well-formed XML ({1})".format(
                os.path.relpath(path, roms_dir), e), file=sys.stderr)
            continue

        if not matches:
            continue

        print("\n{0}  ({1} \"notgame\" entr{2} found)".format(
            os.path.relpath(path, roms_dir), len(matches),
            "y" if len(matches) == 1 else "ies"))
        for block in matches:
            print("  [REMOVE] {0}".format(_gamelist_entry_label(block)))

        files_changed += 1
        total_entries += len(matches)

        if apply:
            backup_path = os.path.join(os.path.dirname(path), GAMELIST_BACKUP_FILENAME)
            shutil.copyfile(path, backup_path)
            print("  [BACKUP] {0}".format(os.path.relpath(backup_path, roms_dir)))
            with open(path, "w", encoding="utf-8") as f:
                f.write(cleaned)

    if total_entries == 0:
        print("\nNo \"{0}\" entries found in {1} gamelist.xml file(s).".format(
            GAMELIST_NOTGAME_MARKER, len(gamelists)))
        return 0, 0

    if not apply:
        print("\nDRY RUN -- would remove {0} entr{1} across {2} file(s). "
              "Re-run with --apply to do it.".format(
                  total_entries, "y" if total_entries == 1 else "ies", files_changed))
    else:
        print("\nRemoved {0} entr{1} across {2} file(s).".format(
            total_entries, "y" if total_entries == 1 else "ies", files_changed))

    return files_changed, total_entries


# ---- bin/cue -> CHD conversion -----------------------------------------

CUE_EXTENSION = ".cue"
CHD_EXTENSION = ".chd"
ISO_EXTENSION = ".iso"

# CD track types a synthesized single-track .cue can declare for a
# standalone .iso -- chdman's createcd needs a .cue even for one track,
# so one gets generated on the fly rather than requiring the user to
# hand-author one. Which of these is correct depends on how the .iso was
# originally dumped, not on the file extension:
#   MODE2/2352 -- a raw sector dump (every sector carries the CD-ROM sync
#                 pattern) -- the standard choice for most CD-XA-derived
#                 systems' single-data-track discs (PSX in particular).
#   MODE1/2048 -- a plain ISO9660 data image with no raw sector wrapper,
#                 one 2048-byte block of user data per sector.
# This is a heuristic, not a certainty: it correctly identifies WHICH of
# these two shapes the file has, but within the raw-sector case it can't
# distinguish finer sub-modes some platforms occasionally need -- if a
# specific title doesn't play right after conversion, that's the first
# thing to check by hand.
ISO_TRACK_MODE_RAW = "MODE2/2352"
ISO_TRACK_MODE_DATA = "MODE1/2048"

RAW_SECTOR_SIZE = 2352
DATA_SECTOR_SIZE = 2048

# The 12-byte sync pattern every raw CD-ROM sector begins with (Yellow
# Book / ECMA-130) -- present in a raw 2352-byte sector dump, absent from
# a plain 2048-byte ISO9660 image. This is what actually distinguishes
# the two track types below; file size alone isn't enough; since a size
# that happens to be a multiple of both 2352 and 2048 (any multiple of
# 301,056 bytes) would otherwise be ambiguous.
CD_SECTOR_SYNC_PATTERN = b"\x00\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\x00"


def find_cue_files(roms_dir, dup_dir):
    """Recursively find every .cue file under roms_dir. Never descends
    into dup_dir (see walk_excluding_dup_dir) -- a .cue sitting there was
    already routed away as a duplicate or redundant-alongside-its-chd
    leftover by a prior scan, not something to convert and place back
    into roms_dir as a new .chd.
    """
    found = []
    for root, dirs, files in walk_excluding_dup_dir(roms_dir, dup_dir):
        for fname in files:
            if fname.lower().endswith(CUE_EXTENSION):
                found.append(os.path.join(root, fname))
    return sorted(found)


# Matches a cue sheet's "FILE ..." lines, e.g. FILE "Track01.bin" BINARY
# or the unquoted form FILE Track01.bin BINARY.
CUE_FILE_LINE_RE = re.compile(r'^\s*FILE\s+(?:"([^"]+)"|(\S+))', re.IGNORECASE | re.MULTILINE)


def parse_cue_file_references(cue_path):
    """Return the filenames referenced by FILE lines in this .cue, in the
    order they appear, exactly as written in the cue sheet.
    """
    with open(cue_path, "r", encoding="utf-8", errors="replace") as f:
        text = f.read()
    return [m.group(1) if m.group(1) is not None else m.group(2)
            for m in CUE_FILE_LINE_RE.finditer(text)]


def find_cue_case_mismatches(cue_path):
    """Check that every file referenced by this .cue actually exists (with
    matching case) next to it. Cue sheets are often authored on
    case-insensitive filesystems (Windows) and then moved to a
    case-sensitive one (Linux), where an exact-case reference like
    FILE "GAME.BIN" silently fails to match an on-disk "game.bin" --
    chdman's resulting error message is unhelpful (it doesn't clearly
    name the actual missing file), so this catches it upfront.

    Returns a list of (referenced_name, resolution) tuples for every
    referenced file that ISN'T present under its exact referenced name.
    resolution is the actual on-disk filename if exactly one
    case-insensitive match was found in the same folder (safe to rename
    into place), or None if there's no match at all, or more than one
    (ambiguous) -- either way, a human needs to sort it out. An empty
    list means every referenced file already matches exactly.
    """
    cue_dir = os.path.dirname(cue_path)
    try:
        on_disk = os.listdir(cue_dir)
    except OSError:
        on_disk = []
    # Exact-match check is a plain (case-sensitive) string comparison
    # against the directory listing, not os.path.isfile() -- isfile()
    # follows the *host* filesystem's case-sensitivity, which would make
    # this silently a no-op when run on a case-insensitive filesystem
    # (Windows/default macOS) even though the referenced name and the
    # on-disk name genuinely differ in case.
    on_disk_exact = set(on_disk)
    on_disk_by_lower = defaultdict(list)
    for name in on_disk:
        on_disk_by_lower[name.lower()].append(name)

    mismatches = []
    for referenced in parse_cue_file_references(cue_path):
        if referenced in on_disk_exact:
            continue
        candidates = on_disk_by_lower.get(referenced.lower(), [])
        resolution = candidates[0] if len(candidates) == 1 else None
        mismatches.append((referenced, resolution))
    return mismatches


def find_lone_iso_files(roms_dir, dup_dir):
    """Recursively find every standalone .iso under roms_dir that ISN'T
    already referenced by a .cue sheet sitting in the same folder -- some
    cue sheets name an .iso as their FILE line instead of a .bin, and
    that .iso is already handled via the normal cue-conversion path, so
    counting it again here would produce a second, redundant .chd for the
    same release. Never descends into dup_dir (see walk_excluding_dup_dir).
    """
    isos_by_dir = defaultdict(list)
    referenced_by_dir = defaultdict(set)

    for root, dirs, files in walk_excluding_dup_dir(roms_dir, dup_dir):
        for fname in files:
            lower = fname.lower()
            if lower.endswith(ISO_EXTENSION):
                isos_by_dir[root].append(fname)
            elif lower.endswith(CUE_EXTENSION):
                referenced_by_dir[root] |= set(
                    parse_cue_file_references(os.path.join(root, fname)))

    found = []
    for root, names in isos_by_dir.items():
        referenced = referenced_by_dir.get(root, set())
        for fname in names:
            if fname not in referenced:
                found.append(os.path.join(root, fname))
    return sorted(found)


def detect_iso_track_mode(iso_path):
    """Work out which CD track type a synthesized .cue should declare
    for this standalone .iso, by inspecting the file itself rather than
    guessing from its name or extension. See ISO_TRACK_MODE_RAW/_DATA.

    Returns None if the file size doesn't cleanly divide into either
    sector size, or divides by 2352 without the sync pattern to back it
    up -- an unrecognized layout is left for a human to sort out rather
    than guessed at.
    """
    size = os.path.getsize(iso_path)
    if size > 0 and size % RAW_SECTOR_SIZE == 0:
        with open(iso_path, "rb") as f:
            header = f.read(len(CD_SECTOR_SYNC_PATTERN))
        if header == CD_SECTOR_SYNC_PATTERN:
            return ISO_TRACK_MODE_RAW
    if size > 0 and size % DATA_SECTOR_SIZE == 0:
        return ISO_TRACK_MODE_DATA
    return None


def synthesize_cue_text(iso_filename, track_mode):
    """Build the text of a minimal single-track .cue sheet for a
    standalone .iso, so it can go through chdman's createcd the same way
    a real .cue does -- createcd needs a .cue even for a single track.
    """
    return (
        'FILE "{0}" BINARY\n'
        '  TRACK 01 {1}\n'
        '    INDEX 01 00:00:00\n'
    ).format(iso_filename, track_mode)


def find_chdman(chdman_path=None):
    """Locate the chdman executable (ships with mame-tools / MAME). Checks
    an explicit override first (also resolved against PATH, so a bare
    command name works), falling back to looking up "chdman" on PATH.
    Returns the resolved path, or None if it can't be found.
    """
    if chdman_path:
        return shutil.which(chdman_path) or (chdman_path if os.path.isfile(chdman_path) else None)
    return shutil.which("chdman")


def _plan_one_conversion(source_path, roms_dir, reserved):
    """Shared destination bookkeeping for one convertible source file
    (.cue or .iso): work out where its .chd should end up -- directly
    alongside the source if it's already sitting right in roms_dir, or
    moved up into roms_dir itself otherwise (same "flatten up to
    roms_dir" convention as --flatten-alpha-dirs) -- so a CD release that
    lives in its own subfolder ends up with its .chd sitting flat
    alongside the rest of the roms.

    Returns (working_chd_path, final_chd_path, needs_move, already_done).
    already_done is True if a .chd with the matching name already exists
    at the destination -- makes repeated runs cheap and resumable instead
    of reconverting everything every time; working/final_chd_path are
    both None in that case.
    """
    source_dir = os.path.dirname(source_path)
    chd_name = os.path.splitext(os.path.basename(source_path))[0] + CHD_EXTENSION
    needs_move = os.path.abspath(source_dir) != os.path.abspath(roms_dir)
    dest_dir = roms_dir if needs_move else source_dir

    if os.path.exists(os.path.join(dest_dir, chd_name)):
        return None, None, needs_move, True

    working_chd_path = os.path.join(source_dir, chd_name)
    final_chd_path = (unique_dest_path(roms_dir, chd_name, also_avoid=reserved)
                       if needs_move else working_chd_path)
    reserved.add(final_chd_path)
    return working_chd_path, final_chd_path, needs_move, False


def plan_chd_conversion(roms_dir, dup_dir):
    """Find every .cue file, plus every standalone .iso not already
    claimed by one (see find_lone_iso_files), under roms_dir and work out
    which still need converting and where each resulting .chd should end
    up (see _plan_one_conversion).

    Returns (to_convert, already_done, unrecognized_isos):
        to_convert:   [(source_path, working_chd_path, final_chd_path,
                        needs_move, source_kind, track_mode), ...]
                      source_kind is "cue" or "iso". track_mode is the
                      ISO_TRACK_MODE_RAW/_DATA the synthesized .cue will
                      declare for an "iso" entry (see
                      detect_iso_track_mode), always None for a "cue"
                      entry (a real .cue already declares its own track
                      types).
        already_done: [source_path, ...]
        unrecognized_isos: [iso_path, ...] -- a standalone .iso whose
                      sector layout couldn't be determined, left
                      untouched rather than guessed at.
    """
    to_convert = []
    already_done = []
    unrecognized_isos = []
    reserved = set()

    for cue_path in find_cue_files(roms_dir, dup_dir):
        working_chd_path, final_chd_path, needs_move, done = _plan_one_conversion(
            cue_path, roms_dir, reserved)
        if done:
            already_done.append(cue_path)
            continue
        to_convert.append((cue_path, working_chd_path, final_chd_path, needs_move, "cue", None))

    for iso_path in find_lone_iso_files(roms_dir, dup_dir):
        track_mode = detect_iso_track_mode(iso_path)
        if track_mode is None:
            unrecognized_isos.append(iso_path)
            continue

        working_chd_path, final_chd_path, needs_move, done = _plan_one_conversion(
            iso_path, roms_dir, reserved)
        if done:
            already_done.append(iso_path)
            continue
        to_convert.append((iso_path, working_chd_path, final_chd_path, needs_move, "iso", track_mode))

    return to_convert, already_done, unrecognized_isos


def _print_progress(text, use_inplace):
    """Print one progress update. When use_inplace is True (a real
    terminal, not a pipe/log file), overwrites the previous update on the
    same line via a carriage return, padded to the terminal width so
    nothing lingers from a longer previous line -- otherwise printed as a
    plain new line, since overwriting doesn't make sense once output is
    redirected (it would just corrupt a log file with a string of \\r
    characters instead of one line per item).
    """
    if use_inplace:
        width = shutil.get_terminal_size(fallback=(80, 24)).columns
        print("\r" + text[:width - 1].ljust(width - 1), end="", flush=True)
    else:
        print(text)


def convert_to_chd(roms_dir, dup_dir, apply, chdman_path=None):
    """Find every .cue file, plus every standalone .iso not already
    claimed by one, under roms_dir, convert each to .chd via 'chdman
    createcd', and move the result up into roms_dir if it wasn't already
    sitting directly there. Returns (converted, skipped, errors).

    A standalone .iso has no .cue of its own -- createcd needs one even
    for a single track, so one is synthesized on the fly right next to
    the .iso (see synthesize_cue_text), handed to chdman in its place,
    and removed again afterward regardless of outcome. Which CD track
    type it declares is detected from the .iso itself (see
    detect_iso_track_mode); an .iso whose sector layout can't be
    determined is left alone and reported rather than guessed at.

    Before handing a real .cue to chdman, checks every file it
    references actually exists under that exact name (see
    find_cue_case_mismatches) -- a common problem on case-sensitive
    filesystems is a cue sheet written on Windows referencing e.g.
    "GAME.BIN" when the real file on disk is "game.bin". When there's
    exactly one case-insensitive match, it's renamed into place to match
    what the cue expects (only when apply is True); when it's ambiguous
    or missing entirely, that .cue is reported as blocked and skipped
    rather than being handed to chdman, whose own error message for this
    case doesn't clearly name the actual missing file. This check doesn't
    apply to a synthesized .cue -- it's written with the .iso's own exact
    on-disk name, so it can't mismatch.

    The original .bin/.cue/.iso files are left in place -- run the normal
    duplicate scan with --apply afterward and it will automatically route
    them into .duplicates/Redundant-Raw-Disc/, since it already detects a
    .chd alongside raw disc files for the same release.
    """
    chdman = find_chdman(chdman_path)
    if not chdman:
        print("Error: chdman not found{0}. It ships with mame-tools (Debian/"
              "Ubuntu: 'sudo apt install mame-tools'); on other platforms, "
              "install MAME/mame-tools and make sure chdman is on PATH, or "
              "pass --chdman-path.".format(
                  " at '{0}'".format(chdman_path) if chdman_path else ""),
              file=sys.stderr)
        sys.exit(1)

    to_convert, already_done, unrecognized_isos = plan_chd_conversion(roms_dir, dup_dir)
    if not to_convert and not already_done and not unrecognized_isos:
        print("No {0}/{1} files found under {2}.".format(CUE_EXTENSION, ISO_EXTENSION, roms_dir))
        return 0, 0, 0

    for source_path in already_done:
        print("[SKIP] {0}  (already converted)".format(os.path.relpath(source_path, roms_dir)))

    for iso_path in unrecognized_isos:
        print("\n  Warning: {0!r} -- size doesn't match a recognized CD sector "
              "layout ({1}-byte raw or {2}-byte data sectors), skipping, needs "
              "manual review.".format(
                  os.path.relpath(iso_path, roms_dir), RAW_SECTOR_SIZE, DATA_SECTOR_SIZE),
              file=sys.stderr)

    convertible = []
    blocked = len(unrecognized_isos)

    for source_path, working_chd_path, final_chd_path, needs_move, source_kind, track_mode in to_convert:
        if not apply:
            # In apply mode this is printed later, right before each
            # conversion actually starts (see the progress loop below) --
            # printing the whole list here first is what used to make a
            # long run look like it dumped everything and then went
            # silent, with no way to tell which one was actually running.
            note = "  (synthesizing {0} cue sheet)".format(track_mode) if source_kind == "iso" else ""
            print("[CONVERT] {0}{1}{2}".format(
                os.path.relpath(source_path, roms_dir),
                "  ->  {0}".format(os.path.relpath(final_chd_path, roms_dir)) if needs_move else "",
                note))

        if source_kind == "cue":
            mismatches = find_cue_case_mismatches(source_path)
            source_dir = os.path.dirname(source_path)
            rename_failed = False

            if apply and mismatches:
                # Not printed above for every file in apply mode (see the
                # comment there), but a mismatch needs fixing/reporting, so
                # the [CASE-FIX]/error line(s) below need this for context.
                print("[CONVERT] {0}".format(os.path.relpath(source_path, roms_dir)))

            for referenced, actual in mismatches:
                if actual is None:
                    print("  ERROR: references '{0}', which doesn't exist (even "
                          "case-insensitively) in its folder.".format(referenced),
                          file=sys.stderr)
                    continue
                verb = "Renamed" if apply else "Would rename"
                print("  [CASE-FIX] {0} '{1}' -> '{2}' (to match what the .cue "
                      "references)".format(verb, actual, referenced))
                if apply:
                    try:
                        os.rename(os.path.join(source_dir, actual), os.path.join(source_dir, referenced))
                    except OSError as e:
                        rename_failed = True
                        print("    ERROR: rename failed: {0}".format(e), file=sys.stderr)

            if any(actual is None for _, actual in mismatches) or rename_failed:
                blocked += 1
                continue

        convertible.append(
            (source_path, working_chd_path, final_chd_path, needs_move, source_kind, track_mode))

    if not apply:
        print("\nDRY RUN -- would convert {0} file(s) to .chd ({1} already "
              "converted, skipped{2}). Re-run with --apply to do it.".format(
                  len(convertible), len(already_done),
                  "; {0} blocked, needs manual review".format(blocked) if blocked else ""))
        return len(convertible), len(already_done), blocked

    converted = 0
    errors = blocked
    use_inplace = sys.stdout.isatty()
    total = len(convertible)
    for i, (source_path, working_chd_path, final_chd_path, needs_move, source_kind, track_mode) in enumerate(convertible, 1):
        note = "  (synthesizing {0} cue sheet)".format(track_mode) if source_kind == "iso" else ""
        _print_progress("[{0}/{1}] Converting: {2}{3}".format(
            i, total, os.path.relpath(source_path, roms_dir), note), use_inplace)

        synthesized_cue_path = None
        convert_input = source_path
        if source_kind == "iso":
            synthesized_cue_path = os.path.splitext(source_path)[0] + CUE_EXTENSION
            cue_text = synthesize_cue_text(os.path.basename(source_path), track_mode)
            with open(synthesized_cue_path, "w", encoding="utf-8", newline="\n") as f:
                f.write(cue_text)
            convert_input = synthesized_cue_path

        try:
            result = subprocess.run(
                [chdman, "createcd", "-i", convert_input, "-o", working_chd_path, "-f"],
                capture_output=True, text=True)
        finally:
            if synthesized_cue_path is not None:
                try:
                    os.remove(synthesized_cue_path)
                except OSError as e:
                    print("  Warning: could not remove synthesized cue sheet {0}: "
                          "{1}".format(os.path.relpath(synthesized_cue_path, roms_dir), e),
                          file=sys.stderr)
        if result.returncode != 0:
            errors += 1
            if use_inplace:
                print()  # seal the in-place line before an unrelated stderr write
            print("  ERROR converting {0}: {1}".format(
                os.path.relpath(source_path, roms_dir),
                (result.stderr or result.stdout).strip()), file=sys.stderr)
            continue

        if needs_move:
            shutil.move(working_chd_path, final_chd_path)

        converted += 1

    print("\nConverted {0}/{1} file(s) to .chd ({2} already converted, "
          "skipped{3}).".format(
              converted, len(to_convert), len(already_done),
              "; {0} error(s)".format(errors) if errors else ""))
    if converted:
        print("Run the normal duplicate scan with --apply to route the now-"
              "redundant .bin/.cue/.iso files into .duplicates/Redundant-Raw-Disc/.")

    return converted, len(already_done), errors


# ---- multi-disc M3U playlist grouping -----------------------------------

M3U_EXTENSION = ".m3u"

# Dolphin's own compressed disc format for GameCube/Wii, same role CHD
# plays for CD-based systems -- unlike CHD, there's no conversion step
# here (see --convert-to-chd): an RVZ dump is typically already in this
# format when downloaded/ripped, nothing to compress in place.
RVZ_EXTENSION = ".rvz"

# Multi-disc container formats --make-m3u groups. Each format's hidden
# disc folder (see below) is named after its own extension, so adding a
# new format here is enough to also make it a valid hidden-folder name.
M3U_DISC_EXTENSIONS = {CHD_EXTENSION, RVZ_EXTENSION}

# Every grouped release's disc files live under a hidden folder directly
# in roms_dir, named after that release's OWN format (e.g.
# roms_dir/.chd/Game (USA)/Game (USA) (Disc 1).chd for a CD-based system,
# roms_dir/.rvz/Game (USA)/Game (USA) (Disc 1).rvz for GameCube/Wii) --
# one dot-prefixed folder per release, nested under one shared per-format
# folder, rather than scattering a dot-prefixed folder per release across
# roms_dir itself. Reuses M3U_DISC_EXTENSIONS directly since a hidden
# folder's name and its format's extension are, by design, the same string.
M3U_HIDDEN_DIR_NAMES = M3U_DISC_EXTENSIONS

# Tags that identify one DISC of a multi-disc release, e.g. "(Disc 1)",
# "(Disc 2)", "(CD1)", "(Disk 3)" -- distinct from PART_TAG_RE, which also
# matches "Track N" (a piece WITHIN one disc's own cue sheet, already
# absorbed into that disc's single .chd, not a separate disc of its own).
DISC_TAG_RE = re.compile(r"^(?:disc|disk|cd)\s*([0-9]+)$", re.IGNORECASE)


def parse_disc_number(tag):
    """Return the disc number if this tag identifies one disc of a
    multi-disc release (e.g. "Disc 2" -> 2), or None if it doesn't.
    """
    m = DISC_TAG_RE.match(tag.strip())
    return int(m.group(1)) if m else None


def find_disc_files(roms_dir, dup_dir, extensions):
    """Recursively find every file under roms_dir whose extension is one
    of the given multi-disc container formats (see M3U_DISC_EXTENSIONS).
    Never descends into dup_dir (see walk_excluding_dup_dir).
    """
    found = []
    for root, dirs, files in walk_excluding_dup_dir(roms_dir, dup_dir):
        for fname in files:
            if os.path.splitext(fname)[1].lower() in extensions:
                found.append(os.path.join(root, fname))
    return sorted(found)


def plan_m3u_grouping(roms_dir, dup_dir):
    """Find every disc-tagged file in a supported multi-disc format (see
    M3U_DISC_EXTENSIONS -- e.g. "Game (USA) (Disc 1).chd" for a CD-based
    system, "Game (USA) (Disc 1).rvz" for GameCube/Wii) and group
    same-title, same-tag, same-format sets of 2+ discs so a frontend sees
    ONE entry for the release: the .m3u playlist sits directly in
    roms_dir (e.g. "Game (USA).m3u"), while the actual disc files move
    into a per-release folder nested under a hidden folder in roms_dir
    named after their own format (e.g. ".chd/Game (USA)/" or
    ".rvz/Game (USA)/") -- ES-DE and RetroArch both ignore dot-prefixed
    directories when scanning, so only the .m3u shows up, not a second
    folder entry for the same game. Different formats never group
    together even if title/tags otherwise match, since that shouldn't
    happen for a real release and would produce a nonsensical mixed group.
    Never descends into dup_dir (see walk_excluding_dup_dir).

    A lone disc-tagged file with no siblings sharing its title/tags/format
    (only "Disc 1" ever found, no "Disc 2") is left alone -- nothing to
    group. Two files claiming the SAME disc number for the same
    title/tags/format are ambiguous (can't tell which one is really "Disc
    1") and are also left alone, flagged for manual review rather than
    guessing.

    A group already sitting in its target hidden folder with an
    up-to-date root .m3u is treated as already done -- makes repeated runs
    cheap and resumable, and self-healing if a disc is added/removed later
    (the .m3u content is compared, not just its existence). A release
    still sitting in an older layout (disc files and .m3u together in a
    visible "Game (USA)/" folder, or discs in a dot-prefixed per-release
    folder directly in roms_dir, from before this per-format-nested layout
    existed) is NOT considered done -- it's picked up in to_group and
    migrated into the current layout on --apply.

    Returns (to_group, already_done, ambiguous):
        to_group:     [(hidden_dir_path, m3u_path, discs), ...] where discs
                      is [(current_path, final_path, needs_move), ...]
                      sorted by disc number. hidden_dir_path's own parent
                      folder name is that release's format (".chd"/".rvz").
        already_done: [hidden_dir_path, ...]
        ambiguous:    [(title, non_disc_tags, [disc_path, ...]), ...]
    """
    # (title_key, tags_key, ext) -> [(disc_num, disc_path, title, non_disc_tags), ...]
    groups = defaultdict(list)

    for disc_path in find_disc_files(roms_dir, dup_dir, M3U_DISC_EXTENSIONS):
        ext = os.path.splitext(disc_path)[1].lower()
        stem = os.path.splitext(os.path.basename(disc_path))[0]
        title, tags = extract_tags(stem)

        disc_num = None
        disc_tag = None
        for t in tags:
            n = parse_disc_number(t)
            if n is not None:
                disc_num, disc_tag = n, t
                break
        if disc_num is None:
            continue

        non_disc_tags = [t for t in tags if t is not disc_tag]
        title_key = normalize_title(title)
        tags_key = tuple(sorted(t.lower() for t in non_disc_tags))
        groups[(title_key, tags_key, ext)].append((disc_num, disc_path, title, non_disc_tags))

    to_group = []
    already_done = []
    ambiguous = []

    for (_, _, ext), entries in sorted(groups.items()):
        if len(entries) < 2:
            continue  # lone disc -- nothing to group

        disc_nums = [e[0] for e in entries]
        if len(set(disc_nums)) != len(disc_nums):
            title = entries[0][2]
            non_disc_tags = entries[0][3]
            ambiguous.append((title, non_disc_tags, [e[1] for e in entries]))
            continue

        entries.sort(key=lambda e: e[0])
        title = entries[0][2]
        non_disc_tags = entries[0][3]
        release_name = title + "".join(" ({0})".format(t) for t in non_disc_tags)
        hidden_dir_path = os.path.join(roms_dir, ext, release_name)
        m3u_path = os.path.join(roms_dir, release_name + M3U_EXTENSION)

        reserved = set()
        discs = []
        for disc_num, disc_path, _, _ in entries:
            needs_move = os.path.abspath(os.path.dirname(disc_path)) != os.path.abspath(hidden_dir_path)
            if needs_move:
                final_path = unique_dest_path(hidden_dir_path, os.path.basename(disc_path), also_avoid=reserved)
            else:
                final_path = disc_path
            reserved.add(final_path)
            discs.append((disc_path, final_path, needs_move))

        if not any(needs_move for _, _, needs_move in discs):
            expected_lines = [
                ext + "/" + release_name + "/" + os.path.basename(final_path)
                for _, final_path, _ in discs]
            existing_lines = None
            if os.path.isfile(m3u_path):
                with open(m3u_path, "r", encoding="utf-8") as f:
                    existing_lines = [line.strip() for line in f if line.strip()]
            if existing_lines == expected_lines:
                already_done.append(hidden_dir_path)
                continue

        to_group.append((hidden_dir_path, m3u_path, discs))

    return to_group, already_done, ambiguous


def make_m3u_playlists(roms_dir, dup_dir, apply):
    """Print (and, if apply, perform) the grouping plan from
    plan_m3u_grouping: move each multi-disc release's disc files into its
    own subfolder under a hidden folder in roms_dir named after that
    release's format (".chd/" for CD-based systems, ".rvz/" for
    GameCube/Wii), and write an .m3u playlist -- listing them in disc
    order -- directly in roms_dir. Returns (grouped, already_done).

    Migrating a release out of an older layout also removes that layout's
    now-stale .m3u (the one that used to sit next to the disc files, in
    the original same-folder layout) once its discs have moved out, so it
    doesn't linger as a duplicate playlist.

    Any source folder left empty by the move is cleaned up (see
    remove_now_empty_dirs), same as the normal scan's --apply step.
    """
    to_group, already_done, ambiguous = plan_m3u_grouping(roms_dir, dup_dir)

    if not to_group and not already_done and not ambiguous:
        print("No multi-disc {0} releases found under {1}.".format(
            "/".join(sorted(M3U_DISC_EXTENSIONS)), roms_dir))
        return 0, 0

    def release_name_of(hidden_dir_path):
        return os.path.basename(hidden_dir_path)

    for hidden_dir_path in already_done:
        print("[SKIP] {0}  (already grouped)".format(release_name_of(hidden_dir_path)))

    for title, non_disc_tags, disc_paths in ambiguous:
        label = title + "".join(" ({0})".format(t) for t in non_disc_tags)
        print("\n  Warning: {0!r} has multiple disc files claiming the same "
              "disc number -- skipping, needs manual review:".format(label),
              file=sys.stderr)
        for p in sorted(disc_paths):
            print("    {0}".format(os.path.relpath(p, roms_dir)), file=sys.stderr)

    for hidden_dir_path, m3u_path, discs in to_group:
        print("\n{0}".format(release_name_of(hidden_dir_path)))
        for current_path, final_path, needs_move in discs:
            if needs_move:
                print("  [MOVE] {0}  ->  {1}".format(
                    os.path.relpath(current_path, roms_dir),
                    os.path.relpath(final_path, roms_dir)))
            else:
                print("  {0}".format(os.path.relpath(final_path, roms_dir)))
        print("  [M3U] {0}".format(os.path.relpath(m3u_path, roms_dir)))

    if not apply:
        print("\nDRY RUN -- would group {0} multi-disc release(s) ({1} "
              "already grouped, skipped{2}). Re-run with --apply to do it.".format(
                  len(to_group), len(already_done),
                  "; {0} ambiguous, needs manual review".format(len(ambiguous)) if ambiguous else ""))
        return len(to_group), len(already_done)

    grouped = 0
    errors = 0
    source_dirs = set()
    for hidden_dir_path, m3u_path, discs in to_group:
        release_name = release_name_of(hidden_dir_path)
        hidden_dir_name = os.path.basename(os.path.dirname(hidden_dir_path))
        relative_dir = hidden_dir_name + "/" + release_name
        stale_m3u_name = release_name + M3U_EXTENSION
        try:
            os.makedirs(hidden_dir_path, exist_ok=True)
            for current_path, final_path, needs_move in discs:
                if needs_move:
                    source_dir = os.path.dirname(current_path)
                    source_dirs.add(source_dir)
                    shutil.move(current_path, final_path)
                    stale_m3u = os.path.join(source_dir, stale_m3u_name)
                    if (os.path.abspath(stale_m3u) != os.path.abspath(m3u_path)
                            and os.path.isfile(stale_m3u)):
                        os.remove(stale_m3u)
            with open(m3u_path, "w", encoding="utf-8", newline="\n") as f:
                for _, final_path, _ in discs:
                    f.write(relative_dir + "/" + os.path.basename(final_path) + "\n")
            grouped += 1
        except OSError as e:
            errors += 1
            print("  ERROR grouping {0}: {1}".format(release_name, e), file=sys.stderr)

    print("\nGrouped {0}/{1} multi-disc release(s) ({2} already grouped, "
          "skipped{3}).".format(
              grouped, len(to_group), len(already_done),
              "; {0} error(s)".format(errors) if errors else ""))

    removed_dirs = remove_now_empty_dirs(roms_dir, source_dirs)
    if removed_dirs:
        print("\nRemoved {0} now-empty source folder(s):".format(len(removed_dirs)))
        for d in sorted(removed_dirs):
            print("  {0}".format(os.path.relpath(d, roms_dir)))

    return grouped, len(already_done)


# ---- fixing a trailing space before the extension -----------------------

# Extensions this fixes a trailing space in front of, e.g. "Game .chd" ->
# "Game.chd" -- a naming artifact some ripping/download tools leave
# behind. Scoped to these two for now; a plain space right before .cue or
# .bin would need the same fix applied to whichever file references it
# (a .cue's FILE line, or another .cue naming that .bin) to avoid
# breaking that pairing, which is a related but separate problem.
FILENAME_SPACE_FIX_EXTENSIONS = {CHD_EXTENSION, ISO_EXTENSION}


def fix_trailing_space_in_filename(filename):
    """If filename has one of FILENAME_SPACE_FIX_EXTENSIONS and its stem
    ends in whitespace right before the extension (e.g. "Game .iso"),
    return the corrected name with that whitespace stripped (e.g.
    "Game.iso"). Returns None if the extension isn't one this fixes, or
    there's no trailing whitespace to strip.
    """
    stem, ext = os.path.splitext(filename)
    if ext.lower() not in FILENAME_SPACE_FIX_EXTENSIONS:
        return None
    fixed_stem = stem.rstrip()
    if fixed_stem == stem:
        return None
    return fixed_stem + ext


def find_badly_spaced_files(roms_dir, dup_dir):
    """Recursively find every file under roms_dir with a trailing space
    before its extension (see fix_trailing_space_in_filename). Never
    descends into dup_dir (see walk_excluding_dup_dir).

    Returns [(path, fixed_name), ...].
    """
    found = []
    for root, dirs, files in walk_excluding_dup_dir(roms_dir, dup_dir):
        for fname in files:
            fixed = fix_trailing_space_in_filename(fname)
            if fixed is not None:
                found.append((os.path.join(root, fname), fixed))
    return sorted(found)


def rewrite_cue_file_reference(cue_path, old_name, new_name):
    """Rewrite this .cue's FILE line to reference new_name instead of
    old_name (preserving quoting style), leaving the rest of the cue
    sheet untouched. Returns True if a matching reference was found and
    rewritten.
    """
    with open(cue_path, "r", encoding="utf-8", errors="replace") as f:
        text = f.read()

    matched = []

    def _sub(m):
        ref = m.group(1) if m.group(1) is not None else m.group(2)
        if ref != old_name:
            return m.group(0)
        matched.append(True)
        if m.group(1) is not None:
            return m.group(0)[:m.start(1) - m.start(0)] + new_name + m.group(0)[m.end(1) - m.start(0):]
        return m.group(0)[:m.start(2) - m.start(0)] + new_name

    new_text = CUE_FILE_LINE_RE.sub(_sub, text)
    if not matched:
        return False
    with open(cue_path, "w", encoding="utf-8") as f:
        f.write(new_text)
    return True


def rewrite_m3u_disc_reference(m3u_path, old_relative, new_relative):
    """Rewrite this .m3u playlist's content line referencing old_relative
    (a path relative to the playlist's own directory, forward-slashed --
    see make_m3u_playlists) to reference new_relative instead. Returns
    True if a matching line was found and rewritten.
    """
    with open(m3u_path, "r", encoding="utf-8") as f:
        lines = [line.rstrip("\n") for line in f]

    changed = False
    new_lines = []
    for line in lines:
        if line.strip() == old_relative:
            new_lines.append(new_relative)
            changed = True
        else:
            new_lines.append(line)

    if not changed:
        return False
    with open(m3u_path, "w", encoding="utf-8", newline="\n") as f:
        for line in new_lines:
            f.write(line + "\n")
    return True


def plan_filename_spacing_fix(roms_dir, dup_dir):
    """Work out the rename for each badly-spaced file (see
    find_badly_spaced_files), plus which .cue or .m3u file (if any)
    references its current name and will need updating to keep pointing
    at it -- a .cue can name a badly-spaced .iso as its data track, and
    an .m3u can reference a badly-spaced .chd/.iso if it was grouped by
    --make-m3u before being renamed. Without this, the rename would
    silently break that pairing/playlist.

    Returns [(old_path, new_path, ref_updates), ...] where ref_updates is
    [(ref_path, kind), ...], kind being "cue" or "m3u".
    """
    to_rename = []
    reserved = set()

    for old_path, fixed_name in find_badly_spaced_files(roms_dir, dup_dir):
        old_dir = os.path.dirname(old_path)
        old_name = os.path.basename(old_path)
        new_path = unique_dest_path(old_dir, fixed_name, also_avoid=reserved)
        reserved.add(new_path)

        ref_updates = []

        for fname in sorted(os.listdir(old_dir)):
            if fname.lower().endswith(CUE_EXTENSION):
                cue_path = os.path.join(old_dir, fname)
                if old_name in parse_cue_file_references(cue_path):
                    ref_updates.append((cue_path, "cue"))

        old_relative = os.path.relpath(old_path, roms_dir).replace(os.sep, "/")
        for fname in sorted(os.listdir(roms_dir)):
            if fname.lower().endswith(M3U_EXTENSION):
                m3u_path = os.path.join(roms_dir, fname)
                with open(m3u_path, "r", encoding="utf-8", errors="replace") as f:
                    if any(line.strip() == old_relative for line in f):
                        ref_updates.append((m3u_path, "m3u"))

        to_rename.append((old_path, new_path, ref_updates))

    return to_rename


def fix_filename_spacing(roms_dir, dup_dir, apply):
    """Print (and, if apply, perform) the plan from
    plan_filename_spacing_fix: strip a trailing space some ripping/
    download tools leave right before a .chd/.iso extension. Updates any
    .cue or .m3u reference to the old name so nothing is left pointing at
    a file that no longer exists. Returns (fixed, errors).
    """
    to_rename = plan_filename_spacing_fix(roms_dir, dup_dir)
    if not to_rename:
        print("No filenames with a trailing space before {0} found under "
              "{1}.".format(
                  "/".join(sorted(FILENAME_SPACE_FIX_EXTENSIONS)), roms_dir))
        return 0, 0

    for old_path, new_path, ref_updates in to_rename:
        print("[FIX] {0}  ->  {1}".format(
            os.path.relpath(old_path, roms_dir), os.path.basename(new_path)))
        for ref_path, kind in ref_updates:
            print("  [{0}] also updates reference in {1}".format(
                kind.upper(), os.path.relpath(ref_path, roms_dir)))

    if not apply:
        print("\nDRY RUN -- would fix {0} filename(s). Re-run with --apply "
              "to do it.".format(len(to_rename)))
        return len(to_rename), 0

    fixed = 0
    errors = 0
    for old_path, new_path, ref_updates in to_rename:
        old_name = os.path.basename(old_path)
        new_name = os.path.basename(new_path)
        old_relative = os.path.relpath(old_path, roms_dir).replace(os.sep, "/")

        try:
            os.rename(old_path, new_path)
        except OSError as e:
            errors += 1
            print("  ERROR renaming {0}: {1}".format(
                os.path.relpath(old_path, roms_dir), e), file=sys.stderr)
            continue

        for ref_path, kind in ref_updates:
            try:
                if kind == "cue":
                    rewrite_cue_file_reference(ref_path, old_name, new_name)
                else:
                    dir_part = os.path.dirname(old_relative)
                    new_relative = (dir_part + "/" + new_name) if dir_part else new_name
                    rewrite_m3u_disc_reference(ref_path, old_relative, new_relative)
            except OSError as e:
                print("  Warning: could not update reference in {0}: {1}".format(
                    os.path.relpath(ref_path, roms_dir), e), file=sys.stderr)

        fixed += 1

    print("\nFixed {0}/{1} filename(s){2}.".format(
        fixed, len(to_rename), "; {0} error(s)".format(errors) if errors else ""))
    return fixed, errors


# ---- isolating titles never officially released in North America -------

# Regions that count as "available in North America" for --isolate-imports:
# an actual USA release, or a World release (sold in NA too, even without
# a USA-specific tag).
NA_REGIONS = {"usa", "world"}

# Dot-prefixed so ES-DE and RetroArch skip it when scanning, the same way
# ".duplicates" and "--make-m3u"'s per-format disc folders are hidden -- isolated
# imports stay on disk and stay browsable, without showing up as a folder
# entry in the frontend alongside the games that did get a NA release.
IMPORTS_DIR_NAME = ".imports"

# The visible name this folder used before it was hidden. A folder left over
# under the old name is migrated into the current one (see
# plan_imports_dir_migration) rather than being mistaken for a game title.
LEGACY_IMPORTS_DIR_NAME = "Imports"

# Common non-ROM asset folders some frontends/collections keep directly
# alongside the ROM files (e.g. ES-DE-style per-system media). These have
# no title/tags of their own -- without this exclusion they'd be treated
# as an untitled release with no recognized region and get swept into
# .imports/, which would be wrong (and potentially disruptive, since some
# frontends expect this folder at a fixed path relative to the roms).
NON_TITLE_DIR_NAMES = {
    "media", "images", "image", "screenshots", "screenshot",
    "videos", "video", "manuals", "manual", "downloaded_media",
}


def tags_indicate_na_release(tags):
    """True if any of these tags identifies the release as available in
    North America -- i.e. carries "usa" or "world" as a recognized region
    component, including combined tags like "USA, Europe".
    """
    for tag in tags:
        parts = [p.strip().lower() for p in re.split(r"[,/]", tag)]
        if any(p in NA_REGIONS for p in parts):
            return True
    return False


def _is_non_game_tagged(tags):
    """True for [BIOS], proto/beta, or (Program) tagged entries -- the same
    categories the normal scan pulls out unconditionally in scan_rom_files().
    A (Program) utility/test disc (e.g. "Sega Channel", "CDX Pro") isn't a
    foreign release of a real game, so it shouldn't be swept into .imports/
    just for lacking a USA/World tag.
    """
    if any(t.strip().lower() == "bios" for t in tags):
        return True
    if any(is_proto_beta_tag(t) for t in tags):
        return True
    if any(is_program_tag(t) for t in tags):
        return True
    return False


def plan_imports_dir_migration(roms_dir, reserved=None):
    """Plan moving everything out of a leftover visible "Imports/" folder
    (the name this used before it was hidden) into the current ".imports/".

    Each entry is moved as one unit, so a release subfolder or an .m3u
    playlist's hidden disc folder keeps its structure. Returns
    (moves, legacy_dir) with legacy_dir set to the old folder's path so it
    can be removed once emptied, or (empty list, None) when there's nothing
    to migrate -- including the case where the old name and the new one are
    the same folder on a case-insensitive filesystem.
    """
    legacy_dir = os.path.join(roms_dir, LEGACY_IMPORTS_DIR_NAME)
    import_dir = os.path.join(roms_dir, IMPORTS_DIR_NAME)
    if not os.path.isdir(legacy_dir):
        return [], None
    if os.path.normcase(os.path.abspath(legacy_dir)) == os.path.normcase(
            os.path.abspath(import_dir)):
        return [], None

    reserved = reserved if reserved is not None else set()
    moves = []
    for name in sorted(os.listdir(legacy_dir)):
        src = os.path.join(legacy_dir, name)
        if name in M3U_HIDDEN_DIR_NAMES:
            # A hidden disc folder has to land at the same relative
            # position or every migrated playlist's disc paths break. Move
            # its per-release subfolders individually rather than the folder
            # itself: shutil.move() onto an existing directory nests inside
            # it instead of merging, which would bury them a level deeper.
            for release_name in sorted(os.listdir(src)):
                moves.append((
                    os.path.join(src, release_name),
                    os.path.join(import_dir, name, release_name),
                ))
            continue
        dest = unique_dest_path(import_dir, name, also_avoid=reserved)
        reserved.add(dest)
        moves.append((src, dest))
    return moves, legacy_dir


def plan_isolate_imports(roms_dir, dup_dir, blacklist_titles=None, whitelist_titles=None,
                          whitelist_releases=None, reject_titles=None):
    """Find every title directly under roms_dir with NO North-American-
    tagged release (no "USA" or "World" tag on ANY of its entries) and
    plan moving every one of that title's entries into
    <roms_dir>/.imports/, keeping every region/revision of the title
    together as a group.

    Only considers roms_dir's own direct children: a plain ROM file, an
    .m3u playlist (see make_m3u_playlists -- its corresponding hidden
    per-format disc folder, e.g. ".chd/<release>/" or ".rvz/<release>/",
    is moved alongside it, keeping the playlist's relative disc paths
    valid), or a whole subfolder (treated as one release unit, e.g. a
    multi-disc release not yet grouped via --make-m3u). BIOS-, proto/
    beta-, and (Program)-tagged entries are left alone, same as the
    normal scan. .duplicates/, .imports/, the M3U hidden per-format
    folders, alpha-bucket leftover folders, and common non-ROM asset
    folders some frontends keep alongside the roms (see
    NON_TITLE_DIR_NAMES, e.g. "media", "images", "screenshots") are never
    themselves treated as titles. Entries already inside .imports/ are
    invisible to this pass (never reconsidered), making re-runs cheap.

    blacklist_titles/whitelist_titles: the whole-title dicts from
    load_filter_file() (rom_filters.txt), keyed by normalized title --
    same file/format the normal duplicate scan reads. A blacklisted title
    is never moved (matches its "never touch this game at all" meaning
    everywhere else in the tool, and always wins over a whitelist, same
    as the normal scan); when a whitelist is present, only whitelisted
    titles are even considered, everything else is left alone.

    whitelist_releases: release-specific whitelist entries (a line WITH
    tags, e.g. "Streets of Rage II (Japan, Europe) (En,Ja)") pin that
    exact release as the forced keeper in the normal scan -- here, a
    title with any release matching one is treated as kept in place too
    (and this bypasses the whole-title whitelist restriction above, since
    pinning a specific release is itself a clear signal to leave that
    title alone). Release-specific BLACKLIST entries are intentionally
    NOT applied here: their meaning ("force this release to always lose
    the duplicate comparison") doesn't translate to "protect it from
    being moved" -- the opposite of what isolate-imports would need.

    reject_titles: whole-title [reject] entries. A rejected title is left
    in place here too (same as blacklist_titles) rather than moved to
    .imports/ -- it's headed for .duplicates/Rejected/ via the normal
    scan instead, which also walks .imports/ and would just have to
    re-discover and re-route it there anyway. Release-specific [reject]
    entries aren't applied here for the same reason release-specific
    blacklist entries aren't: nothing here corresponds to "losing a
    duplicate comparison".

    Returns (to_move, kept_titles, import_titles, blocked, filtered_out):
        to_move:       [(current_path, final_path), ...]
        kept_titles:   sorted display titles staying in roms_dir
        import_titles: sorted display titles moving to .imports/
        blocked:       [(title, reason), ...] for an .m3u release whose
                        target already exists in .imports/ -- skipped
                        rather than risking a renamed, desynced pair
        filtered_out:  count of entries skipped due to the filter file
    """
    blacklist_titles = blacklist_titles or {}
    whitelist_titles = whitelist_titles or {}
    whitelist_releases = whitelist_releases or []
    reject_titles = reject_titles or {}
    dup_dir_abs = os.path.abspath(dup_dir)
    import_dir = os.path.join(roms_dir, IMPORTS_DIR_NAME)
    titles = defaultdict(list)  # title_key -> [(display_title, tags, path, is_m3u), ...]

    for name in sorted(os.listdir(roms_dir)):
        full = os.path.join(roms_dir, name)
        if os.path.abspath(full) == dup_dir_abs:
            continue
        if name.lower() in (IMPORTS_DIR_NAME.lower(), LEGACY_IMPORTS_DIR_NAME.lower()):
            continue  # the legacy one is migrated, not treated as a title
        if name in M3U_HIDDEN_DIR_NAMES:
            continue  # handled together with its .m3u file, not on its own

        if os.path.isdir(full):
            if is_alpha_bucket_dirname(name) or name.strip().lower() in NON_TITLE_DIR_NAMES:
                continue
            stem, is_m3u = name, False
        else:
            stem, ext = os.path.splitext(name)
            ext = ext.lower()
            if ext == M3U_EXTENSION:
                is_m3u = True
            elif ext in ROM_EXTENSIONS_DEFAULT:
                is_m3u = False
            else:
                continue

        title, tags = extract_tags(stem)
        if _is_non_game_tagged(tags):
            continue

        title_key = normalize_title(title) or normalize_title(stem)
        titles[title_key].append((title, tags, full, is_m3u))

    to_move = []
    kept_titles = []
    import_titles = []
    blocked = []
    filtered_out = 0
    reserved = set()

    for title_key, entries in sorted(titles.items()):
        display_title = entries[0][0]

        if title_key in blacklist_titles or title_key in reject_titles:
            filtered_out += len(entries)
            continue

        release_pinned = False
        for _, tags, _, _ in entries:
            release_key = tuple(sorted(t.lower() for t in tags if not is_part_tag(t)))
            if find_release_filter_matches(title_key, release_key, whitelist_releases):
                release_pinned = True
                break

        if not release_pinned and whitelist_titles and title_key not in whitelist_titles:
            filtered_out += len(entries)
            continue

        if release_pinned or any(tags_indicate_na_release(tags) for _, tags, _, _ in entries):
            kept_titles.append(display_title)
            continue

        import_titles.append(display_title)
        for _, _, path, is_m3u in entries:
            name = os.path.basename(path)

            if is_m3u:
                stem = os.path.splitext(name)[0]
                final_path = os.path.join(import_dir, name)
                if os.path.exists(final_path):
                    blocked.append((display_title, "{0} already exists in {1}/".format(
                        name, IMPORTS_DIR_NAME)))
                    continue
                to_move.append((path, final_path))

                # A release only ever exists in one disc format, so at most
                # one of these hidden folders actually holds this release's
                # discs -- check each since the .m3u's own name doesn't say
                # which format it is.
                for hidden_name in M3U_HIDDEN_DIR_NAMES:
                    hidden_src = os.path.join(roms_dir, hidden_name, stem)
                    if os.path.isdir(hidden_src):
                        hidden_dest = os.path.join(import_dir, hidden_name, stem)
                        to_move.append((hidden_src, hidden_dest))
                        break
            else:
                final_path = unique_dest_path(import_dir, name, also_avoid=reserved)
                reserved.add(final_path)
                to_move.append((path, final_path))

    return to_move, sorted(kept_titles), sorted(import_titles), blocked, filtered_out


def isolate_imports(roms_dir, dup_dir, apply, blacklist_titles=None,
                     whitelist_titles=None, whitelist_releases=None,
                     reject_titles=None, filter_file_used=None):
    """Print (and, if apply, perform) the plan from plan_isolate_imports:
    move every title with no North-American release into
    <roms_dir>/.imports/. Returns (moved_titles, kept_titles).

    A leftover visible "Imports/" folder from before this one was hidden is
    migrated into ".imports/" first (see plan_imports_dir_migration).

    blacklist_titles/whitelist_titles/whitelist_releases/reject_titles:
    passed straight through to plan_isolate_imports -- see there for how
    rom_filters.txt applies.
    """
    to_move, kept_titles, import_titles, blocked, filtered_out = plan_isolate_imports(
        roms_dir, dup_dir, blacklist_titles=blacklist_titles,
        whitelist_titles=whitelist_titles, whitelist_releases=whitelist_releases,
        reject_titles=reject_titles)

    migration_moves, legacy_dir = plan_imports_dir_migration(roms_dir)

    if filter_file_used:
        print("Filter file used: {0}".format(filter_file_used))
        if filtered_out:
            print("Entries skipped by filter: {0}".format(filtered_out))

    if migration_moves:
        print("\nMigrating {0} entr{1} from the old visible {2}/ folder into "
              "{3}/ (hidden, so ES-DE and RetroArch skip it):".format(
                  len(migration_moves), "y" if len(migration_moves) == 1 else "ies",
                  LEGACY_IMPORTS_DIR_NAME, IMPORTS_DIR_NAME))
        for src, _dest in migration_moves[:10]:
            print("  [MIGRATE] {0}".format(os.path.relpath(src, roms_dir)))
        if len(migration_moves) > 10:
            print("  ... and {0} more".format(len(migration_moves) - 10))

    if not import_titles and not blocked and not migration_moves:
        print("No import-only titles found under {0} -- nothing to isolate.".format(roms_dir))
        return 0, len(kept_titles)

    if import_titles:
        print("\nTitles with no North American release ({0}) -- moving to {1}/:".format(
            len(import_titles), IMPORTS_DIR_NAME))
        for title in import_titles:
            print("  {0}".format(title))

    for title, reason in blocked:
        print("\n  Warning: {0!r} -- skipping ({1}).".format(title, reason), file=sys.stderr)

    if not apply:
        print("\nDRY RUN -- would move {0} file(s)/folder(s) for {1} title(s) into "
              "{2}/ ({3} title(s) kept in {4}{5}{6}). Re-run with --apply to do it.".format(
                  len(to_move), len(import_titles), IMPORTS_DIR_NAME, len(kept_titles),
                  os.path.basename(roms_dir) or roms_dir,
                  "; {0} blocked".format(len(blocked)) if blocked else "",
                  "; {0} migrated from {1}/".format(
                      len(migration_moves), LEGACY_IMPORTS_DIR_NAME) if migration_moves else ""))
        return len(import_titles), len(kept_titles)

    import_dir = os.path.join(roms_dir, IMPORTS_DIR_NAME)
    os.makedirs(import_dir, exist_ok=True)
    moved = 0
    errors = 0
    # Migration first: the old folder has to be emptied before anything new
    # lands, so a same-named title can't collide with a migrated entry.
    for src, dest in migration_moves + to_move:
        try:
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            shutil.move(src, dest)
            moved += 1
        except OSError as e:
            errors += 1
            print("  ERROR moving {0} -> {1}: {2}".format(src, dest, e), file=sys.stderr)

    print("\nMoved {0}/{1} file(s)/folder(s) for {2} title(s) into {3}/{4}.".format(
        moved, len(migration_moves) + len(to_move), len(import_titles), IMPORTS_DIR_NAME,
        "; {0} error(s)".format(errors) if errors else ""))

    cleanup_dirs = {os.path.join(roms_dir, name) for name in M3U_HIDDEN_DIR_NAMES}
    if legacy_dir:
        cleanup_dirs |= {os.path.join(legacy_dir, name) for name in M3U_HIDDEN_DIR_NAMES}
        cleanup_dirs.add(legacy_dir)
    removed_dirs = remove_now_empty_dirs(roms_dir, cleanup_dirs)
    if removed_dirs:
        print("\nRemoved {0} now-empty folder(s):".format(len(removed_dirs)))
        for d in sorted(removed_dirs):
            print("  {0}".format(os.path.relpath(d, roms_dir)))

    return len(import_titles), len(kept_titles)


def find_release_filter_matches(title_key, release_key, release_entries):
    """Return the list of (title_key, tag_set, line) entries from
    release_entries whose tag_set is a subset of this release's own tags
    -- i.e. the entry gave enough tags to identify this release, even if
    it didn't spell out every single tag.
    """
    release_tags = set(release_key)
    return [
        entry for entry in release_entries
        if entry[0] == title_key and entry[1].issubset(release_tags)
    ]


# ---- the normal duplicate scan ------------------------------------------

# Subfolders of dup_dir that files get routed into by category. Titles that
# lost the duplicate comparison go straight into dup_dir itself.
BIOS_SUBDIR = "bios"
PROTO_BETA_SUBDIR = "Proto-Beta"
PROGRAM_SUBDIR = "Program"
REJECT_SUBDIR = "Rejected"
REDUNDANT_DISC_SUBDIR = "Redundant-Raw-Disc"


# What the scan decided about one title.
#   releases:        {release_key: [(path, tags), ...]} -- every release of
#                    this title found on disk
#   keeper_key:      the release_key staying in roms_dir
#   dupe_keys:       release_keys routed to .duplicates/
#   pinned:          True if keeper_key was pinned by a [whitelist] entry
#                    rather than chosen by scoring
#   forced_dup_keys: {release_key: [filter_line, ...]} for releases forced
#                    to lose by a [blacklist] entry
#   contested:       False for the common case of a title with exactly one
#                    release and no filter involvement -- nothing was
#                    decided, so it's only worth printing in verbose mode
TitleDecision = namedtuple("TitleDecision", [
    "title_key", "releases", "keeper_key", "dupe_keys", "pinned",
    "forced_dup_keys", "contested",
])


# Everything the normal scan worked out, before anything is printed or moved.
# Each *_moves list is [(src, dest), ...]; destinations are deduplicated
# against every other planned move, so two identically-named files from
# different folders can never be planned onto the same destination path.
ScanPlan = namedtuple("ScanPlan", [
    "decisions", "dup_moves", "bios_moves", "proto_beta_moves",
    "program_moves", "reject_moves", "redundant_moves", "skipped",
    "filtered_out", "blacklist_hits", "whitelist_hits", "reject_hits",
    "pin_hits", "force_dup_hits", "warnings", "total_titles",
    "total_releases", "kept_files", "dup_files",
])


def all_planned_moves(plan):
    """Every (src, dest) pair in a ScanPlan, in the order they're reported."""
    return (list(plan.dup_moves) + list(plan.bios_moves)
            + list(plan.proto_beta_moves) + list(plan.program_moves)
            + list(plan.reject_moves) + list(plan.redundant_moves))


def _plan_move(src, dest_dir, reserved):
    """Plan moving src into dest_dir, claiming the destination so no later
    move in this same run can be planned onto it too.

    The reservation matters because nothing has been moved yet at planning
    time: unique_dest_path()'s on-disk existence check alone would hand the
    same free destination to two different sources with the same basename
    (e.g. the same release present in two subfolders), and the second
    shutil.move() would silently overwrite the first.
    """
    dest = unique_dest_path(dest_dir, os.path.basename(src), also_avoid=reserved)
    reserved.add(dest)
    return (src, dest)


def scan_rom_files(roms_dir, dup_dir, extensions, blacklist_titles, whitelist_titles,
                    reject_titles=None, reject_releases=None):
    """Walk roms_dir and group every recognized ROM file by title, then by
    release within that title.

    A release_key groups together files that are pieces of the SAME release
    (e.g. a .cue plus all its .bin tracks, or a multi-disc set) by ignoring
    track/disc-number tags. Different release_keys under the same title are
    what get treated as real duplicates (different region/rev/etc).

    Files tagged [BIOS], proto/beta, or (Program) are pulled out here rather
    than being compared as duplicates -- they're routed to their own
    dup_dir subfolder regardless of what else exists for that title. This
    matters most for (Program) test/utility discs (e.g. "Sega Channel",
    "CDX Pro"): each is typically the only file under its own made-up
    title, so without pulling it out unconditionally here, the "never lose
    the only copy of a title" guard in decide_title_keeper() would keep it
    in place forever no matter how it's tagged.

    A [reject] entry (rom_filters.txt) is the same idea but for a release
    with no recognizable tag of its own -- e.g. a one-off compilation like
    "Arcade Legends Sega Mega Drive (World)" that isn't [BIOS]/proto-beta/
    (Program) tagged, so nothing else would ever pull it out. Checked
    first, ahead of blacklist/whitelist scoping and every other category:
    it's the most explicit signal available ("I know what this is, get it
    out"), so it always wins.

    Returns (titles, bios_files, proto_beta_files, program_files,
    reject_files, reject_hits, skipped, filtered_out, blacklist_hits,
    whitelist_hits), where titles is
    {title_key: {release_key: [(path, tags), ...]}}.
    """
    reject_titles = reject_titles or {}
    reject_releases = reject_releases or []
    titles = defaultdict(lambda: defaultdict(list))
    bios_files = []
    proto_beta_files = []
    program_files = []
    reject_files = []
    skipped = []
    filtered_out = 0
    blacklist_hits = defaultdict(int)   # title_key -> files skipped (title-level)
    whitelist_hits = defaultdict(int)   # title_key -> files processed (title-level)
    reject_hits = defaultdict(int)      # filter line -> files rejected

    for root, dirs, files in walk_excluding_dup_dir(roms_dir, dup_dir):
        for fname in files:
            fpath = os.path.join(root, fname)
            stem, ext = os.path.splitext(fname)
            if ext.lower() not in extensions:
                skipped.append(fpath)
                continue
            base_title, tags = extract_tags(stem)

            title_key = normalize_title(base_title)
            if not title_key:
                title_key = normalize_title(stem)

            non_part_tags = tuple(sorted(
                t.lower() for t in tags if not is_part_tag(t)
            ))

            if title_key in reject_titles:
                reject_files.append(fpath)
                reject_hits[reject_titles[title_key]] += 1
                continue
            release_reject_matches = find_release_filter_matches(
                title_key, non_part_tags, reject_releases)
            if release_reject_matches:
                reject_files.append(fpath)
                for m in release_reject_matches:
                    reject_hits[m[2]] += 1
                continue

            if title_key in blacklist_titles:
                filtered_out += 1
                blacklist_hits[title_key] += 1
                continue
            if whitelist_titles and title_key not in whitelist_titles:
                filtered_out += 1
                continue
            if whitelist_titles and title_key in whitelist_titles:
                whitelist_hits[title_key] += 1

            if any(is_proto_beta_tag(t) for t in tags):
                proto_beta_files.append(fpath)
                continue

            if any(t.strip().lower() == "bios" for t in tags):
                bios_files.append(fpath)
                continue

            if any(is_program_tag(t) for t in tags):
                program_files.append(fpath)
                continue

            titles[title_key][non_part_tags].append((fpath, tags))

    return (titles, bios_files, proto_beta_files, program_files,
            reject_files, reject_hits, skipped, filtered_out,
            blacklist_hits, whitelist_hits)


def split_redundant_raw_disc(titles):
    """If a single release contains BOTH a .chd and raw disc-image files
    (.bin/.cue/.iso/etc) for the same disc, the raw files are leftovers from
    a conversion. Drop them from the release (so it's compared on its .chd
    alone) and return them for routing to Redundant-Raw-Disc/.

    Mutates titles in place. Returns the list of redundant file paths.
    """
    redundant_disc_files = []
    for title_key, releases in titles.items():
        for release_key, file_list in list(releases.items()):
            exts = {os.path.splitext(f)[1].lower() for f, _ in file_list}
            if ".chd" in exts and (exts & RAW_DISC_EXTENSIONS):
                keep_list = [(f, t) for f, t in file_list
                             if os.path.splitext(f)[1].lower() == ".chd"]
                raw_list = [(f, t) for f, t in file_list
                            if os.path.splitext(f)[1].lower() != ".chd"]
                releases[release_key] = keep_list
                redundant_disc_files.extend(f for f, t in raw_list)
    return redundant_disc_files


def decide_title_keeper(title_key, releases, region_priority,
                         whitelist_releases=None, blacklist_releases=None,
                         warnings=None):
    """Work out which release of one title stays in place and which ones are
    duplicates. Returns a TitleDecision.

    A release-specific [whitelist] entry pins that exact release as the
    keeper, overriding scoring entirely. A release-specific [blacklist]
    entry forces that release to lose even if it would otherwise win --
    unless every release of the title is blacklisted, in which case scoring
    runs normally anyway so the game isn't lost outright (flagged via
    warnings).

    Otherwise the keeper is the lowest-scoring release per score_release().

    warnings: optional list, appended to rather than printed, so this stays
    usable as a pure function.
    """
    whitelist_releases = whitelist_releases or []
    blacklist_releases = blacklist_releases or []
    if warnings is None:
        warnings = []

    # Check for a manual override: a release-specific whitelist entry pins
    # that exact release as the forced keeper, overriding scoring.
    pinned_key = None
    pinned_lines = []
    for release_key in releases:
        matches = find_release_filter_matches(title_key, release_key, whitelist_releases)
        if matches:
            pinned_lines.extend(m[2] for m in matches)
            if pinned_key is not None and pinned_key != release_key:
                warnings.append(
                    "multiple [whitelist] entries pin different releases of "
                    "{0!r} -- using the first one found.".format(title_key))
                continue
            pinned_key = release_key

    # Release-specific blacklist entries force that release to always be
    # treated as a duplicate, even if it would otherwise win.
    forced_dup_keys = {}  # release_key -> list of matching filter lines
    for release_key in releases:
        matches = find_release_filter_matches(title_key, release_key, blacklist_releases)
        if matches:
            forced_dup_keys[release_key] = [m[2] for m in matches]

    contested = not (len(releases) == 1 and pinned_key is None and not forced_dup_keys)

    if pinned_key is not None:
        keeper_key = pinned_key
    else:
        candidate_keys = [rk for rk in releases if rk not in forced_dup_keys]
        if not candidate_keys:
            # Every release under this title is blacklisted -- rather than
            # lose the game entirely, fall back to normal scoring so at
            # least one copy survives, but flag it for the user.
            warnings.append(
                "every release of {0!r} is blacklisted -- keeping the "
                "best-scoring one anyway to avoid losing the game "
                "entirely.".format(title_key))
            candidate_keys = list(releases.keys())

        scored = []
        for release_key in candidate_keys:
            file_list = releases[release_key]
            total_size = sum(os.path.getsize(f) for f, _ in file_list)
            fmt_rank = disc_format_rank(file_list)
            s = score_release(list(release_key), total_size, region_priority, fmt_rank)
            scored.append((s, release_key))
        scored.sort(key=lambda x: x[0])
        keeper_key = scored[0][1]

    return TitleDecision(
        title_key=title_key,
        releases=dict(releases),
        keeper_key=keeper_key,
        dupe_keys=[rk for rk in releases if rk != keeper_key],
        pinned=pinned_key is not None,
        forced_dup_keys=forced_dup_keys,
        contested=contested,
    ), pinned_lines


def plan_duplicate_scan(roms_dir, dup_dir, region_priority=None, extensions=None,
                         whitelist_titles=None, whitelist_releases=None,
                         blacklist_titles=None, blacklist_releases=None,
                         reject_titles=None, reject_releases=None):
    """Work out the whole normal duplicate scan without printing or moving
    anything: which release of each title to keep, which to route to
    .duplicates/, and where every moved file lands.

    This is the planning half of the tool's main operation, split out from
    main() so the outcome can be inspected (and tested) directly instead of
    only through the printed output. See print_scan_plan() for the reporting
    half. Returns a ScanPlan.
    """
    region_priority = region_priority or DEFAULT_REGION_PRIORITY
    extensions = extensions if extensions is not None else ROM_EXTENSIONS_DEFAULT
    whitelist_titles = whitelist_titles or {}
    whitelist_releases = whitelist_releases or []
    blacklist_titles = blacklist_titles or {}
    blacklist_releases = blacklist_releases or []
    reject_titles = reject_titles or {}
    reject_releases = reject_releases or []

    (titles, bios_files, proto_beta_files, program_files, reject_files,
     reject_hits, skipped, filtered_out, blacklist_hits, whitelist_hits) = scan_rom_files(
        roms_dir, dup_dir, extensions, blacklist_titles, whitelist_titles,
        reject_titles=reject_titles, reject_releases=reject_releases)

    redundant_disc_files = split_redundant_raw_disc(titles)

    decisions = []
    dup_moves = []
    warnings = []
    pin_hits = defaultdict(int)         # filter line -> files pinned as keeper
    force_dup_hits = defaultdict(int)   # filter line -> files forced to dup
    kept_files = 0
    dup_files = 0
    total_releases = 0
    # One shared reservation set across every category: destinations are
    # claimed as they're planned so nothing can collide with anything else.
    reserved = set()

    for title_key, releases in sorted(titles.items()):
        total_releases += len(releases)
        decision, pinned_lines = decide_title_keeper(
            title_key, releases, region_priority,
            whitelist_releases=whitelist_releases,
            blacklist_releases=blacklist_releases,
            warnings=warnings)
        decisions.append(decision)

        keeper_files = decision.releases[decision.keeper_key]
        kept_files += len(keeper_files)

        if decision.pinned:
            for line in pinned_lines:
                pin_hits[line] += len(keeper_files)

        for release_key in decision.dupe_keys:
            file_list = decision.releases[release_key]
            for line in decision.forced_dup_keys.get(release_key, []):
                force_dup_hits[line] += len(file_list)
            for fpath, tags in sorted(file_list):
                dup_files += 1
                dup_moves.append(_plan_move(fpath, dup_dir, reserved))

    bios_moves = [_plan_move(f, os.path.join(dup_dir, BIOS_SUBDIR), reserved)
                  for f in sorted(bios_files)]
    proto_beta_moves = [_plan_move(f, os.path.join(dup_dir, PROTO_BETA_SUBDIR), reserved)
                        for f in sorted(proto_beta_files)]
    program_moves = [_plan_move(f, os.path.join(dup_dir, PROGRAM_SUBDIR), reserved)
                     for f in sorted(program_files)]
    reject_moves = [_plan_move(f, os.path.join(dup_dir, REJECT_SUBDIR), reserved)
                    for f in sorted(reject_files)]
    redundant_moves = [_plan_move(f, os.path.join(dup_dir, REDUNDANT_DISC_SUBDIR), reserved)
                       for f in sorted(redundant_disc_files)]

    return ScanPlan(
        decisions=decisions,
        dup_moves=dup_moves,
        bios_moves=bios_moves,
        proto_beta_moves=proto_beta_moves,
        program_moves=program_moves,
        reject_moves=reject_moves,
        redundant_moves=redundant_moves,
        skipped=skipped,
        filtered_out=filtered_out,
        blacklist_hits=blacklist_hits,
        whitelist_hits=whitelist_hits,
        reject_hits=reject_hits,
        pin_hits=pin_hits,
        force_dup_hits=force_dup_hits,
        warnings=warnings,
        total_titles=len(titles),
        total_releases=total_releases,
        kept_files=kept_files,
        dup_files=dup_files,
    )


def print_scan_plan(plan, roms_dir, verbose=False):
    """Print the per-title KEEP/DUP breakdown of a ScanPlan, followed by the
    BIOS, proto/beta, Program, rejected, and redundant-raw-disc sections.
    Reporting only -- see plan_duplicate_scan() for the half that decides
    all of this.
    """
    for decision in plan.decisions:
        if not decision.contested:
            if verbose:
                for fpath, tags in decision.releases[decision.keeper_key]:
                    print("[KEEP] {0}  tags={1}".format(
                        os.path.relpath(fpath, roms_dir), tags))
            continue

        print("\nGame: {0!r}  ({1} release(s) found)".format(
            decision.title_key, len(decision.releases)))
        print("  [KEEP]{0} release tags={1}".format(
            "  [PINNED via whitelist]" if decision.pinned else "",
            list(decision.keeper_key) or ["<none>"]))
        for fpath, tags in sorted(decision.releases[decision.keeper_key]):
            print("         {0}".format(os.path.relpath(fpath, roms_dir)))

        for release_key in decision.dupe_keys:
            print("  [DUP]{0} release tags={1}".format(
                "  [FORCED via blacklist]" if release_key in decision.forced_dup_keys else "",
                list(release_key) or ["<none>"]))
            for fpath, tags in sorted(decision.releases[release_key]):
                print("         {0}".format(os.path.relpath(fpath, roms_dir)))

    for moves, header, label in (
        (plan.bios_moves, "BIOS files ({0} found):", "BIOS"),
        (plan.proto_beta_moves, "Proto/Beta files ({0} found):", "PROTO/BETA"),
        (plan.program_moves, "Program files ({0} found):", "PROGRAM"),
        (plan.reject_moves, "Rejected files ({0} found, via [reject]):", "REJECT"),
        (plan.redundant_moves,
         "Redundant raw disc files ({0} found, CHD already present):", "REDUNDANT"),
    ):
        if not moves:
            continue
        print("\n" + header.format(len(moves)))
        for src, _dest in moves:
            print("  [{0}] {1}".format(label, os.path.relpath(src, roms_dir)))


def read_scan_marker(roms_dir):
    """Return {'version': ..., 'last_scanned': ...} from a prior run's
    marker file in this roms_dir, or None if it doesn't exist / is unreadable.
    """
    path = os.path.join(roms_dir, SCAN_MARKER_FILENAME)
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def write_scan_marker(roms_dir):
    """Record that this roms_dir was just processed by this script version."""
    path = os.path.join(roms_dir, SCAN_MARKER_FILENAME)
    data = {
        "version": __version__,
        "last_scanned": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except OSError as e:
        print("Warning: could not write scan marker: {0}".format(e), file=sys.stderr)


def log_run(roms_dir, mode, filter_file_used, summary, moved=None, errors=0):
    """Append a timestamped entry to the hidden log file next to this
    script. Never raises -- logging failures shouldn't break a scan.
    """
    try:
        script_dir = os.path.dirname(os.path.abspath(__file__))
        log_path = os.path.join(script_dir, LOG_FILENAME)
        timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        lines = []
        lines.append("=== {0} ===".format(timestamp))
        lines.append("roms_dir:    {0}".format(roms_dir))
        lines.append("mode:        {0}".format(mode))
        lines.append("filter_file: {0}".format(filter_file_used or "(none)"))
        lines.append(
            "games={0} releases={1} kept={2} dup={3} bios={4} "
            "proto_beta={5} program={6} reject={7} redundant={8} "
            "filtered={9}".format(
                summary["games"], summary["releases"], summary["kept"],
                summary["dup"], summary["bios"], summary["proto_beta"],
                summary["program"], summary["reject"], summary["redundant"],
                summary["filtered"]
            )
        )
        if moved is not None:
            lines.append("moved: {0}/{1}  errors: {2}".format(
                moved,
                summary["dup"] + summary["bios"] + summary["proto_beta"]
                + summary["program"] + summary["reject"] + summary["redundant"],
                errors))
        lines.append("")

        with open(log_path, "a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
    except OSError as e:
        print("Warning: could not write log file: {0}".format(e), file=sys.stderr)


# ---- finding potential cross-region duplicate titles ---------------------

# A small, fixed vocabulary of roman numerals used in game titles/sequel
# numbers (e.g. "Final Fantasy VII" vs "Final Fantasy 7") -- large enough
# to cover realistic sequel numbering, never meant to be a general roman
# numeral parser.
ROMAN_TO_ARABIC = {
    "i": "1", "ii": "2", "iii": "3", "iv": "4", "v": "5", "vi": "6",
    "vii": "7", "viii": "8", "ix": "9", "x": "10", "xi": "11", "xii": "12",
    "xiii": "13", "xiv": "14", "xv": "15", "xvi": "16", "xvii": "17",
    "xviii": "18", "xix": "19", "xx": "20",
}

# Small, common words that regions/dump groups are inconsistent about
# including (e.g. "The Legend of Zelda" vs "Legend of Zelda", "Licence to
# Kill" vs "007 - Licence to Kill") -- dropped only for fuzzy comparison,
# never for the real title_key normal duplicate-scan grouping relies on.
FUZZY_TITLE_STOPWORDS = {"the", "a", "an", "and", "of", "to"}

FUZZY_TITLE_SIMILARITY_THRESHOLD = 0.82

# Only tokens at least this long are used as blocking keys when looking
# for potential-match candidates -- long enough that two titles sharing
# one aren't sharing it by coincidence (a short/common word like "man" or
# "game" would otherwise create huge, mostly-useless comparison buckets).
FUZZY_BLOCK_MIN_TOKEN_LEN = 4
# Skip a blocking bucket this large -- almost certainly a generic word
# rather than a real shared title fragment, and comparing everything in
# it pairwise would be wasted (possibly slow) work.
FUZZY_BLOCK_MAX_SIZE = 400


def fuzzy_title_tokens(title):
    """A looser tokenization than normalize_title(), used only for
    FINDING potential cross-region duplicate titles -- never for the real
    title_key normal duplicate-scan grouping relies on, since it's lossy
    enough to occasionally over-merge two unrelated short titles (that's
    why matches found this way are only ever reported, never auto-moved --
    see find_potential_duplicate_titles()). Converts a roman-numeral
    token to its arabic digit and drops a small set of filler words (see
    FUZZY_TITLE_STOPWORDS).
    """
    tokens = normalize_title(title).split(" ")
    out = []
    for tok in tokens:
        if not tok or tok in FUZZY_TITLE_STOPWORDS:
            continue
        out.append(ROMAN_TO_ARABIC.get(tok, tok))
    return out


def _is_contiguous_sub_or_super(shorter, longer):
    """True if shorter's tokens appear as an exact prefix OR suffix of
    longer's tokens -- e.g. "Licence to Kill" is a suffix of "007 -
    Licence to Kill", and "Aliens" is a prefix of "Aliens - The Computer
    Game" -- the common shape of a region dropping/adding a franchise
    prefix or subtitle.
    """
    if not shorter or len(shorter) >= len(longer):
        return False
    return longer[:len(shorter)] == shorter or longer[-len(shorter):] == shorter


def _differ_only_in_numeric_tokens(tokens_a, tokens_b):
    """True if the two token lists become identical once every purely-
    numeric token is dropped from each -- i.e. the only difference
    between them is which sequel number (or whether one exists at all)
    is present, e.g. "Mega Man 2" vs "Mega Man 3", or "Fatal Fury" vs
    "Fatal Fury 2".

    Deliberately excluded from every match tier in
    find_potential_duplicate_titles(), not just downgraded to medium
    confidence: those are genuinely different games, and numbered
    sequels are common enough in any real ROM library that flagging
    every such pair -- even as a low-confidence hint -- would bury the
    real cross-region matches in noise. The cost is that a same-shape
    but legitimate difference, like a numeric franchise-prefix drop
    ("007 - Licence to Kill" -> "Licence to Kill"), gets excluded too --
    a deliberate trade-off, since it looks identical to a sequel-number
    difference from token position alone.
    """
    non_numeric_a = [t for t in tokens_a if not t.isdigit()]
    non_numeric_b = [t for t in tokens_b if not t.isdigit()]
    return non_numeric_a == non_numeric_b and tokens_a != tokens_b


def extract_regions(tags, region_priority):
    """Return the set of recognized region tokens (lowercased, e.g.
    "usa", "europe") found among these tags. Mirrors region_rank()'s
    parsing of combined tags like "USA, Europe", but collects every
    match instead of just the best-ranked one.
    """
    regions = set()
    for tag in tags:
        for part in re.split(r"[,/]", tag):
            part = part.strip().lower()
            if part in region_priority:
                regions.add(part)
    return regions


def collect_title_index(roms_dir, dup_dir, extensions, region_priority=None):
    """Walk roms_dir and build a simple index of every distinct title_key
    found, independent of any filter file. Skips BIOS/proto-beta/
    (Program)-tagged files, matching scan_rom_files()'s treatment --
    they're not real "duplicate title" candidates.

    Returns {title_key: {"display": str, "files": [path, ...],
    "regions": {region, ...}}}, where "regions" is the union of every
    recognized region tag (see extract_regions()) found across all of
    that title's files.
    """
    region_priority = region_priority or DEFAULT_REGION_PRIORITY
    index = {}
    for root, dirs, files in walk_excluding_dup_dir(roms_dir, dup_dir):
        for fname in files:
            stem, ext = os.path.splitext(fname)
            if ext.lower() not in extensions:
                continue
            base_title, tags = extract_tags(stem)
            if any(is_proto_beta_tag(t) for t in tags):
                continue
            if any(t.strip().lower() == "bios" for t in tags):
                continue
            if any(is_program_tag(t) for t in tags):
                continue

            title_key = normalize_title(base_title) or normalize_title(stem)
            display = base_title.strip() or stem
            entry = index.setdefault(
                title_key, {"display": display, "files": [], "regions": set()})
            entry["files"].append(os.path.join(root, fname))
            entry["regions"].update(extract_regions(tags, region_priority))
    return index


class _UnionFind:
    """Minimal union-find so several pairwise "these look related" edges
    collapse into one cluster per connected group of titles, instead of
    printing overlapping pairs (e.g. three regional names for the same
    game should be one 3-title cluster, not three separate pairs).
    """

    def __init__(self, keys):
        self.parent = {k: k for k in keys}

    def find(self, x):
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[ra] = rb


def _region_relationship(regions_a, regions_b):
    """Classify what a title pair's region tags say about whether
    they're likely the same game under a different name:

      "differ"  -- both sides carry at least one recognized region tag
                   and share none of them (e.g. {"usa"} vs {"europe"}).
                   Two different-looking titles each tied to a
                   different region corroborates that they're regional
                   names for the same game, so this promotes an
                   otherwise-medium text match to high confidence.
      "unknown" -- either side carries no recognized region tag, or the
                   two sides share at least one region (e.g. one release
                   is tagged "(Europe)" and the other "(USA, Europe)") --
                   a shared region isn't necessarily a red flag on its
                   own (a multi-region release naturally overlaps with a
                   single-region one for the same game), so this is
                   treated as no signal either way rather than a reason
                   to exclude the pair.
    """
    if regions_a and regions_b and not (regions_a & regions_b):
        return "differ"
    return "unknown"


def _format_region_set(regions):
    return "/".join(r.title() for r in sorted(regions)) if regions else "unknown"


def find_potential_duplicate_titles(roms_dir, dup_dir, extensions=None, region_priority=None):
    """Look for titles that are probably the same game under a different
    name -- a roman-numeral vs arabic-numeral sequel number, a region
    that drops/adds "The", or a franchise-prefix/subtitle another
    region's release of the same game omits -- none of which
    normalize_title() folds together, so plan_duplicate_scan() treats
    them as entirely separate titles and never compares them.

    Each candidate pair's region tags (see extract_regions()) are used
    as corroborating evidence, not just the title text: two
    differently-spelled titles tied to two entirely different regions
    (e.g. one "(USA)", the other "(Europe)") are more likely the same
    game released under regionally different names, so that promotes an
    otherwise-medium text-similarity match to high confidence. See
    _region_relationship() for exactly when that applies -- a shared or
    missing region tag isn't treated as a red flag, just as no extra
    signal either way.

    This is a discovery report only: nothing is moved, and no filter file
    is consulted -- a "potential" match still needs human judgement (see
    fuzzy_title_tokens()'s docstring for why an automatic merge would be
    unsafe -- e.g. "Fatal Fury" vs "Fatal Fury 2" are NOT the same game
    despite looking related). Review the printed clusters, then rename
    files so normalize_title() already agrees on the ones that really
    are the same game.

    Returns a list of clusters, each
    {"confidence": "high"|"medium", "reasons": [str, ...],
     "titles": [(title_key, display_title, [file_path, ...]), ...]},
    sorted highest-confidence, then largest cluster, first.
    """
    extensions = extensions if extensions is not None else ROM_EXTENSIONS_DEFAULT
    region_priority = region_priority or DEFAULT_REGION_PRIORITY
    index = collect_title_index(roms_dir, dup_dir, extensions, region_priority=region_priority)
    title_keys = list(index.keys())

    uf = _UnionFind(title_keys)
    pair_reasons = {}  # (title_key_a, title_key_b) sorted -> (confidence, reason)
    fuzzy_tokens = {tk: fuzzy_title_tokens(index[tk]["display"]) for tk in title_keys}

    def record(tk_a, tk_b, confidence, reason):
        uf.union(tk_a, tk_b)
        pair_key = tuple(sorted((tk_a, tk_b)))
        existing = pair_reasons.get(pair_key)
        if existing is None or (existing[0] == "medium" and confidence == "high"):
            pair_reasons[pair_key] = (confidence, reason)

    # Pass 1: titles that collapse to the exact same fuzzy token sequence
    # (roman numeral / filler-word differences only).
    fuzzy_key_groups = defaultdict(list)
    for tk in title_keys:
        key = " ".join(fuzzy_tokens[tk])
        if key:
            fuzzy_key_groups[key].append(tk)
    for group in fuzzy_key_groups.values():
        if len(group) < 2:
            continue
        for i in range(len(group)):
            for j in range(i + 1, len(group)):
                tk_a, tk_b = group[i], group[j]
                relationship = _region_relationship(
                    index[tk_a]["regions"], index[tk_b]["regions"])
                reason = "same after ignoring \"the/a/an/of/and/to\" and roman numerals"
                if relationship == "differ":
                    reason += "; region tags differ ({0} vs {1})".format(
                        _format_region_set(index[tk_a]["regions"]),
                        _format_region_set(index[tk_b]["regions"]))
                record(tk_a, tk_b, "high", reason)

    # Pass 2: block titles by any long, distinctive shared token, then
    # check each candidate pair for a prefix/suffix relationship or high
    # text similarity.
    blocks = defaultdict(list)
    for tk in title_keys:
        for tok in set(fuzzy_tokens[tk]):
            if len(tok) >= FUZZY_BLOCK_MIN_TOKEN_LEN and not tok.isdigit():
                blocks[tok].append(tk)

    checked_pairs = set()
    for bucket in blocks.values():
        if len(bucket) < 2 or len(bucket) > FUZZY_BLOCK_MAX_SIZE:
            continue
        for i in range(len(bucket)):
            for j in range(i + 1, len(bucket)):
                tk_a, tk_b = bucket[i], bucket[j]
                pair_key = tuple(sorted((tk_a, tk_b)))
                if pair_key in checked_pairs:
                    continue
                checked_pairs.add(pair_key)

                tokens_a, tokens_b = fuzzy_tokens[tk_a], fuzzy_tokens[tk_b]
                if tokens_a == tokens_b:
                    continue
                if _differ_only_in_numeric_tokens(tokens_a, tokens_b):
                    continue

                relationship = _region_relationship(
                    index[tk_a]["regions"], index[tk_b]["regions"])
                region_suffix = ""
                if relationship == "differ":
                    region_suffix = "; region tags differ ({0} vs {1})".format(
                        _format_region_set(index[tk_a]["regions"]),
                        _format_region_set(index[tk_b]["regions"]))

                shorter, longer = (
                    (tokens_a, tokens_b) if len(tokens_a) <= len(tokens_b)
                    else (tokens_b, tokens_a))
                if _is_contiguous_sub_or_super(shorter, longer):
                    record(tk_a, tk_b, "high",
                           "one title is a prefix/suffix of the other "
                           "(subtitle or franchise-prefix difference)" + region_suffix)
                    continue

                ratio = difflib.SequenceMatcher(
                    None, " ".join(tokens_a), " ".join(tokens_b)).ratio()
                if ratio >= FUZZY_TITLE_SIMILARITY_THRESHOLD:
                    # A confirmed region difference is corroborating evidence
                    # strong enough to promote an otherwise-medium text match.
                    confidence = "high" if relationship == "differ" else "medium"
                    record(tk_a, tk_b, confidence,
                           "text similarity {0:.0%}".format(ratio) + region_suffix)

    groups = defaultdict(list)
    for tk in title_keys:
        groups[uf.find(tk)].append(tk)

    clusters = []
    for members in groups.values():
        if len(members) < 2:
            continue
        reasons = set()
        confidence = "medium"
        for i in range(len(members)):
            for j in range(i + 1, len(members)):
                hit = pair_reasons.get(tuple(sorted((members[i], members[j]))))
                if hit:
                    reasons.add(hit[1])
                    if hit[0] == "high":
                        confidence = "high"
        clusters.append({
            "confidence": confidence,
            "reasons": sorted(reasons),
            "titles": sorted(
                (tk, index[tk]["display"], sorted(index[tk]["files"]))
                for tk in members),
        })

    confidence_rank = {"high": 0, "medium": 1}
    clusters.sort(key=lambda c: (confidence_rank[c["confidence"]], -len(c["titles"])))
    return clusters


def find_potential_duplicates(roms_dir, dup_dir, extensions=None, region_priority=None):
    """Print the clusters from find_potential_duplicate_titles(). Report
    only -- see that function's docstring for why nothing gets moved.
    Returns the cluster list.
    """
    clusters = find_potential_duplicate_titles(
        roms_dir, dup_dir, extensions=extensions, region_priority=region_priority)
    if not clusters:
        print("No potential cross-region duplicate titles found under {0}.".format(roms_dir))
        return clusters

    for cluster in clusters:
        print("\n[{0}] possible match ({1})".format(
            cluster["confidence"].upper(), "; ".join(cluster["reasons"])))
        for title_key, display, files in cluster["titles"]:
            print("  {0!r}  ({1} file(s))".format(display, len(files)))
            for fpath in files:
                print("    {0}".format(os.path.relpath(fpath, roms_dir)))

    high = sum(1 for c in clusters if c["confidence"] == "high")
    medium = len(clusters) - high
    print("\n{0} potential duplicate cluster(s) found ({1} high-confidence, "
          "{2} medium-confidence). Nothing was moved -- review manually, "
          "then rename files so they share a title (or add a "
          "release-specific [blacklist] entry once you're sure) before "
          "re-running the normal scan.".format(len(clusters), high, medium))
    return clusters


def main():
    parser = argparse.ArgumentParser(description="Find and move duplicate ROMs.")
    parser.add_argument("--version", action="version",
                         version="rom_cleanup.py {0}".format(__version__))
    parser.add_argument("roms_dir", help="Path to the ROMs folder")
    parser.add_argument("--apply", action="store_true",
                         help="Actually move files (default is dry-run / preview only)")
    parser.add_argument("-v", "--verbose", action="store_true",
                         help="Print details for every group, not just duplicates")
    parser.add_argument("--dup-dir", default=None,
                         help="Name/path of duplicates folder (default: <roms_dir>/.duplicates)")
    parser.add_argument("--regions", default=None,
                         help="Comma-separated region preference, best first "
                              "(default: USA,World,Europe,Japan,...)")
    parser.add_argument("--ext", default=None,
                         help="Comma-separated list of extensions to consider "
                              "(default: common ROM/disc image extensions)")
    parser.add_argument("--filter-file", default=None, metavar="FILE",
                         help="Path to a flat text file with [whitelist], "
                              "[blacklist], and/or [reject] sections. A plain "
                              "title line (e.g. 'Chrono Trigger') applies to the "
                              "whole game. A line with tags (e.g. 'Shadow Dancer "
                              "(World)') targets that one specific release: "
                              "whitelist it to pin it as the keeper, or blacklist "
                              "it to force it to always be treated as a duplicate "
                              "(only matters if another release exists to lose "
                              "the comparison to). [reject] always routes a "
                              "matching title/release to .duplicates/{0}/, with "
                              "no comparison needed -- for a release with no "
                              "recognizable tag of its own (e.g. a one-off "
                              "compilation) that would otherwise never leave "
                              "roms_dir. Default: <roms_dir>/{1} if it "
                              "exists.".format(REJECT_SUBDIR, DEFAULT_FILTER_FILENAME))
    parser.add_argument("--recursive", action="store_true", default=True,
                         help="Scan subfolders too (default: on)")
    parser.add_argument("--flatten-alpha-dirs", action="store_true",
                         help="Move everything out of single-letter A-Z (or catch-all "
                              "'#'/'0-9'/'Misc'/'[BIOS]'/etc) bucket folders directly under "
                              "roms_dir up into roms_dir itself, then remove the "
                              "emptied bucket folders. Respects --apply (dry-run "
                              "preview by default). Runs standalone -- does not also "
                              "perform the normal duplicate scan in the same invocation.")
    parser.add_argument("--gamelist-clean", action="store_true",
                         help="Find every {0} under roms_dir (ES-DE/EmulationStation "
                              "style -- works whether roms_dir is a single console "
                              "folder or a top-level ROMs folder with one per system) "
                              "and remove <game> entries whose block contains "
                              "\"{1}\" (libretro core setup/config entries), so they "
                              "don't show up in ES-DE. Backs up each changed file to a "
                              "hidden {2} next to it first (overwritten on re-run). "
                              "Respects --apply (dry-run preview by default). Runs "
                              "standalone.".format(
                                  GAMELIST_FILENAME, GAMELIST_NOTGAME_MARKER,
                                  GAMELIST_BACKUP_FILENAME))
    parser.add_argument("--convert-to-chd", action="store_true",
                         help="Find every .cue file, plus every standalone .iso "
                              "not already referenced by one, under roms_dir, and "
                              "convert each to .chd via 'chdman createcd' (requires "
                              "chdman, which ships with mame-tools, on PATH -- or "
                              "pass --chdman-path), moving the result up into "
                              "roms_dir itself if it wasn't already directly there. "
                              "A standalone .iso gets a minimal single-track .cue "
                              "synthesized for it on the fly (removed again "
                              "afterward) -- its CD track type (raw-sector vs plain "
                              "data image) is detected from the file itself, not "
                              "guessed from the extension; a .iso whose sector "
                              "layout can't be determined is left alone and "
                              "flagged for manual review. Skips files already "
                              "converted. Original .bin/.cue/.iso files are left in "
                              "place -- run the normal scan with --apply "
                              "afterward to route them into "
                              ".duplicates/Redundant-Raw-Disc/. With --apply, "
                              "announces each conversion right before it starts "
                              "(a single updating line on a real terminal, one "
                              "line per item when output is redirected) instead "
                              "of dumping the whole job list upfront and going "
                              "silent while chdman works through it. Respects "
                              "--apply (dry-run preview by default, showing the "
                              "full list). Runs standalone.")
    parser.add_argument("--chdman-path", default=None, metavar="PATH",
                         help="Path to the chdman executable, if it's not on "
                              "PATH (default: look up 'chdman' on PATH)")
    parser.add_argument("--make-m3u", action="store_true",
                         help="Group multi-disc .chd (CD-based systems) or .rvz "
                              "(GameCube/Wii, via Dolphin) releases (e.g. \"Game "
                              "(USA) (Disc 1).chd\", \"(Disc 2).chd\") behind a "
                              "single playlist entry: the .m3u is written directly "
                              "in roms_dir (e.g. \"Game (USA).m3u\") while the disc "
                              "files move into their own subfolder under a hidden "
                              "folder in roms_dir named after their format (e.g. "
                              "\".chd/Game (USA)/\" or \".rvz/Game (USA)/\"), which "
                              "ES-DE and RetroArch both ignore when scanning -- so "
                              "only the .m3u shows up, not a second folder entry "
                              "for the same game. Discs of different formats never "
                              "group together. Skips releases already grouped with "
                              "an up-to-date .m3u, and migrates any release still "
                              "in an older layout into this one. A lone disc-tagged "
                              "file with no siblings, or two files claiming the "
                              "same disc number, are left alone. Note: Dolphin's "
                              "automatic disc-change on .m3u is off by default -- "
                              "enable it in Dolphin's settings for the same "
                              "single-entry auto-swap experience CD-based systems "
                              "get. Respects --apply (dry-run preview by default). "
                              "Runs standalone.")
    parser.add_argument("--isolate-imports", action="store_true",
                         help="Move every title with NO North-American-tagged "
                              "release (no \"USA\" or \"World\" tag on any of its "
                              "entries) into roms_dir/.imports/ -- a hidden folder, so "
                              "ES-DE and RetroArch skip it -- keeping every "
                              "region/revision of that title together. Titles with "
                              "at least one USA/World release are left in roms_dir. "
                              "Considers roms_dir's direct children only: ROM files, "
                              "an --make-m3u playlist (moved together with its "
                              "hidden disc folder), or a whole release subfolder. "
                              "BIOS-, proto/beta-, and (Program)-tagged entries "
                              "are left alone. "
                              "Respects the [whitelist]/[blacklist]/[reject] "
                              "entries in rom_filters.txt (--filter-file): a "
                              "whole-title or release-specific whitelist entry "
                              "keeps that title in place, a whole-title blacklist "
                              "or [reject] entry always wins and keeps it too "
                              "(a rejected title is routed to .duplicates/{0}/ "
                              "via the normal scan instead); a release-specific "
                              "blacklist or reject entry does NOT apply here (it "
                              "means \"lose the duplicate comparison\", not "
                              "\"protect from being moved\"). "
                              "A leftover visible \"Imports/\" folder from before "
                              "this one was hidden is migrated into \".imports/\" "
                              "automatically. Respects --apply (dry-run preview by "
                              "default). Runs standalone.".format(REJECT_SUBDIR))
    parser.add_argument("--fix-filename-spacing", action="store_true",
                         help="Find every .chd/.iso file under roms_dir with a "
                              "trailing space right before the extension (e.g. "
                              "\"Game .chd\") -- a naming artifact some ripping/"
                              "download tools leave behind -- and strip it (e.g. "
                              "\"Game.chd\"). Also updates a .cue that names the "
                              "old filename as its data track, or an .m3u playlist "
                              "(see --make-m3u) that references it, so neither is "
                              "left pointing at a file that no longer exists. "
                              "Respects --apply (dry-run preview by default). "
                              "Runs standalone.")
    parser.add_argument("--find-potential-duplicates", action="store_true",
                         help="Look for titles that are probably the same game "
                              "under a different name across regions -- a roman-"
                              "numeral vs arabic-numeral sequel number, a region "
                              "that drops/adds \"The\", or a franchise-prefix/"
                              "subtitle another region's release omits -- none of "
                              "which the normal scan's exact title match catches "
                              "on its own. A candidate pair's region tags are used "
                              "as corroborating evidence (respects --regions): two "
                              "differently-spelled titles tied to two entirely "
                              "DIFFERENT regions promotes an otherwise-medium "
                              "text-similarity match to high confidence. Report "
                              "only: nothing is moved and no filter file is consulted, "
                              "since a fuzzy match still needs human judgement "
                              "(e.g. \"Fatal Fury\" vs \"Fatal Fury 2\" are NOT "
                              "the same game despite looking related). Review the "
                              "printed clusters, then rename files so the normal "
                              "scan's exact title match already agrees on the "
                              "ones that really are the same game. Ignores "
                              "--apply. Runs standalone.")
    args = parser.parse_args()

    roms_dir = os.path.abspath(args.roms_dir)
    if not os.path.isdir(roms_dir):
        print("Error: '{0}' is not a directory.".format(roms_dir), file=sys.stderr)
        sys.exit(1)

    dup_dir = os.path.abspath(args.dup_dir) if args.dup_dir else os.path.join(roms_dir, ".duplicates")

    region_priority = (
        [r.strip().lower() for r in args.regions.split(",")]
        if args.regions else DEFAULT_REGION_PRIORITY
    )

    standalone_flags = [name for enabled, name in (
        (args.flatten_alpha_dirs, "--flatten-alpha-dirs"),
        (args.gamelist_clean, "--gamelist-clean"),
        (args.convert_to_chd, "--convert-to-chd"),
        (args.make_m3u, "--make-m3u"),
        (args.isolate_imports, "--isolate-imports"),
        (args.fix_filename_spacing, "--fix-filename-spacing"),
        (args.find_potential_duplicates, "--find-potential-duplicates"),
    ) if enabled]
    if len(standalone_flags) > 1:
        print("Error: {0} can't be combined -- run them one at a time.".format(
            " and ".join(standalone_flags)), file=sys.stderr)
        sys.exit(1)

    if args.flatten_alpha_dirs:
        flatten_alpha_dirs(roms_dir, dup_dir, args.apply)
        return

    if args.gamelist_clean:
        gamelist_clean(roms_dir, dup_dir, args.apply)
        return

    if args.convert_to_chd:
        convert_to_chd(roms_dir, dup_dir, args.apply, args.chdman_path)
        return

    if args.make_m3u:
        make_m3u_playlists(roms_dir, dup_dir, args.apply)
        return

    if args.isolate_imports:
        parsed_filter, filter_file_used = resolve_filter_file(roms_dir, args.filter_file)
        isolate_imports(roms_dir, dup_dir, args.apply,
                         blacklist_titles=parsed_filter["blacklist_titles"],
                         whitelist_titles=parsed_filter["whitelist_titles"],
                         whitelist_releases=parsed_filter["whitelist_releases"],
                         reject_titles=parsed_filter["reject_titles"],
                         filter_file_used=filter_file_used)
        return

    if args.fix_filename_spacing:
        fix_filename_spacing(roms_dir, dup_dir, args.apply)
        return

    if args.find_potential_duplicates:
        find_potential_duplicates(roms_dir, dup_dir, region_priority=region_priority)
        return

    prior_scan = read_scan_marker(roms_dir)
    if prior_scan:
        prior_version = prior_scan.get("version", "unknown")
        prior_date = prior_scan.get("last_scanned", "unknown date")
        if prior_version != __version__:
            print("Note: this folder was last processed by rom_cleanup.py "
                  "v{0} on {1}. You are now running v{2} -- behavior may "
                  "have changed since then.\n".format(prior_version, prior_date, __version__))
        elif args.verbose:
            print("This folder was last processed by rom_cleanup.py v{0} "
                  "on {1} (same version as now).\n".format(prior_version, prior_date))

    extensions = (
        {e.strip().lower() if e.strip().startswith(".") else "." + e.strip().lower()
         for e in args.ext.split(",")}
        if args.ext else ROM_EXTENSIONS_DEFAULT
    )

    parsed_filter, filter_file_used = resolve_filter_file(roms_dir, args.filter_file)
    whitelist_titles = parsed_filter["whitelist_titles"]
    whitelist_releases = parsed_filter["whitelist_releases"]
    blacklist_titles = parsed_filter["blacklist_titles"]
    blacklist_releases = parsed_filter["blacklist_releases"]
    reject_titles = parsed_filter["reject_titles"]
    reject_releases = parsed_filter["reject_releases"]

    plan = plan_duplicate_scan(
        roms_dir, dup_dir,
        region_priority=region_priority, extensions=extensions,
        whitelist_titles=whitelist_titles, whitelist_releases=whitelist_releases,
        blacklist_titles=blacklist_titles, blacklist_releases=blacklist_releases,
        reject_titles=reject_titles, reject_releases=reject_releases)

    for warning in plan.warnings:
        print("\nWarning: {0}".format(warning), file=sys.stderr)

    print_scan_plan(plan, roms_dir, verbose=args.verbose)

    to_move = all_planned_moves(plan)

    # ---- Summary ----
    print("\n" + "=" * 60)
    print("Games (unique titles):     {0}".format(plan.total_titles))
    print("Releases found in total:   {0}".format(plan.total_releases))
    print("Files kept in place:       {0}".format(plan.kept_files))
    print("Files marked as dupes:     {0}".format(plan.dup_files))
    print("BIOS files set aside:      {0}".format(len(plan.bios_moves)))
    print("Proto/Beta files set aside:{0}".format(len(plan.proto_beta_moves)))
    print("Program files set aside:   {0}".format(len(plan.program_moves)))
    print("Rejected files set aside:  {0}".format(len(plan.reject_moves)))
    print("Redundant raw disc files:  {0}".format(len(plan.redundant_moves)))
    if filter_file_used:
        print("Filter file used:          {0}".format(filter_file_used))
        print("Files skipped by filter:   {0}".format(plan.filtered_out))
    if plan.skipped and args.verbose:
        print("Skipped (unrecognized extension): {0}".format(len(plan.skipped)))
    print("=" * 60)

    def print_filter_report():
        if not (filter_file_used and any([whitelist_titles, whitelist_releases,
                                           blacklist_titles, blacklist_releases,
                                           reject_titles, reject_releases])):
            return
        print("\n--- Filter file details ({0}) ---".format(filter_file_used))

        if reject_titles:
            print("\n[reject] whole titles -- always routed to .duplicates/{0}/:".format(
                REJECT_SUBDIR))
            for norm, original in sorted(reject_titles.items(),
                                          key=lambda kv: kv[1].lower()):
                count = plan.reject_hits.get(original, 0)
                note = "({0} file(s) rejected)".format(count) if count else "(no matching files found)"
                print("  {0}  {1}".format(original, note))

        if reject_releases:
            print("\n[reject] specific releases -- always routed to .duplicates/{0}/:".format(
                REJECT_SUBDIR))
            for title_key, tag_set, original in sorted(reject_releases, key=lambda e: e[2].lower()):
                count = plan.reject_hits.get(original, 0)
                note = "({0} file(s) rejected)".format(count) if count else "(no matching files found)"
                print("  {0}  {1}".format(original, note))

        if blacklist_titles:
            print("\n[blacklist] whole titles -- always left untouched:")
            for norm, original in sorted(blacklist_titles.items(),
                                          key=lambda kv: kv[1].lower()):
                count = plan.blacklist_hits.get(norm, 0)
                note = "({0} file(s) protected)".format(count) if count else "(no matching files found)"
                print("  {0}  {1}".format(original, note))

        if blacklist_releases:
            print("\n[blacklist] specific releases -- always forced to duplicate:")
            for title_key, tag_set, original in sorted(blacklist_releases, key=lambda e: e[2].lower()):
                count = plan.force_dup_hits.get(original, 0)
                note = "({0} file(s) forced to dup)".format(count) if count else "(no matching files found)"
                print("  {0}  {1}".format(original, note))

        if whitelist_titles:
            print("\n[whitelist] whole titles -- only these titles are processed:")
            for norm, original in sorted(whitelist_titles.items(),
                                          key=lambda kv: kv[1].lower()):
                count = plan.whitelist_hits.get(norm, 0)
                note = "({0} file(s) matched)".format(count) if count else "(no matching files found)"
                print("  {0}  {1}".format(original, note))

        if whitelist_releases:
            print("\n[whitelist] specific releases -- pinned as the forced keeper:")
            for title_key, tag_set, original in sorted(whitelist_releases, key=lambda e: e[2].lower()):
                count = plan.pin_hits.get(original, 0)
                note = "({0} file(s) pinned)".format(count) if count else "(no matching files found)"
                print("  {0}  {1}".format(original, note))

        print("-" * 60)

    def print_last_scan_footer():
        print("")
        if prior_scan:
            print("Last scanned: v{0} on {1}".format(
                prior_scan.get("version", "unknown"),
                prior_scan.get("last_scanned", "unknown date")))
        else:
            print("Last scanned: never (this is the first scan of this folder)")

    summary = {
        "games": plan.total_titles, "releases": plan.total_releases,
        "kept": plan.kept_files, "dup": plan.dup_files,
        "bios": len(plan.bios_moves), "proto_beta": len(plan.proto_beta_moves),
        "program": len(plan.program_moves), "reject": len(plan.reject_moves),
        "redundant": len(plan.redundant_moves),
        "filtered": plan.filtered_out,
    }
    run_mode = "APPLY" if args.apply else "DRY RUN"

    if not to_move:
        print("No duplicates found. Nothing to do.")
        print_filter_report()
        print_last_scan_footer()
        if args.apply:
            write_scan_marker(roms_dir)
            log_run(roms_dir, run_mode, filter_file_used, summary)
        return

    if not args.apply:
        print("\nDRY RUN -- no files were moved. Would move {0} "
              "file(s) into:\n  {1}\n".format(len(to_move), dup_dir))
        print("Re-run with --apply once you're happy with the grouping above.")
        print_filter_report()
        print_last_scan_footer()
        return

    os.makedirs(dup_dir, exist_ok=True)
    moved = 0
    errors = 0
    source_dirs = set()
    for src, dest in to_move:
        try:
            source_dirs.add(os.path.dirname(src))
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            shutil.move(src, dest)
            moved += 1
        except OSError as e:
            errors += 1
            print("  ERROR moving {0} -> {1}: {2}".format(src, dest, e), file=sys.stderr)

    print("\nMoved {0}/{1} duplicate file(s) into: {2}".format(moved, len(to_move), dup_dir))

    removed_dirs = remove_now_empty_dirs(roms_dir, source_dirs)
    if removed_dirs:
        print("\nRemoved {0} now-empty source folder(s):".format(len(removed_dirs)))
        for d in sorted(removed_dirs):
            print("  {0}".format(os.path.relpath(d, roms_dir)))
    print_filter_report()
    print_last_scan_footer()
    write_scan_marker(roms_dir)
    log_run(roms_dir, run_mode, filter_file_used, summary, moved=moved, errors=errors)


if __name__ == "__main__":
    main()
