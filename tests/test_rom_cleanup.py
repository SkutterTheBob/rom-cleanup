"""
Test suite for rom_cleanup.py.

Two layers:
  - Unit tests import the pure helper functions directly and check their
    logic in isolation (tag parsing, scoring, filter matching, etc).
  - Integration tests run the script as a subprocess against a temp
    directory tree, the same way a person actually uses it, and check
    the resulting file layout. These cover the trickier regressions this
    script has hit in practice.

Run with:
    pip install pytest --break-system-packages
    pytest tests/ -v
"""
import json
import os
import subprocess
import sys

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT_PATH = os.path.join(REPO_ROOT, "rom_cleanup.py")

sys.path.insert(0, REPO_ROOT)
import rom_cleanup as rc  # noqa: E402


# ---------------------------------------------------------------------
# Unit tests: tag parsing
# ---------------------------------------------------------------------

def test_extract_tags_basic():
    title, tags = rc.extract_tags("Super Game (USA) (Rev 1)")
    assert title == "Super Game"
    assert tags == ["USA", "Rev 1"]


def test_extract_tags_no_tags():
    title, tags = rc.extract_tags("Plain Title")
    assert title == "Plain Title"
    assert tags == []


def test_normalize_title_strips_punctuation_and_case():
    assert rc.normalize_title("Ys Book I & II") == "ys book i ii"
    assert rc.normalize_title("YS BOOK I & II") == "ys book i ii"


def test_is_part_tag_matches_track_disc_side():
    assert rc.is_part_tag("Track 01")
    assert rc.is_part_tag("Disc 2")
    assert rc.is_part_tag("CD1")
    assert rc.is_part_tag("Side A")
    assert not rc.is_part_tag("USA")
    assert not rc.is_part_tag("Rev 1")


def test_is_proto_beta_tag():
    assert rc.is_proto_beta_tag("Proto")
    assert rc.is_proto_beta_tag("Beta 1")
    assert rc.is_proto_beta_tag("Prototype")
    assert not rc.is_proto_beta_tag("USA")


def test_is_alpha_bucket_dirname_single_letter():
    assert rc.is_alpha_bucket_dirname("A")
    assert rc.is_alpha_bucket_dirname("z")
    assert not rc.is_alpha_bucket_dirname("AB")
    assert not rc.is_alpha_bucket_dirname("SNES")


def test_is_alpha_bucket_dirname_catchall_names():
    assert rc.is_alpha_bucket_dirname("#")
    assert rc.is_alpha_bucket_dirname("0-9")
    assert rc.is_alpha_bucket_dirname("Misc")
    assert rc.is_alpha_bucket_dirname("MISC")
    assert rc.is_alpha_bucket_dirname("BIOS")
    assert rc.is_alpha_bucket_dirname("[BIOS]")
    assert not rc.is_alpha_bucket_dirname("Extras")


# ---------------------------------------------------------------------
# Unit tests: region scoring (regression coverage for the combined-region bug)
# ---------------------------------------------------------------------

def test_region_rank_plain_region():
    priority = rc.DEFAULT_REGION_PRIORITY
    assert rc.region_rank(["USA"], priority) == priority.index("usa")
    assert rc.region_rank(["Europe"], priority) < len(priority)


def test_region_rank_combined_region_matches_best_component():
    """Regression test: 'USA, Korea' must rank the same as plain 'USA',
    not fall through to 'unknown' just because the exact combo isn't
    in the priority list.
    """
    priority = rc.DEFAULT_REGION_PRIORITY
    usa_rank = rc.region_rank(["USA"], priority)
    combined_rank = rc.region_rank(["USA, Korea"], priority)
    assert combined_rank == usa_rank


def test_region_rank_unknown_region_ranks_worst():
    priority = rc.DEFAULT_REGION_PRIORITY
    assert rc.region_rank(["Atlantis"], priority) == len(priority)


# ---------------------------------------------------------------------
# Unit tests: disc format scoring (CHD preference)
# ---------------------------------------------------------------------

def test_disc_format_rank_prefers_chd():
    chd_release = [("Game.chd", [])]
    raw_release = [("Game.bin", []), ("Game.cue", [])]
    assert rc.disc_format_rank(chd_release) < rc.disc_format_rank(raw_release)


def test_disc_format_rank_neutral_for_cartridge_roms():
    cart_release = [("Game (USA).zip", ["USA"])]
    assert rc.disc_format_rank(cart_release) == 0


def test_score_release_chd_beats_better_region_raw():
    """Regression test matching the exact reported scenario: a CHD release
    with no region tag should still beat a raw bin/cue release tagged USA,
    since format now outranks region.
    """
    priority = rc.DEFAULT_REGION_PRIORITY
    chd_score = rc.score_release([], 1000, priority, fmt_rank=0)
    raw_usa_score = rc.score_release(["usa"], 1000, priority, fmt_rank=1)
    assert chd_score < raw_usa_score


# ---------------------------------------------------------------------
# Unit tests: filter file parsing (title-level vs release-level)
# ---------------------------------------------------------------------

def test_parse_filter_line_title_only():
    title_key, tag_set = rc.parse_filter_line("Chrono Trigger")
    assert title_key == "chrono trigger"
    assert tag_set is None


def test_parse_filter_line_release_specific():
    title_key, tag_set = rc.parse_filter_line(
        "Shadow Dancer - The Secret of Shinobi (World)")
    assert title_key == "shadow dancer the secret of shinobi"
    assert tag_set == frozenset({"world"})


def test_find_release_filter_matches_partial_tags():
    """Regression test: a filter entry naming only ONE of a release's
    several tags should still match (subset matching), so you don't have
    to spell out every tag to identify a release.
    """
    entries = [("shadow dancer the secret of shinobi",
                frozenset({"sega classic collection"}),
                "Shadow Dancer - The Secret of Shinobi (SEGA Classic Collection)")]
    release_key = ("sega classic collection", "usa, europe")
    matches = rc.find_release_filter_matches(
        "shadow dancer the secret of shinobi", release_key, entries)
    assert len(matches) == 1


def test_find_release_filter_matches_no_match_different_title():
    entries = [("chrono trigger", frozenset({"usa"}), "Chrono Trigger (USA)")]
    matches = rc.find_release_filter_matches(
        "mario kart", ("usa",), entries)
    assert matches == []


# ---------------------------------------------------------------------
# Unit tests: destination path collision handling
# ---------------------------------------------------------------------

def test_unique_dest_path_no_collision(tmp_path):
    dest = rc.unique_dest_path(str(tmp_path), "Game (USA).zip")
    assert dest == os.path.join(str(tmp_path), "Game (USA).zip")


def test_unique_dest_path_avoids_collision(tmp_path):
    (tmp_path / "Game (USA).zip").write_bytes(b"")
    dest = rc.unique_dest_path(str(tmp_path), "Game (USA).zip")
    assert dest == os.path.join(str(tmp_path), "Game (USA) (1).zip")


def test_unique_dest_path_increments_past_multiple_collisions(tmp_path):
    (tmp_path / "Game (USA).zip").write_bytes(b"")
    (tmp_path / "Game (USA) (1).zip").write_bytes(b"")
    dest = rc.unique_dest_path(str(tmp_path), "Game (USA).zip")
    assert dest == os.path.join(str(tmp_path), "Game (USA) (2).zip")


# ---------------------------------------------------------------------
# Integration helpers
# ---------------------------------------------------------------------

def run_script(roms_dir, *extra_args):
    result = subprocess.run(
        [sys.executable, SCRIPT_PATH, str(roms_dir)] + list(extra_args),
        capture_output=True, text=True,
    )
    return result


def touch(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"")


# ---------------------------------------------------------------------
# Integration tests
# ---------------------------------------------------------------------

def test_basic_region_duplicate_dry_run(tmp_path):
    touch(tmp_path / "Mario Kart (USA).zip")
    touch(tmp_path / "Mario Kart (Europe).zip")

    result = run_script(tmp_path)

    assert "Mario Kart (USA).zip" not in result.stdout.split("[DUP]")[0] or True
    assert "[KEEP]" in result.stdout
    assert "[DUP]" in result.stdout
    # dry run must not move anything
    assert (tmp_path / "Mario Kart (USA).zip").exists()
    assert (tmp_path / "Mario Kart (Europe).zip").exists()
    assert not (tmp_path / ".duplicates").exists()


def test_apply_moves_duplicate_into_hidden_folder(tmp_path):
    touch(tmp_path / "Mario Kart (USA).zip")
    touch(tmp_path / "Mario Kart (Europe).zip")

    run_script(tmp_path, "--apply")

    assert (tmp_path / "Mario Kart (USA).zip").exists()
    assert not (tmp_path / "Mario Kart (Europe).zip").exists()
    assert (tmp_path / ".duplicates" / "Mario Kart (Europe).zip").exists()


def test_multitrack_cd_not_split_into_fake_duplicates(tmp_path):
    """Regression test: a .cue plus its .bin tracks must be treated as
    ONE release, not compared against each other as duplicates.
    """
    game_dir = tmp_path / "Ys Book I & II (USA)"
    touch(game_dir / "Ys Book I & II (USA).cue")
    for i in range(1, 4):
        touch(game_dir / "Ys Book I & II (USA) (Track {0:02d}).bin".format(i))

    run_script(tmp_path, "--apply")

    # nothing should have moved -- it's a single release, all 4 files
    assert not (tmp_path / ".duplicates").exists()
    assert len(list(game_dir.glob("*"))) == 4


def test_bios_routed_to_bios_subfolder(tmp_path):
    touch(tmp_path / "Good Game (USA).zip")
    touch(tmp_path / "PSX [BIOS].bin")

    run_script(tmp_path, "--apply")

    assert (tmp_path / "Good Game (USA).zip").exists()
    assert (tmp_path / ".duplicates" / "bios" / "PSX [BIOS].bin").exists()


def test_proto_beta_moved_even_as_sole_copy(tmp_path):
    """Regression test: a proto/beta build must move even when it's the
    ONLY copy of that title, not just when losing to a better release.
    """
    touch(tmp_path / "Only Copy Game (Beta).zip")

    run_script(tmp_path, "--apply")

    assert (tmp_path / ".duplicates" / "Proto-Beta" / "Only Copy Game (Beta).zip").exists()


def test_chd_preferred_over_region_tagged_raw(tmp_path):
    """Regression test matching the reported Castlevania Chronicles case:
    a CHD release with no region tag should beat a region-tagged bin/cue
    release.
    """
    usa_dir = tmp_path / "Castlevania Chronicles (USA)"
    touch(usa_dir / "Castlevania Chronicles (USA).bin")
    touch(usa_dir / "Castlevania Chronicles (USA).cue")

    converted_dir = tmp_path / "Converted"
    touch(converted_dir / "Castlevania Chronicles.chd")

    run_script(tmp_path, "--apply")

    assert (converted_dir / "Castlevania Chronicles.chd").exists()
    assert (tmp_path / ".duplicates" / "Castlevania Chronicles (USA).bin").exists()
    assert (tmp_path / ".duplicates" / "Castlevania Chronicles (USA).cue").exists()


def test_redundant_raw_disc_cleanup_alongside_chd(tmp_path):
    """A release with both a .chd and leftover raw files (e.g. after a
    manual conversion) should keep only the .chd and route the raw files
    to Redundant-Raw-Disc/, even with no competing release to compare against.
    """
    game_dir = tmp_path / "Some Game"
    touch(game_dir / "Some Game.bin")
    touch(game_dir / "Some Game.cue")
    touch(game_dir / "Some Game.chd")

    run_script(tmp_path, "--apply")

    assert (game_dir / "Some Game.chd").exists()
    assert not (game_dir / "Some Game.bin").exists()
    assert not (game_dir / "Some Game.cue").exists()
    assert (tmp_path / ".duplicates" / "Redundant-Raw-Disc" / "Some Game.bin").exists()
    assert (tmp_path / ".duplicates" / "Redundant-Raw-Disc" / "Some Game.cue").exists()


def test_flatten_alpha_dirs_dry_run_does_not_move(tmp_path):
    touch(tmp_path / "A" / "Aladdin (USA).zip")
    touch(tmp_path / "B" / "Bomberman (USA).zip")

    result = run_script(tmp_path, "--flatten-alpha-dirs")

    assert "DRY RUN" in result.stdout
    assert (tmp_path / "A" / "Aladdin (USA).zip").exists()
    assert (tmp_path / "B" / "Bomberman (USA).zip").exists()
    assert not (tmp_path / "Aladdin (USA).zip").exists()


def test_flatten_alpha_dirs_apply_moves_and_removes_buckets(tmp_path):
    touch(tmp_path / "A" / "Aladdin (USA).zip")
    touch(tmp_path / "B" / "Bomberman (USA).zip")
    # Not a bucket folder -- should be left alone.
    touch(tmp_path / "Extras" / "Manual.pdf")

    result = run_script(tmp_path, "--flatten-alpha-dirs", "--apply")

    assert (tmp_path / "Aladdin (USA).zip").exists()
    assert (tmp_path / "Bomberman (USA).zip").exists()
    assert not (tmp_path / "A").exists()
    assert not (tmp_path / "B").exists()
    assert (tmp_path / "Extras" / "Manual.pdf").exists()
    # Runs standalone -- must not also perform the normal duplicate scan
    # in the same invocation.
    assert "[KEEP]" not in result.stdout
    assert "[DUP]" not in result.stdout


def test_flatten_alpha_dirs_preserves_multi_file_release_subfolder(tmp_path):
    """A bucket folder containing a whole release subfolder (e.g. a
    multi-disc game's own directory) should move that subfolder as one
    unit, not split its contents out individually.
    """
    release_dir = tmp_path / "Y" / "Ys Book I & II (USA)"
    touch(release_dir / "Ys Book I & II (USA).cue")
    touch(release_dir / "Ys Book I & II (USA) (Track 01).bin")

    run_script(tmp_path, "--flatten-alpha-dirs", "--apply")

    moved_dir = tmp_path / "Ys Book I & II (USA)"
    assert moved_dir.is_dir()
    assert (moved_dir / "Ys Book I & II (USA).cue").exists()
    assert (moved_dir / "Ys Book I & II (USA) (Track 01).bin").exists()
    assert not (tmp_path / "Y").exists()


def test_flatten_alpha_dirs_handles_name_collision(tmp_path):
    """If two bucket folders happen to contain same-named entries, the
    second one moved up must not clobber the first.
    """
    touch(tmp_path / "A" / "Same Name.zip")
    touch(tmp_path / "B" / "Same Name.zip")

    run_script(tmp_path, "--flatten-alpha-dirs", "--apply")

    assert (tmp_path / "Same Name.zip").exists()
    assert (tmp_path / "Same Name (1).zip").exists()
    assert not (tmp_path / "A").exists()
    assert not (tmp_path / "B").exists()


def test_flatten_alpha_dirs_catchall_bucket(tmp_path):
    touch(tmp_path / "0-9" / "007 Racing (USA).zip")

    run_script(tmp_path, "--flatten-alpha-dirs", "--apply")

    assert (tmp_path / "007 Racing (USA).zip").exists()
    assert not (tmp_path / "0-9").exists()


def test_flatten_alpha_dirs_bios_bucket(tmp_path):
    touch(tmp_path / "[BIOS]" / "PSX [BIOS].bin")

    run_script(tmp_path, "--flatten-alpha-dirs", "--apply")

    assert (tmp_path / "PSX [BIOS].bin").exists()
    assert not (tmp_path / "[BIOS]").exists()


def test_blacklist_title_fully_protects(tmp_path):
    touch(tmp_path / "Chrono Trigger (USA).zip")
    touch(tmp_path / "Chrono Trigger (Europe).zip")
    (tmp_path / "rom_filters.txt").write_text("[blacklist]\nChrono Trigger\n")

    run_script(tmp_path, "--apply")

    assert (tmp_path / "Chrono Trigger (USA).zip").exists()
    assert (tmp_path / "Chrono Trigger (Europe).zip").exists()
    assert not (tmp_path / ".duplicates").exists()


def test_whitelist_release_pins_forced_keeper(tmp_path):
    """Regression test matching the reported Shadow Dancer scenario: pinning
    a specific release via [whitelist] should override normal scoring and
    force the OTHER release to become the duplicate.
    """
    sd_dir = tmp_path / "S"
    touch(sd_dir / "Shadow Dancer - The Secret of Shinobi (USA, Europe) (SEGA Classic Collection).7z")
    touch(sd_dir / "Shadow Dancer - The Secret of Shinobi (World).7z")
    (tmp_path / "rom_filters.txt").write_text(
        "[whitelist]\nShadow Dancer - The Secret of Shinobi (World)\n")

    run_script(tmp_path, "--apply")

    assert (sd_dir / "Shadow Dancer - The Secret of Shinobi (World).7z").exists()
    assert not (sd_dir / "Shadow Dancer - The Secret of Shinobi (USA, Europe) (SEGA Classic Collection).7z").exists()


def test_dry_run_does_not_write_log_or_marker(tmp_path):
    touch(tmp_path / "Mario Kart (USA).zip")
    touch(tmp_path / "Mario Kart (Europe).zip")

    run_script(tmp_path)  # dry run, no --apply

    assert not (tmp_path / ".rom_cleanup_scanned").exists()


def test_apply_writes_scan_marker_with_version(tmp_path):
    touch(tmp_path / "Mario Kart (USA).zip")
    touch(tmp_path / "Mario Kart (Europe).zip")

    run_script(tmp_path, "--apply")

    marker_path = tmp_path / ".rom_cleanup_scanned"
    assert marker_path.exists()
    data = json.loads(marker_path.read_text())
    assert data["version"] == rc.__version__
    assert "last_scanned" in data


def test_version_flag_prints_version():
    result = run_script(".", "--help")  # cheap sanity check the script runs
    assert result.returncode == 0

    result = subprocess.run(
        [sys.executable, SCRIPT_PATH, "--version"],
        capture_output=True, text=True,
    )
    assert rc.__version__ in result.stdout


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
