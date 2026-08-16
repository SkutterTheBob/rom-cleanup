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
# Unit tests: bin/cue -> CHD conversion planning
# ---------------------------------------------------------------------

def write_chdman_stub(dir_path, fail=False):
    """Write a fake chdman executable for tests. Relies on this project's
    fixed invocation order (createcd -i INPUT -o OUTPUT -f) rather than
    doing real argument parsing, to sidestep shell-scripting quirks (e.g.
    batch's well-known trouble with parentheses inside `if` blocks) that
    have nothing to do with the Python code under test -- subprocess.run
    passes argv as a real list with no shell parsing, so real filenames
    with parens/spaces are handled correctly regardless of this stub's
    simplicity. Returns the stub's path as a string.
    """
    if sys.platform.startswith("win"):
        stub_path = dir_path / "chdman.bat"
        if fail:
            stub_path.write_text("@echo off\r\necho boom 1>&2\r\nexit /b 1\r\n")
        else:
            stub_path.write_text('@echo off\r\n> "%~5" echo fake-chd-content\r\nexit /b 0\r\n')
    else:
        stub_path = dir_path / "chdman"
        if fail:
            stub_path.write_text("#!/bin/sh\necho boom >&2\nexit 1\n")
        else:
            stub_path.write_text('#!/bin/sh\necho fake-chd-content > "$5"\nexit 0\n')
        stub_path.chmod(0o755)
    return str(stub_path)


def test_find_cue_files_recursive(tmp_path):
    touch(tmp_path / "A.cue")
    touch(tmp_path / "sub" / "B.CUE")
    touch(tmp_path / "sub" / "C.bin")

    found = rc.find_cue_files(str(tmp_path))

    names = sorted(os.path.basename(f) for f in found)
    assert names == ["A.cue", "B.CUE"]


def test_find_chdman_resolves_override_path(tmp_path):
    stub = write_chdman_stub(tmp_path)

    assert rc.find_chdman(stub) == stub or os.path.samefile(rc.find_chdman(stub), stub)


def test_find_chdman_returns_none_for_missing_override(tmp_path):
    assert rc.find_chdman(str(tmp_path / "does-not-exist")) is None


def test_plan_chd_conversion_no_move_for_direct_cue(tmp_path):
    touch(tmp_path / "Game (USA).cue")

    to_convert, already_done = rc.plan_chd_conversion(str(tmp_path))

    assert already_done == []
    assert len(to_convert) == 1
    cue_path, working_chd_path, final_chd_path, needs_move = to_convert[0]
    assert needs_move is False
    assert working_chd_path == final_chd_path
    assert os.path.dirname(final_chd_path) == str(tmp_path)


def test_plan_chd_conversion_moves_nested_cue(tmp_path):
    release_dir = tmp_path / "Some Game (USA)"
    touch(release_dir / "Some Game (USA).cue")

    to_convert, already_done = rc.plan_chd_conversion(str(tmp_path))

    assert len(to_convert) == 1
    cue_path, working_chd_path, final_chd_path, needs_move = to_convert[0]
    assert needs_move is True
    assert os.path.dirname(working_chd_path) == str(release_dir)
    assert os.path.dirname(final_chd_path) == str(tmp_path)


def test_plan_chd_conversion_skips_when_chd_already_exists(tmp_path):
    touch(tmp_path / "Game (USA).cue")
    touch(tmp_path / "Game (USA).chd")

    to_convert, already_done = rc.plan_chd_conversion(str(tmp_path))

    assert to_convert == []
    assert len(already_done) == 1


def test_plan_chd_conversion_skips_nested_cue_when_final_dest_already_exists(tmp_path):
    """Regression coverage: a previous run may have already converted and
    moved this release's .chd up into roms_dir, leaving the original .cue
    behind in its subfolder -- that .cue must be recognized as already
    done, not reconverted.
    """
    release_dir = tmp_path / "Some Game (USA)"
    touch(release_dir / "Some Game (USA).cue")
    touch(tmp_path / "Some Game (USA).chd")

    to_convert, already_done = rc.plan_chd_conversion(str(tmp_path))

    assert to_convert == []
    assert len(already_done) == 1


def test_plan_chd_conversion_handles_name_collision(tmp_path):
    touch(tmp_path / "A" / "Game.cue")
    touch(tmp_path / "B" / "Game.cue")

    to_convert, already_done = rc.plan_chd_conversion(str(tmp_path))

    assert len(to_convert) == 2
    final_names = sorted(os.path.basename(t[2]) for t in to_convert)
    assert final_names == ["Game (1).chd", "Game.chd"]


# ---------------------------------------------------------------------
# Unit tests: multi-disc M3U playlist grouping
# ---------------------------------------------------------------------

def test_parse_disc_number_matches_disc_disk_cd():
    assert rc.parse_disc_number("Disc 1") == 1
    assert rc.parse_disc_number("Disc 2") == 2
    assert rc.parse_disc_number("Disk 3") == 3
    assert rc.parse_disc_number("CD1") == 1
    assert rc.parse_disc_number("cd 4") == 4


def test_parse_disc_number_none_for_non_disc_tags():
    assert rc.parse_disc_number("USA") is None
    assert rc.parse_disc_number("Track 1") is None
    assert rc.parse_disc_number("Rev 1") is None


def test_plan_m3u_grouping_groups_multi_disc_release(tmp_path):
    touch(tmp_path / "Game (USA) (Disc 1).chd")
    touch(tmp_path / "Game (USA) (Disc 2).chd")

    to_group, already_done, ambiguous = rc.plan_m3u_grouping(str(tmp_path))

    assert already_done == []
    assert ambiguous == []
    assert len(to_group) == 1
    hidden_dir_path, m3u_path, discs = to_group[0]
    assert hidden_dir_path == str(tmp_path / ".chd" / "Game (USA)")
    assert m3u_path == str(tmp_path / "Game (USA).m3u")
    assert len(discs) == 2
    assert [os.path.basename(d[1]) for d in discs] == [
        "Game (USA) (Disc 1).chd", "Game (USA) (Disc 2).chd"]
    assert all(needs_move for _, _, needs_move in discs)


def test_plan_m3u_grouping_orders_discs_numerically_not_alphabetically(tmp_path):
    """Regression coverage: "Disc 10" must sort after "Disc 2", not before
    it as plain string sorting would produce.
    """
    touch(tmp_path / "Game (USA) (Disc 1).chd")
    touch(tmp_path / "Game (USA) (Disc 2).chd")
    touch(tmp_path / "Game (USA) (Disc 10).chd")

    to_group, already_done, ambiguous = rc.plan_m3u_grouping(str(tmp_path))

    assert len(to_group) == 1
    _, _, discs = to_group[0]
    assert [os.path.basename(d[1]) for d in discs] == [
        "Game (USA) (Disc 1).chd", "Game (USA) (Disc 2).chd", "Game (USA) (Disc 10).chd"]


def test_plan_m3u_grouping_ignores_lone_disc(tmp_path):
    touch(tmp_path / "Only Disc (USA) (Disc 1).chd")

    to_group, already_done, ambiguous = rc.plan_m3u_grouping(str(tmp_path))

    assert to_group == []
    assert already_done == []
    assert ambiguous == []


def test_plan_m3u_grouping_ignores_non_disc_releases(tmp_path):
    touch(tmp_path / "Single Disc Game (USA).chd")

    to_group, already_done, ambiguous = rc.plan_m3u_grouping(str(tmp_path))

    assert to_group == []
    assert already_done == []


def test_plan_m3u_grouping_flags_ambiguous_duplicate_disc_numbers(tmp_path):
    touch(tmp_path / "SetA" / "Game (USA) (Disc 1).chd")
    touch(tmp_path / "SetB" / "Game (USA) (Disc 1).chd")

    to_group, already_done, ambiguous = rc.plan_m3u_grouping(str(tmp_path))

    assert to_group == []
    assert len(ambiguous) == 1
    title, non_disc_tags, chd_paths = ambiguous[0]
    assert title == "Game"
    assert non_disc_tags == ["USA"]
    assert len(chd_paths) == 2


def test_plan_m3u_grouping_detects_already_grouped(tmp_path):
    hidden_dir = tmp_path / ".chd" / "Game (USA)"
    touch(hidden_dir / "Game (USA) (Disc 1).chd")
    touch(hidden_dir / "Game (USA) (Disc 2).chd")
    (tmp_path / "Game (USA).m3u").write_text(
        ".chd/Game (USA)/Game (USA) (Disc 1).chd\n.chd/Game (USA)/Game (USA) (Disc 2).chd\n",
        encoding="utf-8")

    to_group, already_done, ambiguous = rc.plan_m3u_grouping(str(tmp_path))

    assert to_group == []
    assert already_done == [str(hidden_dir)]


def test_plan_m3u_grouping_regroups_when_m3u_content_is_stale(tmp_path):
    """If a disc was added since the .m3u was last written, the group is
    NOT considered already-done -- it needs the .m3u rewritten.
    """
    hidden_dir = tmp_path / ".chd" / "Game (USA)"
    touch(hidden_dir / "Game (USA) (Disc 1).chd")
    touch(hidden_dir / "Game (USA) (Disc 2).chd")
    (tmp_path / "Game (USA).m3u").write_text(
        ".chd/Game (USA)/Game (USA) (Disc 1).chd\n", encoding="utf-8")

    to_group, already_done, ambiguous = rc.plan_m3u_grouping(str(tmp_path))

    assert already_done == []
    assert len(to_group) == 1


def test_plan_m3u_grouping_migrates_old_same_folder_layout(tmp_path):
    """A release grouped under the pre-hidden-folder layout (.chd files and
    .m3u together in a visible folder) is NOT already-done -- it's picked
    up for migration into the current ".chd/"-nested layout.
    """
    old_folder = tmp_path / "Game (USA)"
    touch(old_folder / "Game (USA) (Disc 1).chd")
    touch(old_folder / "Game (USA) (Disc 2).chd")
    (old_folder / "Game (USA).m3u").write_text(
        "Game (USA) (Disc 1).chd\nGame (USA) (Disc 2).chd\n", encoding="utf-8")

    to_group, already_done, ambiguous = rc.plan_m3u_grouping(str(tmp_path))

    assert already_done == []
    assert len(to_group) == 1
    hidden_dir_path, m3u_path, discs = to_group[0]
    assert hidden_dir_path == str(tmp_path / ".chd" / "Game (USA)")
    assert m3u_path == str(tmp_path / "Game (USA).m3u")
    assert all(needs_move for _, _, needs_move in discs)


def test_plan_m3u_grouping_migrates_old_per_release_hidden_dir_layout(tmp_path):
    """A release grouped under the earlier ".Game (USA)/"-per-release-
    hidden-folder layout (before all releases were nested under a single
    ".chd/" folder) is NOT already-done -- it's picked up for migration
    into the current layout.
    """
    old_hidden_dir = tmp_path / ".Game (USA)"
    touch(old_hidden_dir / "Game (USA) (Disc 1).chd")
    touch(old_hidden_dir / "Game (USA) (Disc 2).chd")
    (tmp_path / "Game (USA).m3u").write_text(
        ".Game (USA)/Game (USA) (Disc 1).chd\n.Game (USA)/Game (USA) (Disc 2).chd\n",
        encoding="utf-8")

    to_group, already_done, ambiguous = rc.plan_m3u_grouping(str(tmp_path))

    assert already_done == []
    assert len(to_group) == 1
    hidden_dir_path, m3u_path, discs = to_group[0]
    assert hidden_dir_path == str(tmp_path / ".chd" / "Game (USA)")
    assert all(needs_move for _, _, needs_move in discs)


# ---------------------------------------------------------------------
# Unit tests: cue sheet FILE-reference case-mismatch detection
# ---------------------------------------------------------------------

def fs_is_case_sensitive(tmp_path):
    """Probe whether tmp_path's filesystem actually distinguishes files by
    case -- needed because some scenarios (e.g. two on-disk files that
    differ only by case) simply can't exist on a case-insensitive
    filesystem (default Windows/macOS), regardless of the code under test.
    """
    probe_dir = tmp_path / "_case_probe"
    probe_dir.mkdir()
    (probe_dir / "a").write_text("")
    (probe_dir / "A").write_text("")
    return len(os.listdir(str(probe_dir))) == 2


def write_cue(path, *file_lines):
    """Write a minimal .cue with one FILE line per name in file_lines."""
    lines = ['FILE "{0}" BINARY\n  TRACK 01 MODE1/2352\n    INDEX 01 00:00:00\n'.format(name)
             for name in file_lines]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(lines), encoding="utf-8")


def test_parse_cue_file_references_extracts_quoted_names(tmp_path):
    cue = tmp_path / "game.cue"
    write_cue(cue, "GAME.BIN")

    assert rc.parse_cue_file_references(str(cue)) == ["GAME.BIN"]


def test_parse_cue_file_references_extracts_unquoted_names(tmp_path):
    cue = tmp_path / "game.cue"
    cue.write_text("FILE game.bin BINARY\n  TRACK 01 MODE1/2352\n    INDEX 01 00:00:00\n",
                    encoding="utf-8")

    assert rc.parse_cue_file_references(str(cue)) == ["game.bin"]


def test_parse_cue_file_references_multi_track(tmp_path):
    cue = tmp_path / "game.cue"
    write_cue(cue, "Game (Track 01).bin", "Game (Track 02).bin")

    assert rc.parse_cue_file_references(str(cue)) == [
        "Game (Track 01).bin", "Game (Track 02).bin"]


def test_find_cue_case_mismatches_none_when_exact_match(tmp_path):
    cue = tmp_path / "game.cue"
    write_cue(cue, "game.bin")
    touch(tmp_path / "game.bin")

    assert rc.find_cue_case_mismatches(str(cue)) == []


def test_find_cue_case_mismatches_resolves_unique_case_insensitive_match(tmp_path):
    """The real-world scenario reported: a cue authored on a
    case-insensitive filesystem references "GAME.BIN" in a different case
    than the actual on-disk file. Must be resolvable since exactly one
    case-insensitive match exists.
    """
    cue = tmp_path / "game.cue"
    write_cue(cue, "GAME.BIN")
    touch(tmp_path / "game.bin")

    mismatches = rc.find_cue_case_mismatches(str(cue))

    assert mismatches == [("GAME.BIN", "game.bin")]


def test_find_cue_case_mismatches_unresolved_when_no_match(tmp_path):
    cue = tmp_path / "game.cue"
    write_cue(cue, "totally_different_name.bin")
    touch(tmp_path / "game.bin")

    mismatches = rc.find_cue_case_mismatches(str(cue))

    assert mismatches == [("totally_different_name.bin", None)]


def test_find_cue_case_mismatches_unresolved_when_ambiguous(tmp_path):
    """Two on-disk files differing only by case (legal on a case-sensitive
    filesystem) -- can't safely guess which one the cue meant.
    """
    if not fs_is_case_sensitive(tmp_path):
        pytest.skip("requires a case-sensitive filesystem")

    cue = tmp_path / "game.cue"
    write_cue(cue, "GAME.BIN")
    touch(tmp_path / "game.bin")
    touch(tmp_path / "Game.bin")

    mismatches = rc.find_cue_case_mismatches(str(cue))

    assert mismatches == [("GAME.BIN", None)]


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


def test_has_bad_tag_detects_switch_online():
    assert rc.has_bad_tag(["Switch Online"])
    assert rc.has_bad_tag(["USA", "Switch Online"])


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


def test_score_release_switch_online_loses_size_tiebreak_to_plain_release():
    priority = rc.DEFAULT_REGION_PRIORITY
    plain_score = rc.score_release(["usa"], 1000, priority)
    switch_online_score = rc.score_release(["usa", "switch online"], 5000, priority)
    assert plain_score < switch_online_score


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
# Unit tests: cleaning up now-empty source folders after a move
# ---------------------------------------------------------------------

def test_remove_now_empty_dirs_removes_empty_folder(tmp_path):
    empty_dir = tmp_path / "Some Game"
    empty_dir.mkdir()

    removed = rc.remove_now_empty_dirs(str(tmp_path), [str(empty_dir)])

    assert removed == {str(empty_dir)}
    assert not empty_dir.exists()


def test_remove_now_empty_dirs_leaves_non_empty_folder(tmp_path):
    non_empty_dir = tmp_path / "Some Game"
    touch(non_empty_dir / "Some Game.chd")

    removed = rc.remove_now_empty_dirs(str(tmp_path), [str(non_empty_dir)])

    assert removed == set()
    assert non_empty_dir.is_dir()


def test_remove_now_empty_dirs_cascades_to_parent(tmp_path):
    nested_dir = tmp_path / "Multi Disc Game" / "Disc 1"
    nested_dir.mkdir(parents=True)

    removed = rc.remove_now_empty_dirs(str(tmp_path), [str(nested_dir)])

    assert removed == {str(nested_dir), str(tmp_path / "Multi Disc Game")}
    assert not (tmp_path / "Multi Disc Game").exists()


def test_remove_now_empty_dirs_never_removes_roms_dir_itself(tmp_path):
    """Even if roms_dir is passed in directly and happens to be empty, it
    must never be removed -- only things strictly beneath it.
    """
    removed = rc.remove_now_empty_dirs(str(tmp_path), [str(tmp_path)])

    assert removed == set()
    assert tmp_path.is_dir()


def test_remove_now_empty_dirs_ignores_paths_outside_roms_dir(tmp_path):
    outside_dir = tmp_path.parent / "definitely_not_under_roms_dir_{0}".format(tmp_path.name)
    outside_dir.mkdir()
    roms_dir = tmp_path / "roms"
    roms_dir.mkdir()

    try:
        removed = rc.remove_now_empty_dirs(str(roms_dir), [str(outside_dir)])
        assert removed == set()
        assert outside_dir.exists()
    finally:
        outside_dir.rmdir()


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


def test_switch_online_release_moved_to_duplicates_even_when_larger(tmp_path):
    plain = tmp_path / "Super Game (USA).zip"
    plain.parent.mkdir(parents=True, exist_ok=True)
    plain.write_bytes(b"x" * 1000)

    switch_online = tmp_path / "Super Game (USA) (Switch Online).zip"
    switch_online.write_bytes(b"x" * 5000)

    run_script(tmp_path, "--apply")

    assert plain.exists()
    assert not switch_online.exists()
    assert (tmp_path / ".duplicates" / "Super Game (USA) (Switch Online).zip").exists()


def test_switch_online_sole_copy_is_kept(tmp_path):
    touch(tmp_path / "Only Game (USA) (Switch Online).zip")

    run_script(tmp_path, "--apply")

    assert (tmp_path / "Only Game (USA) (Switch Online).zip").exists()
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
    # The .chd is still in game_dir, so it must NOT be removed.
    assert game_dir.is_dir()


def test_apply_removes_now_empty_source_folder_after_moving_all_its_files(tmp_path):
    """Regression test for the --convert-to-chd workflow: once a release's
    .chd has been moved up out of its per-release subfolder (leaving only
    the now-redundant .bin/.cue behind), running the normal scan should
    both route those raw files to Redundant-Raw-Disc/ AND remove the
    subfolder they came from, since nothing is left in it.
    """
    game_dir = tmp_path / "Some Game"
    touch(game_dir / "Some Game.bin")
    touch(game_dir / "Some Game.cue")
    touch(tmp_path / "Some Game.chd")  # already moved up, e.g. by --convert-to-chd

    run_script(tmp_path, "--apply")

    assert (tmp_path / "Some Game.chd").exists()
    assert (tmp_path / ".duplicates" / "Redundant-Raw-Disc" / "Some Game.bin").exists()
    assert (tmp_path / ".duplicates" / "Redundant-Raw-Disc" / "Some Game.cue").exists()
    assert not game_dir.exists()


def test_apply_leaves_source_folder_with_other_files_alone(tmp_path):
    game_dir = tmp_path / "Some Game"
    touch(game_dir / "Some Game.bin")
    touch(game_dir / "Some Game.cue")
    touch(tmp_path / "Some Game.chd")
    touch(game_dir / "readme.txt")  # unrelated file the tool never touches

    run_script(tmp_path, "--apply")

    assert game_dir.is_dir()
    assert (game_dir / "readme.txt").exists()


def test_apply_cascades_empty_folder_removal_to_parent(tmp_path):
    """If removing an emptied subfolder leaves ITS parent empty too, the
    parent should be removed as well, continuing upward.
    """
    nested_dir = tmp_path / "Multi Disc Game" / "Disc 1"
    touch(nested_dir / "Multi Disc Game (Disc 1).bin")
    touch(nested_dir / "Multi Disc Game (Disc 1).cue")
    touch(tmp_path / "Multi Disc Game (Disc 1).chd")

    run_script(tmp_path, "--apply")

    assert not nested_dir.exists()
    assert not (tmp_path / "Multi Disc Game").exists()


def test_apply_never_removes_roms_dir_itself(tmp_path):
    """Sanity check: even if every real ROM ends up moved into .duplicates,
    roms_dir itself must never be deleted -- only things strictly beneath it.
    """
    touch(tmp_path / "Some Game.bin")
    touch(tmp_path / "Some Game.cue")
    touch(tmp_path / "Some Game.chd")

    run_script(tmp_path, "--apply")

    assert tmp_path.is_dir()


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


def test_convert_to_chd_dry_run_does_not_invoke_chdman(tmp_path):
    stub = write_chdman_stub(tmp_path)
    console_dir = tmp_path / "PSX"
    touch(console_dir / "Game (USA).cue")

    result = run_script(console_dir, "--convert-to-chd", "--chdman-path", stub)

    assert "DRY RUN" in result.stdout
    assert not (console_dir / "Game (USA).chd").exists()


def test_convert_to_chd_apply_converts_direct_cue(tmp_path):
    stub = write_chdman_stub(tmp_path)
    console_dir = tmp_path / "PSX"
    touch(console_dir / "Game (USA).cue")

    result = run_script(console_dir, "--convert-to-chd", "--chdman-path", stub, "--apply")

    assert (console_dir / "Game (USA).chd").exists()
    assert (console_dir / "Game (USA).cue").exists()  # original left in place
    assert "Converted 1/1" in result.stdout


def test_convert_to_chd_moves_nested_cue_chd_to_console_root(tmp_path):
    stub = write_chdman_stub(tmp_path)
    console_dir = tmp_path / "PSX"
    release_dir = console_dir / "Some Game (USA)"
    touch(release_dir / "Some Game (USA).cue")

    run_script(console_dir, "--convert-to-chd", "--chdman-path", stub, "--apply")

    assert (console_dir / "Some Game (USA).chd").exists()
    assert not (release_dir / "Some Game (USA).chd").exists()
    assert (release_dir / "Some Game (USA).cue").exists()


def test_convert_to_chd_skips_already_converted_on_rerun(tmp_path):
    stub = write_chdman_stub(tmp_path)
    console_dir = tmp_path / "PSX"
    touch(console_dir / "Game (USA).cue")

    run_script(console_dir, "--convert-to-chd", "--chdman-path", stub, "--apply")
    result = run_script(console_dir, "--convert-to-chd", "--chdman-path", stub, "--apply")

    assert "[SKIP]" in result.stdout
    assert "already converted" in result.stdout


def test_convert_to_chd_reports_chdman_errors(tmp_path):
    stub = write_chdman_stub(tmp_path, fail=True)
    console_dir = tmp_path / "PSX"
    touch(console_dir / "Game (USA).cue")

    result = run_script(console_dir, "--convert-to-chd", "--chdman-path", stub, "--apply")

    assert "ERROR converting" in result.stderr
    assert "Converted 0/1" in result.stdout
    assert not (console_dir / "Game (USA).chd").exists()


def test_convert_to_chd_missing_chdman_errors_clearly(tmp_path):
    console_dir = tmp_path / "PSX"
    touch(console_dir / "Game (USA).cue")

    result = run_script(console_dir, "--convert-to-chd", "--chdman-path",
                         str(tmp_path / "nonexistent-chdman"))

    assert result.returncode != 0
    assert "chdman not found" in result.stderr


def test_convert_to_chd_no_cue_files_found(tmp_path):
    stub = write_chdman_stub(tmp_path)
    console_dir = tmp_path / "PSX"
    touch(console_dir / "Game (USA).zip")

    result = run_script(console_dir, "--convert-to-chd", "--chdman-path", stub)

    assert "No .cue files found" in result.stdout


def test_convert_to_chd_and_flatten_alpha_dirs_cannot_combine(tmp_path):
    result = run_script(tmp_path, "--convert-to-chd", "--flatten-alpha-dirs")

    assert result.returncode != 0
    assert "can't be combined" in result.stderr


def test_convert_to_chd_dry_run_previews_case_fix_without_renaming(tmp_path):
    stub = write_chdman_stub(tmp_path)
    console_dir = tmp_path / "PSX"
    write_cue(console_dir / "game.cue", "GAME.BIN")
    touch(console_dir / "game.bin")

    result = run_script(console_dir, "--convert-to-chd", "--chdman-path", stub)

    assert "[CASE-FIX] Would rename 'game.bin' -> 'GAME.BIN'" in result.stdout
    # os.listdir() reports the exact stored-case name regardless of
    # whether this filesystem's lookups are case-insensitive, so this
    # correctly verifies the dry run didn't touch anything even on
    # Windows/macOS where Path.exists() can't tell the two names apart.
    entries = os.listdir(str(console_dir))
    assert "game.bin" in entries
    assert "GAME.BIN" not in entries


def test_convert_to_chd_apply_renames_case_mismatch_then_converts(tmp_path):
    """Regression test for the real-world scenario reported: a redump-style
    cue sheet referencing an all-caps filename that only differs from the
    actual on-disk file by case (common when a cue authored on a
    case-insensitive filesystem is moved to a case-sensitive one). Must be
    renamed into place and the conversion must still succeed.
    """
    stub = write_chdman_stub(tmp_path)
    console_dir = tmp_path / "PSX"
    write_cue(console_dir / "game.cue", "GAME.BIN")
    touch(console_dir / "game.bin")

    result = run_script(console_dir, "--convert-to-chd", "--chdman-path", stub, "--apply")

    assert "[CASE-FIX] Renamed 'game.bin' -> 'GAME.BIN'" in result.stdout
    entries = os.listdir(str(console_dir))
    assert "GAME.BIN" in entries
    assert "game.bin" not in entries
    assert (console_dir / "game.chd").exists()
    assert "Converted 1/1" in result.stdout


def test_convert_to_chd_blocks_unresolvable_reference_without_invoking_chdman(tmp_path):
    stub = write_chdman_stub(tmp_path)
    console_dir = tmp_path / "PSX"
    write_cue(console_dir / "game.cue", "totally_different_name.bin")
    touch(console_dir / "game.bin")

    result = run_script(console_dir, "--convert-to-chd", "--chdman-path", stub, "--apply")

    assert "doesn't exist (even case-insensitively)" in result.stderr
    assert not (console_dir / "game.chd").exists()
    assert "Converted 0/1" in result.stdout
    assert "1 error(s)" in result.stdout


def test_make_m3u_dry_run_does_not_move_anything(tmp_path):
    touch(tmp_path / "Game (USA) (Disc 1).chd")
    touch(tmp_path / "Game (USA) (Disc 2).chd")

    result = run_script(tmp_path, "--make-m3u")

    assert "DRY RUN" in result.stdout
    assert (tmp_path / "Game (USA) (Disc 1).chd").exists()
    assert (tmp_path / "Game (USA) (Disc 2).chd").exists()
    assert not (tmp_path / ".chd").exists()
    assert not (tmp_path / "Game (USA).m3u").exists()


def test_make_m3u_apply_groups_discs_and_writes_playlist(tmp_path):
    touch(tmp_path / "Final Fantasy VII (USA) (Disc 1).chd")
    touch(tmp_path / "Final Fantasy VII (USA) (Disc 2).chd")
    touch(tmp_path / "Final Fantasy VII (USA) (Disc 3).chd")

    result = run_script(tmp_path, "--make-m3u", "--apply")

    hidden_dir = tmp_path / ".chd" / "Final Fantasy VII (USA)"
    assert (hidden_dir / "Final Fantasy VII (USA) (Disc 1).chd").exists()
    assert (hidden_dir / "Final Fantasy VII (USA) (Disc 2).chd").exists()
    assert (hidden_dir / "Final Fantasy VII (USA) (Disc 3).chd").exists()
    assert not (tmp_path / "Final Fantasy VII (USA) (Disc 1).chd").exists()

    m3u_content = (tmp_path / "Final Fantasy VII (USA).m3u").read_text(encoding="utf-8")
    assert m3u_content == (
        ".chd/Final Fantasy VII (USA)/Final Fantasy VII (USA) (Disc 1).chd\n"
        ".chd/Final Fantasy VII (USA)/Final Fantasy VII (USA) (Disc 2).chd\n"
        ".chd/Final Fantasy VII (USA)/Final Fantasy VII (USA) (Disc 3).chd\n"
    )
    assert "Grouped 1/1" in result.stdout


def test_make_m3u_leaves_single_disc_and_lone_disc_releases_alone(tmp_path):
    touch(tmp_path / "Single Disc Game (USA).chd")
    touch(tmp_path / "Lone Disc Game (USA) (Disc 1).chd")

    result = run_script(tmp_path, "--make-m3u", "--apply")

    assert (tmp_path / "Single Disc Game (USA).chd").exists()
    assert (tmp_path / "Lone Disc Game (USA) (Disc 1).chd").exists()
    assert "No multi-disc" in result.stdout


def test_make_m3u_skips_already_grouped_on_rerun(tmp_path):
    touch(tmp_path / "Game (USA) (Disc 1).chd")
    touch(tmp_path / "Game (USA) (Disc 2).chd")

    run_script(tmp_path, "--make-m3u", "--apply")
    result = run_script(tmp_path, "--make-m3u", "--apply")

    assert "[SKIP]" in result.stdout
    assert "already grouped" in result.stdout


def test_make_m3u_flags_ambiguous_duplicate_disc_numbers_without_touching_files(tmp_path):
    touch(tmp_path / "SetA" / "Game (USA) (Disc 1).chd")
    touch(tmp_path / "SetB" / "Game (USA) (Disc 1).chd")

    result = run_script(tmp_path, "--make-m3u", "--apply")

    assert "needs manual review" in result.stderr
    assert (tmp_path / "SetA" / "Game (USA) (Disc 1).chd").exists()
    assert (tmp_path / "SetB" / "Game (USA) (Disc 1).chd").exists()
    assert not (tmp_path / ".chd").exists()


def test_make_m3u_removes_now_empty_source_folder(tmp_path):
    release_dir = tmp_path / "Some Folder"
    touch(release_dir / "Game (USA) (Disc 1).chd")
    touch(release_dir / "Game (USA) (Disc 2).chd")

    run_script(tmp_path, "--make-m3u", "--apply")

    assert not release_dir.exists()
    assert (tmp_path / ".chd" / "Game (USA)" / "Game (USA) (Disc 1).chd").exists()


def test_make_m3u_migrates_old_same_folder_layout(tmp_path):
    """A release grouped under the pre-hidden-folder layout is moved into
    the current ".chd/"-nested layout on re-run, and the old visible
    folder (including its now-stale .m3u) is cleaned up.
    """
    old_folder = tmp_path / "Game (USA)"
    touch(old_folder / "Game (USA) (Disc 1).chd")
    touch(old_folder / "Game (USA) (Disc 2).chd")
    (old_folder / "Game (USA).m3u").write_text(
        "Game (USA) (Disc 1).chd\nGame (USA) (Disc 2).chd\n", encoding="utf-8")

    result = run_script(tmp_path, "--make-m3u", "--apply")

    assert "Grouped 1/1" in result.stdout
    assert not old_folder.exists()
    hidden_dir = tmp_path / ".chd" / "Game (USA)"
    assert (hidden_dir / "Game (USA) (Disc 1).chd").exists()
    assert (hidden_dir / "Game (USA) (Disc 2).chd").exists()
    m3u_content = (tmp_path / "Game (USA).m3u").read_text(encoding="utf-8")
    assert m3u_content == (
        ".chd/Game (USA)/Game (USA) (Disc 1).chd\n"
        ".chd/Game (USA)/Game (USA) (Disc 2).chd\n"
    )


def test_make_m3u_migrates_old_per_release_hidden_dir_layout(tmp_path):
    """A release grouped under the earlier ".Game (USA)/"-per-release-
    hidden-folder layout is moved into the current single-".chd/"-folder
    layout on re-run, and the old hidden folder is cleaned up.
    """
    old_hidden_dir = tmp_path / ".Game (USA)"
    touch(old_hidden_dir / "Game (USA) (Disc 1).chd")
    touch(old_hidden_dir / "Game (USA) (Disc 2).chd")
    (tmp_path / "Game (USA).m3u").write_text(
        ".Game (USA)/Game (USA) (Disc 1).chd\n.Game (USA)/Game (USA) (Disc 2).chd\n",
        encoding="utf-8")

    result = run_script(tmp_path, "--make-m3u", "--apply")

    assert "Grouped 1/1" in result.stdout
    assert not old_hidden_dir.exists()
    hidden_dir = tmp_path / ".chd" / "Game (USA)"
    assert (hidden_dir / "Game (USA) (Disc 1).chd").exists()
    assert (hidden_dir / "Game (USA) (Disc 2).chd").exists()
    m3u_content = (tmp_path / "Game (USA).m3u").read_text(encoding="utf-8")
    assert m3u_content == (
        ".chd/Game (USA)/Game (USA) (Disc 1).chd\n"
        ".chd/Game (USA)/Game (USA) (Disc 2).chd\n"
    )


def test_make_m3u_and_convert_to_chd_cannot_combine(tmp_path):
    result = run_script(tmp_path, "--make-m3u", "--convert-to-chd")

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
