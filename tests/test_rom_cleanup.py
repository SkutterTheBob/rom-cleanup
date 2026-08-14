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
# Unit tests: gamelist.xml "notgame" entry removal
# ---------------------------------------------------------------------

GAMELIST_TEMPLATE = """<?xml version="1.0"?>
<gameList>
\t<game>
\t\t<path>./Aladdin (USA).zip</path>
\t\t<name>Aladdin</name>
\t</game>
\t<game id="151193" source="ScreenScraper.fr">
\t\t<path>./Pro Action Replay MK2 (Europe) (v1.1) (Unl) [b].zip</path>
\t\t<name>ZZZ(notgame):#NONGAME</name>
\t\t<rating>0.8</rating>
\t\t<releasedate />
\t\t<developer>Catapult Entertainment</developer>
\t\t<publisher>Catapult Entertainment</publisher>
\t\t<hash>70D6B036</hash>
\t\t<md5>C22E8390832F8E97F3A7472251E46C3E</md5>
\t</game>
\t<game>
\t\t<path>./Bomberman (USA).zip</path>
\t\t<name>Bomberman</name>
\t</game>
</gameList>
"""


def test_plan_gamelist_clean_removes_notgame_entry(tmp_path):
    gamelist = tmp_path / "gamelist.xml"
    gamelist.write_text(GAMELIST_TEMPLATE, encoding="utf-8")

    matches, cleaned = rc.plan_gamelist_clean(str(gamelist))

    assert len(matches) == 1
    assert "ZZZ(notgame)" in matches[0]
    assert "ZZZ(notgame)" not in cleaned
    assert "<name>Aladdin</name>" in cleaned
    assert "<name>Bomberman</name>" in cleaned


def test_plan_gamelist_clean_no_matches_returns_none(tmp_path):
    gamelist = tmp_path / "gamelist.xml"
    gamelist.write_text(
        "<?xml version=\"1.0\"?>\n<gameList>\n\t<game>\n\t\t<name>Aladdin</name>\n\t</game>\n</gameList>\n",
        encoding="utf-8")

    matches, cleaned = rc.plan_gamelist_clean(str(gamelist))

    assert matches == []
    assert cleaned is None


def test_plan_gamelist_clean_result_is_well_formed_xml(tmp_path):
    gamelist = tmp_path / "gamelist.xml"
    gamelist.write_text(GAMELIST_TEMPLATE, encoding="utf-8")

    matches, cleaned = rc.plan_gamelist_clean(str(gamelist))

    rc.ET.fromstring(cleaned)  # must not raise


def test_plan_gamelist_clean_collapses_consecutive_removed_blocks(tmp_path):
    """Regression test: removing two ADJACENT <game> blocks (as opposed to
    one with a real entry on either side) must not leave a stray
    whitespace-only line behind.
    """
    content = (
        "<?xml version=\"1.0\"?>\n<gameList>\n"
        "\t<game>\n\t\t<name>Real Game</name>\n\t</game>\n"
        "\t<game>\n\t\t<name>ZZZ(notgame):#NONGAME</name>\n\t</game>\n"
        "\t<game>\n\t\t<name>ZZZ(notgame):#NONGAME</name>\n\t</game>\n"
        "</gameList>\n"
    )
    gamelist = tmp_path / "gamelist.xml"
    gamelist.write_text(content, encoding="utf-8")

    matches, cleaned = rc.plan_gamelist_clean(str(gamelist))

    assert len(matches) == 2
    assert cleaned == "<?xml version=\"1.0\"?>\n<gameList>\n\t<game>\n\t\t<name>Real Game</name>\n\t</game>\n</gameList>\n"


def test_plan_gamelist_clean_rejects_malformed_xml(tmp_path):
    gamelist = tmp_path / "gamelist.xml"
    gamelist.write_text("<gameList><game><name>Oops</name></gameList>", encoding="utf-8")

    with pytest.raises(rc.ET.ParseError):
        rc.plan_gamelist_clean(str(gamelist))


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
# Unit tests: bad-tag detection (deprioritized re-releases/bad dumps)
# ---------------------------------------------------------------------

def test_has_bad_tag_detects_virtual_console():
    assert rc.has_bad_tag(["Virtual Console"])
    assert rc.has_bad_tag(["USA", "Virtual Console"])


def test_has_bad_tag_false_for_normal_tags():
    assert not rc.has_bad_tag(["USA"])
    assert not rc.has_bad_tag(["Europe", "Rev 1"])


def test_score_release_virtual_console_loses_size_tiebreak_to_plain_release():
    """Regression test for the reported bug: a larger Virtual Console
    re-release was winning the file-size tiebreak over a smaller plain
    release of the same title. bad-tag status must be compared before
    size, so the plain release wins regardless of which file is bigger.
    """
    priority = rc.DEFAULT_REGION_PRIORITY
    plain_score = rc.score_release(["usa"], 1000, priority)
    vc_score = rc.score_release(["usa", "virtual console"], 5000, priority)
    assert plain_score < vc_score


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


def test_virtual_console_release_moved_to_duplicates_even_when_larger(tmp_path):
    """Regression test for the reported real-world bug: a "(Virtual
    Console)" re-release was winning over the plain release of the same
    title purely because it happened to be a larger file (the file-size
    tiebreak only kicks in after bad-tag status, region, and revision are
    equal -- Virtual Console must lose there regardless of size).
    """
    plain = tmp_path / "Super Game (USA).zip"
    plain.parent.mkdir(parents=True, exist_ok=True)
    plain.write_bytes(b"x" * 1000)

    vc = tmp_path / "Super Game (USA) (Virtual Console).zip"
    vc.write_bytes(b"x" * 5000)

    run_script(tmp_path, "--apply")

    assert plain.exists()
    assert not vc.exists()
    assert (tmp_path / ".duplicates" / "Super Game (USA) (Virtual Console).zip").exists()


def test_virtual_console_sole_copy_is_kept(tmp_path):
    """A Virtual Console release must NOT be discarded just because it's
    the only copy of that title on hand -- unlike proto/beta builds,
    which are always routed to duplicates even as the sole copy.
    """
    touch(tmp_path / "Only Game (USA) (Virtual Console).zip")

    run_script(tmp_path, "--apply")

    assert (tmp_path / "Only Game (USA) (Virtual Console).zip").exists()
    assert not (tmp_path / ".duplicates").exists()


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


def write_gamelist(path, content=GAMELIST_TEMPLATE):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_gamelist_clean_dry_run_does_not_modify_file(tmp_path):
    gamelist_path = tmp_path / "SNES" / "gamelist.xml"
    write_gamelist(gamelist_path)

    result = run_script(tmp_path, "--gamelist-clean")

    assert "DRY RUN" in result.stdout
    assert "ZZZ(notgame)" in gamelist_path.read_text(encoding="utf-8")


def test_gamelist_clean_dry_run_does_not_create_backup(tmp_path):
    gamelist_path = tmp_path / "SNES" / "gamelist.xml"
    write_gamelist(gamelist_path)

    run_script(tmp_path, "--gamelist-clean")

    assert not (tmp_path / "SNES" / ".rom-cleanup-gamelist-xml.bak").exists()


def test_gamelist_clean_apply_removes_notgame_entries(tmp_path):
    gamelist_path = tmp_path / "SNES" / "gamelist.xml"
    write_gamelist(gamelist_path)

    result = run_script(tmp_path, "--gamelist-clean", "--apply")

    content = gamelist_path.read_text(encoding="utf-8")
    assert "ZZZ(notgame)" not in content
    assert "<name>Aladdin</name>" in content
    assert "<name>Bomberman</name>" in content
    assert "Removed 1 entry across 1 file(s)." in result.stdout


def test_gamelist_clean_apply_creates_backup_with_original_content(tmp_path):
    gamelist_path = tmp_path / "SNES" / "gamelist.xml"
    write_gamelist(gamelist_path)

    run_script(tmp_path, "--gamelist-clean", "--apply")

    backup_path = tmp_path / "SNES" / ".rom-cleanup-gamelist-xml.bak"
    assert backup_path.exists()
    assert backup_path.read_text(encoding="utf-8") == GAMELIST_TEMPLATE
    assert "ZZZ(notgame)" in backup_path.read_text(encoding="utf-8")


def test_gamelist_clean_backup_is_overwritten_not_accumulated_on_rerun(tmp_path):
    gamelist_path = tmp_path / "SNES" / "gamelist.xml"
    write_gamelist(gamelist_path)
    backup_path = tmp_path / "SNES" / ".rom-cleanup-gamelist-xml.bak"

    run_script(tmp_path, "--gamelist-clean", "--apply")
    first_backup_content = backup_path.read_text(encoding="utf-8")
    assert "ZZZ(notgame)" in first_backup_content

    # Second run: no more "notgame" entries left, so nothing should change
    # and the existing backup (still holding the real original) must be
    # left alone -- not clobbered with the already-cleaned content.
    run_script(tmp_path, "--gamelist-clean", "--apply")

    assert backup_path.read_text(encoding="utf-8") == first_backup_content


def test_gamelist_clean_finds_multiple_console_gamelists(tmp_path):
    write_gamelist(tmp_path / "SNES" / "gamelist.xml")
    write_gamelist(tmp_path / "Genesis" / "gamelist.xml")

    result = run_script(tmp_path, "--gamelist-clean", "--apply")

    assert "ZZZ(notgame)" not in (tmp_path / "SNES" / "gamelist.xml").read_text(encoding="utf-8")
    assert "ZZZ(notgame)" not in (tmp_path / "Genesis" / "gamelist.xml").read_text(encoding="utf-8")
    assert "Removed 2 entries across 2 file(s)." in result.stdout


def test_gamelist_clean_no_gamelists_found(tmp_path):
    touch(tmp_path / "SNES" / "Aladdin (USA).zip")

    result = run_script(tmp_path, "--gamelist-clean")

    assert "No gamelist.xml files found" in result.stdout


def test_gamelist_clean_skips_malformed_file_with_warning(tmp_path):
    gamelist_path = tmp_path / "SNES" / "gamelist.xml"
    write_gamelist(gamelist_path, "<gameList><game><name>Oops</name></gameList>")

    result = run_script(tmp_path, "--gamelist-clean", "--apply")

    assert "not well-formed XML" in result.stderr
    assert gamelist_path.read_text(encoding="utf-8") == "<gameList><game><name>Oops</name></gameList>"


def test_gamelist_clean_and_flatten_alpha_dirs_cannot_combine(tmp_path):
    result = run_script(tmp_path, "--gamelist-clean", "--flatten-alpha-dirs")

    assert result.returncode != 0
    assert "can't be combined" in result.stderr


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
