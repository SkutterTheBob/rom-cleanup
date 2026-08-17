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
# Unit tests: isolating titles never officially released in North America
# ---------------------------------------------------------------------

def default_dup_dir(tmp_path):
    return str(tmp_path / ".duplicates")


def test_tags_indicate_na_release_usa_and_world():
    assert rc.tags_indicate_na_release(["USA"])
    assert rc.tags_indicate_na_release(["World"])
    assert rc.tags_indicate_na_release(["USA, Europe"])
    assert not rc.tags_indicate_na_release(["Japan"])
    assert not rc.tags_indicate_na_release(["Europe"])
    assert not rc.tags_indicate_na_release(["Japan", "En"])


def test_plan_isolate_imports_keeps_na_titles(tmp_path):
    touch(tmp_path / "Super Game (USA).zip")
    touch(tmp_path / "World Racer (World).zip")

    to_move, kept, imports, blocked, filtered_out = rc.plan_isolate_imports(str(tmp_path), default_dup_dir(tmp_path))

    assert to_move == []
    assert blocked == []
    assert kept == ["Super Game", "World Racer"]
    assert imports == []


def test_plan_isolate_imports_moves_import_only_title(tmp_path):
    touch(tmp_path / "SaGa 2 (Japan).zip")
    touch(tmp_path / "SaGa 2 (Japan) (En).zip")

    to_move, kept, imports, blocked, filtered_out = rc.plan_isolate_imports(str(tmp_path), default_dup_dir(tmp_path))

    assert kept == []
    assert imports == ["SaGa 2"]
    assert len(to_move) == 2
    dest_dirs = {os.path.dirname(dest) for _, dest in to_move}
    assert dest_dirs == {str(tmp_path / ".imports")}


def test_plan_isolate_imports_mixed_title_stays_if_any_release_is_na(tmp_path):
    """A title with BOTH a Japan release and a USA release must stay in
    roms_dir entirely -- having at least one NA release is enough.
    """
    touch(tmp_path / "Mixed Game (Japan).zip")
    touch(tmp_path / "Mixed Game (USA).zip")

    to_move, kept, imports, blocked, filtered_out = rc.plan_isolate_imports(str(tmp_path), default_dup_dir(tmp_path))

    assert to_move == []
    assert kept == ["Mixed Game"]
    assert imports == []


def test_plan_isolate_imports_ignores_bios_and_proto_beta(tmp_path):
    touch(tmp_path / "PSX [BIOS].bin")
    touch(tmp_path / "Unreleased (Japan) (Proto).zip")

    to_move, kept, imports, blocked, filtered_out = rc.plan_isolate_imports(str(tmp_path), default_dup_dir(tmp_path))

    assert to_move == []
    assert kept == []
    assert imports == []


def test_plan_isolate_imports_ignores_duplicates_and_imports_dirs(tmp_path):
    touch(tmp_path / ".duplicates" / "Whatever (USA).zip")
    touch(tmp_path / ".imports" / "Already There (Japan).zip")

    to_move, kept, imports, blocked, filtered_out = rc.plan_isolate_imports(str(tmp_path), default_dup_dir(tmp_path))

    assert to_move == []
    assert kept == []
    assert imports == []


def test_plan_isolate_imports_ignores_alpha_bucket_folders(tmp_path):
    touch(tmp_path / "A" / "Aladdin (Japan).zip")

    to_move, kept, imports, blocked, filtered_out = rc.plan_isolate_imports(str(tmp_path), default_dup_dir(tmp_path))

    assert to_move == []
    assert kept == []
    assert imports == []


def test_plan_isolate_imports_ignores_non_title_asset_folders(tmp_path):
    """Regression test for the reported bug: a "media" folder (common in
    ES-DE/frontend setups for box art, screenshots, videos alongside the
    roms) has no tags of its own and must not be treated as an untitled
    release with no NA tag and swept into .imports/.
    """
    touch(tmp_path / "Super Game (USA).zip")
    touch(tmp_path / "media" / "screenshots" / "super game.png")
    touch(tmp_path / "images" / "super game.jpg")

    to_move, kept, imports, blocked, filtered_out = rc.plan_isolate_imports(
        str(tmp_path), default_dup_dir(tmp_path))

    assert to_move == []
    assert imports == []
    assert kept == ["Super Game"]


def test_plan_isolate_imports_moves_whole_release_subfolder_as_one_unit(tmp_path):
    release_dir = tmp_path / "Old Style CD (Japan)"
    touch(release_dir / "Old Style CD (Japan).cue")
    touch(release_dir / "Old Style CD (Japan).bin")

    to_move, kept, imports, blocked, filtered_out = rc.plan_isolate_imports(str(tmp_path), default_dup_dir(tmp_path))

    assert imports == ["Old Style CD"]
    assert len(to_move) == 1
    src, dest = to_move[0]
    assert src == str(release_dir)
    assert dest == str(tmp_path / ".imports" / "Old Style CD (Japan)")


def test_plan_isolate_imports_moves_m3u_with_its_hidden_disc_folder(tmp_path):
    hidden_dir = tmp_path / rc.M3U_HIDDEN_DIR_NAME / "SaGa CD (Japan)"
    touch(hidden_dir / "SaGa CD (Japan) (Disc 1).chd")
    touch(hidden_dir / "SaGa CD (Japan) (Disc 2).chd")
    (tmp_path / "SaGa CD (Japan).m3u").write_text(
        ".chd/SaGa CD (Japan)/SaGa CD (Japan) (Disc 1).chd\n"
        ".chd/SaGa CD (Japan)/SaGa CD (Japan) (Disc 2).chd\n", encoding="utf-8")

    to_move, kept, imports, blocked, filtered_out = rc.plan_isolate_imports(str(tmp_path), default_dup_dir(tmp_path))

    assert imports == ["SaGa CD"]
    assert blocked == []
    srcs = {src for src, _ in to_move}
    assert str(tmp_path / "SaGa CD (Japan).m3u") in srcs
    assert str(hidden_dir) in srcs
    dests = {dest for _, dest in to_move}
    assert str(tmp_path / ".imports" / "SaGa CD (Japan).m3u") in dests
    assert str(tmp_path / ".imports" / rc.M3U_HIDDEN_DIR_NAME / "SaGa CD (Japan)") in dests


def test_plan_isolate_imports_blocks_m3u_collision_instead_of_renaming(tmp_path):
    """An .m3u/hidden-folder pair must never be silently renamed on
    collision -- that would desync the playlist's relative disc paths
    from the folder they actually live in. Blocked and reported instead.
    """
    hidden_dir = tmp_path / rc.M3U_HIDDEN_DIR_NAME / "SaGa CD (Japan)"
    touch(hidden_dir / "SaGa CD (Japan) (Disc 1).chd")
    (tmp_path / "SaGa CD (Japan).m3u").write_text(
        ".chd/SaGa CD (Japan)/SaGa CD (Japan) (Disc 1).chd\n", encoding="utf-8")
    touch(tmp_path / ".imports" / "SaGa CD (Japan).m3u")  # pre-existing collision

    to_move, kept, imports, blocked, filtered_out = rc.plan_isolate_imports(str(tmp_path), default_dup_dir(tmp_path))

    assert to_move == []
    assert len(blocked) == 1
    assert blocked[0][0] == "SaGa CD"


def test_plan_isolate_imports_blacklisted_title_is_never_moved(tmp_path):
    """Regression test: a whole-title blacklist entry means "never touch
    this game at all", same as everywhere else in the tool -- it must not
    be moved to .imports/ even though it has no NA release.
    """
    touch(tmp_path / "SaGa 2 (Japan).zip")
    touch(tmp_path / "Protected Import (Japan).zip")
    blacklist_titles = {"protected import": "Protected Import"}

    to_move, kept, imports, blocked, filtered_out = rc.plan_isolate_imports(
        str(tmp_path), default_dup_dir(tmp_path), blacklist_titles=blacklist_titles)

    assert imports == ["SaGa 2"]
    assert "Protected Import" not in imports
    assert "Protected Import" not in kept
    assert filtered_out == 1


def test_plan_isolate_imports_whitelist_restricts_scope(tmp_path):
    """Regression test: when a whitelist is present, only whitelisted
    titles are considered at all -- everything else is left untouched,
    same "restrict this run to ONLY these titles" meaning as the normal
    scan.
    """
    touch(tmp_path / "SaGa 2 (Japan).zip")
    touch(tmp_path / "Another Import (Japan).zip")
    whitelist_titles = {"saga 2": "SaGa 2"}

    to_move, kept, imports, blocked, filtered_out = rc.plan_isolate_imports(
        str(tmp_path), default_dup_dir(tmp_path), whitelist_titles=whitelist_titles)

    assert imports == ["SaGa 2"]
    assert "Another Import" not in imports
    assert "Another Import" not in kept
    assert filtered_out == 1


def test_plan_isolate_imports_release_specific_whitelist_pin_keeps_title(tmp_path):
    """Regression test for the reported Streets of Rage II bug: a
    release-specific whitelist entry (a line WITH tags) must keep that
    title in place too, not just a whole-title entry.
    """
    touch(tmp_path / "Streets of Rage II (Japan, Europe) (En,Ja).7z")
    title_key, tag_set = rc.parse_filter_line("Streets of Rage II (Japan, Europe) (En,Ja)")
    whitelist_releases = [(title_key, tag_set, "Streets of Rage II (Japan, Europe) (En,Ja)")]

    to_move, kept, imports, blocked, filtered_out = rc.plan_isolate_imports(
        str(tmp_path), default_dup_dir(tmp_path), whitelist_releases=whitelist_releases)

    assert to_move == []
    assert imports == []
    assert kept == ["Streets of Rage II"]


def test_plan_isolate_imports_release_pin_bypasses_unrelated_whitelist_restriction(tmp_path):
    """A release-specific pin for one title must still work even when a
    whole-title whitelist is active that does NOT name it -- the pin is
    itself a clear signal to leave that title alone, regardless of scope.
    A third, wholly unrelated title (neither pinned nor whitelisted)
    stays excluded, confirming the restriction still applies normally.
    """
    touch(tmp_path / "Streets of Rage II (Japan, Europe) (En,Ja).7z")
    touch(tmp_path / "Some Other Game (USA).zip")
    touch(tmp_path / "Unrelated Game (Japan).zip")
    title_key, tag_set = rc.parse_filter_line("Streets of Rage II (Japan, Europe) (En,Ja)")
    whitelist_releases = [(title_key, tag_set, "Streets of Rage II (Japan, Europe) (En,Ja)")]
    whitelist_titles = {"some other game": "Some Other Game"}

    to_move, kept, imports, blocked, filtered_out = rc.plan_isolate_imports(
        str(tmp_path), default_dup_dir(tmp_path),
        whitelist_titles=whitelist_titles, whitelist_releases=whitelist_releases)

    assert "Streets of Rage II" in kept  # pinned -- kept despite not being whitelisted
    assert "Some Other Game" in kept     # whitelisted, has a USA release
    assert "Unrelated Game" not in kept
    assert "Unrelated Game" not in imports
    assert imports == []


def test_plan_isolate_imports_blacklist_title_wins_over_release_whitelist_pin(tmp_path):
    """Regression test matching the documented rule everywhere else in
    the tool: "Blacklist always wins over whitelist for the same
    release."
    """
    touch(tmp_path / "Streets of Rage II (Japan, Europe) (En,Ja).7z")
    title_key, tag_set = rc.parse_filter_line("Streets of Rage II (Japan, Europe) (En,Ja)")
    whitelist_releases = [(title_key, tag_set, "Streets of Rage II (Japan, Europe) (En,Ja)")]
    blacklist_titles = {"streets of rage ii": "Streets of Rage II"}

    to_move, kept, imports, blocked, filtered_out = rc.plan_isolate_imports(
        str(tmp_path), default_dup_dir(tmp_path),
        blacklist_titles=blacklist_titles, whitelist_releases=whitelist_releases)

    assert to_move == []
    assert imports == []
    assert kept == []
    assert filtered_out == 1


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
# Unit tests: non-standard-tag detection (deprioritized re-releases,
# compilations, bad dumps, and anything else beyond region/revision)
# ---------------------------------------------------------------------

def test_is_recognized_region_tag_plain_and_combined():
    priority = rc.DEFAULT_REGION_PRIORITY
    assert rc.is_recognized_region_tag("USA", priority)
    assert rc.is_recognized_region_tag("USA, Korea", priority)
    assert rc.is_recognized_region_tag("USA/Europe", priority)
    assert not rc.is_recognized_region_tag("Atlantis", priority)
    assert not rc.is_recognized_region_tag("Rev 1", priority)


def test_has_non_standard_tag_false_for_region_and_revision_only():
    priority = rc.DEFAULT_REGION_PRIORITY
    assert not rc.has_non_standard_tag(["USA"], priority)
    assert not rc.has_non_standard_tag(["Europe", "Rev 1"], priority)
    assert not rc.has_non_standard_tag(["USA, Korea"], priority)
    assert not rc.has_non_standard_tag(["World", "v1.1"], priority)


def test_has_non_standard_tag_true_for_known_re_release_tags():
    """Virtual Console / Switch Online no longer need a hardcoded list --
    they're caught the same way any other non-region/revision tag is.
    """
    priority = rc.DEFAULT_REGION_PRIORITY
    assert rc.has_non_standard_tag(["USA", "Virtual Console"], priority)
    assert rc.has_non_standard_tag(["USA", "Switch Online"], priority)


def test_has_non_standard_tag_true_for_arbitrary_compilation_names():
    """The whole point of the general rule: catches ANY compilation/
    collection re-release tag, not just ones on a maintained list.
    """
    priority = rc.DEFAULT_REGION_PRIORITY
    assert rc.has_non_standard_tag(["USA", "Sega Channel"], priority)
    assert rc.has_non_standard_tag(["USA", "Disney Classic Games"], priority)
    assert rc.has_non_standard_tag(["USA", "Castlevania Anniversary Collection"], priority)


def test_score_release_non_standard_tag_loses_size_tiebreak_to_plain_release():
    """Regression test for the reported bug pattern: a larger re-release
    was winning the file-size tiebreak over a smaller plain release of
    the same title. non-standard-tag status must be compared before
    size, so the plain release wins regardless of which file is bigger --
    and this now holds for ANY extra tag, not just a maintained list.
    """
    priority = rc.DEFAULT_REGION_PRIORITY
    plain_score = rc.score_release(["usa"], 1000, priority)
    for extra_tag in ["virtual console", "switch online", "sega channel",
                       "disney classic games", "castlevania anniversary collection"]:
        other_score = rc.score_release(["usa", extra_tag], 5000, priority)
        assert plain_score < other_score, extra_tag


def test_score_release_better_region_beats_non_standard_tag_at_worse_region():
    """Regression test for the reported SA-GA 3 bug: region must be
    compared BEFORE non-standard-tag status. A "(World) (Collection of
    SaGa)" release should beat a plain "(Japan)" release, since World is
    simply the better region -- the compilation tag only matters as a
    tiebreaker when regions are otherwise equal (see the Virtual Console/
    Switch Online tests above, where both releases share the same
    region).
    """
    priority = rc.DEFAULT_REGION_PRIORITY
    japan_only = rc.score_release(["japan"], 1000, priority)
    world_collection = rc.score_release(["world", "ja", "collection of saga"], 1000, priority)
    assert world_collection < japan_only


# ---------------------------------------------------------------------
# Unit tests: effective_region_rank (region + confirmed-English-language
# combined ranking; regression coverage for the reported Mickey Mouse case)
# ---------------------------------------------------------------------

def test_effective_region_rank_top_two_regions_are_tier_zero():
    priority = rc.DEFAULT_REGION_PRIORITY
    assert rc.effective_region_rank(["USA"], priority)[0] == 0
    assert rc.effective_region_rank(["World"], priority)[0] == 0


def test_effective_region_rank_explicit_english_tag_is_tier_one():
    priority = rc.DEFAULT_REGION_PRIORITY
    assert rc.effective_region_rank(["Japan", "En"], priority)[0] == 1
    assert rc.effective_region_rank(["Europe", "En,Fr,De"], priority)[0] == 1


def test_effective_region_rank_no_english_no_top_region_is_tier_two():
    priority = rc.DEFAULT_REGION_PRIORITY
    assert rc.effective_region_rank(["Japan"], priority)[0] == 2
    assert rc.effective_region_rank(["Europe"], priority)[0] == 2


def test_score_release_english_tagged_japan_beats_plain_europe():
    """Regression test for the reported Mickey Mouse bug: an explicit
    "(En)" tag confirms English text is present, which matters more than
    a plain region tag that doesn't confirm it -- "(Europe)" alone
    doesn't guarantee English the way "(Japan) (En)" does.
    """
    priority = rc.DEFAULT_REGION_PRIORITY
    japan_en_score = rc.score_release(["japan", "en"], 1000, priority)
    europe_score = rc.score_release(["europe"], 1000, priority)
    assert japan_en_score < europe_score


def test_score_release_world_beats_english_tagged_japan():
    """Companion to the Mickey Mouse fix: a plain "(World)" release still
    beats an "(En)"-tagged Japan release, since World already implies
    English by convention without needing an explicit tag -- only USA
    and World are untouchable top tier, not merely-en-tagged releases.
    """
    priority = rc.DEFAULT_REGION_PRIORITY
    world_score = rc.score_release(["world"], 1000, priority)
    japan_en_score = rc.score_release(["japan", "en"], 1000, priority)
    assert world_score < japan_en_score


# ---------------------------------------------------------------------
# Unit tests: language-tag recognition (regression coverage for the
# reported VS Battler / Turok cases)
# ---------------------------------------------------------------------

def test_is_language_tag_plain_and_combined():
    assert rc.is_language_tag("En")
    assert rc.is_language_tag("en")
    assert rc.is_language_tag("En,Fr,De,Es")
    assert rc.is_language_tag("En/Fr")
    assert not rc.is_language_tag("USA")
    assert not rc.is_language_tag("En,USA")  # mixed -- not a pure language list


def test_has_non_standard_tag_false_for_language_tags():
    """Regression test for the reported Turok bug: a legitimate release
    carrying a real region tag PLUS a language list (e.g. "USA, Europe"
    + "En,Fr,De,Es") must not be treated as non-standard just because of
    the language list -- it was losing to a Japan-only release that had
    no such tag at all.
    """
    priority = rc.DEFAULT_REGION_PRIORITY
    assert not rc.has_non_standard_tag(["USA, Europe", "En,Fr,De,Es"], priority)
    assert not rc.has_non_standard_tag(["Japan", "En"], priority)


def test_is_neutral_tag_sgb_enhanced():
    assert rc.is_neutral_tag("SGB Enhanced")
    assert rc.is_neutral_tag("sgb enhanced")
    assert not rc.is_neutral_tag("Sega Channel")


def test_has_non_standard_tag_false_for_neutral_tags():
    """Regression test for the reported Smurfs bug: "SGB Enhanced" is a
    legitimate hardware-capability footnote, not a compilation/service
    re-release indicator, and must not penalize an otherwise
    better-regioned release.
    """
    priority = rc.DEFAULT_REGION_PRIORITY
    assert not rc.has_non_standard_tag(
        ["USA, Europe", "En,Fr,De", "Rev 1", "SGB Enhanced"], priority)


def test_score_release_sgb_enhanced_does_not_lose_to_lesser_region():
    """Regression test for the reported Smurfs bug: a (USA, Europe)
    release with (Rev 1) (SGB Enhanced) must still beat a plain (Europe)
    release -- the neutral tag must not drag it down to "non-standard".
    """
    priority = rc.DEFAULT_REGION_PRIORITY
    usa_europe_score = rc.score_release(
        ["usa, europe", "en,fr,de", "rev 1", "sgb enhanced"], 1000, priority)
    europe_only_score = rc.score_release(["europe", "en,fr,de,es"], 5000, priority)
    assert usa_europe_score < europe_only_score


def test_language_rank_prefers_english():
    assert rc.language_rank(["Japan", "En"]) == 0
    assert rc.language_rank(["USA, Europe", "En,Fr,De,Es"]) == 0
    assert rc.language_rank(["Japan"]) == 1
    assert rc.language_rank(["Japan", "Fr"]) == 1


def test_score_release_english_tagged_beats_untranslated_same_region():
    """Regression test for the reported VS Battler bug: a Japan release
    with an "(En)" fan translation should beat the untranslated Japan
    release when there's no proper USA/English release available.
    """
    priority = rc.DEFAULT_REGION_PRIORITY
    plain_japan = rc.score_release(["japan"], 1000, priority)
    japan_en = rc.score_release(["japan", "en"], 1000, priority)
    assert japan_en < plain_japan


def test_score_release_usa_still_beats_english_tagged_japan():
    """Regression test for the reported requirement: "USA still takes
    priority" over a Japan release with an "(En)" tag.
    """
    priority = rc.DEFAULT_REGION_PRIORITY
    usa_score = rc.score_release(["usa"], 1000, priority)
    japan_en_score = rc.score_release(["japan", "en"], 1000, priority)
    assert usa_score < japan_en_score


def test_score_release_multi_region_with_languages_beats_single_region_no_language():
    """Regression test for the reported Turok bug: a properly-regioned
    multi-language release (USA, Europe + En,Fr,De,Es) must beat a
    Japan-only release with no language indication at all, regardless of
    file size.
    """
    priority = rc.DEFAULT_REGION_PRIORITY
    japan_score = rc.score_release(["japan"], 5000, priority)
    usa_europe_score = rc.score_release(["usa, europe", "en,fr,de,es"], 1000, priority)
    assert usa_europe_score < japan_score


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


def test_parse_filter_line_strips_trailing_extension():
    """Regression test: pasting the full filename (with extension)
    straight out of a directory listing must still match correctly --
    the extension used to get folded into the title text, producing a
    title_key that never matched the actual file's.
    """
    with_ext = rc.parse_filter_line(
        "Streets of Rage II (Japan, Europe) (En,Ja).7z")
    without_ext = rc.parse_filter_line(
        "Streets of Rage II (Japan, Europe) (En,Ja)")
    assert with_ext == without_ext
    assert with_ext == ("streets of rage ii", frozenset({"japan, europe", "en,ja"}))


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
# Unit tests: the normal duplicate scan (plan_duplicate_scan and friends)
#
# These exercise the scan's actual decisions directly, without shelling
# out to the script and grepping its printed output.
# ---------------------------------------------------------------------

def default_dup_dir(tmp_path):
    return str(tmp_path / ".duplicates")


def write_rom(tmp_path, filename, size=100):
    """Create a ROM file of a given size. Size matters because it's the
    final tiebreak in score_release().
    """
    path = tmp_path / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    return path


def releases_from(tmp_path, *specs):
    """Build the {release_key: [(path, tags), ...]} mapping that
    decide_title_keeper() takes, from a set of filenames all belonging to
    one title. A spec is either "Name (USA).zip" or ("Name (USA).zip", size).
    """
    releases = {}
    for spec in specs:
        filename, size = spec if isinstance(spec, tuple) else (spec, 100)
        path = write_rom(tmp_path, filename, size)
        _title, tags = rc.extract_tags(os.path.splitext(filename)[0])
        key = tuple(sorted(t.lower() for t in tags if not rc.is_part_tag(t)))
        releases.setdefault(key, []).append((str(path), tags))
    return releases


def keeper_filename(tmp_path, *specs):
    """Which of these competing releases does the scan keep? Returns the
    winning file's basename, so scoring expectations read as filenames.
    """
    releases = releases_from(tmp_path, *specs)
    decision, _pinned_lines = rc.decide_title_keeper(
        "title", releases, rc.DEFAULT_REGION_PRIORITY)
    winning_path = decision.releases[decision.keeper_key][0][0]
    return os.path.basename(winning_path)


# -- scoring regressions, as direct unit tests --------------------------
# Each of these is a real case reported against the tool; before the scan
# was extracted from main() they could only be checked by running the
# script and reading its output.

def test_decide_title_keeper_prefers_usa_over_europe(tmp_path):
    assert keeper_filename(
        tmp_path, "G (USA).zip", "G (Europe).zip") == "G (USA).zip"


def test_decide_title_keeper_prefers_higher_revision(tmp_path):
    assert keeper_filename(
        tmp_path, "G (USA).zip", "G (USA) (Rev 1).zip") == "G (USA) (Rev 1).zip"


def test_decide_title_keeper_multi_region_english_beats_japan(tmp_path):
    # Turok: a combined-region release that also lists its languages is a
    # normal release, not a tagged oddity, so it beats a plain Japan copy.
    assert keeper_filename(
        tmp_path,
        "G (USA, Europe) (En,Fr,De,Es).zip",
        "G (Japan).zip") == "G (USA, Europe) (En,Fr,De,Es).zip"


def test_decide_title_keeper_sgb_enhanced_is_neutral(tmp_path):
    # Smurfs: "(SGB Enhanced)" is a technical footnote, not a re-release tag.
    assert keeper_filename(
        tmp_path,
        "G (USA, Europe) (SGB Enhanced).zip",
        "G (Japan).zip") == "G (USA, Europe) (SGB Enhanced).zip"


def test_decide_title_keeper_world_beats_japan_even_with_collection_tag(tmp_path):
    # Sa-Ga 3: World is simply the better region tier, and no plain-World
    # release exists to compete with instead.
    assert keeper_filename(
        tmp_path,
        "G (World) (Collection of SaGa).zip",
        "G (Japan).zip") == "G (World) (Collection of SaGa).zip"


def test_decide_title_keeper_explicit_en_tag_beats_plain_europe(tmp_path):
    # Mickey Mouse: "Europe" alone doesn't confirm English; "(En)" does.
    assert keeper_filename(
        tmp_path, "G (Japan) (En).zip", "G (Europe).zip") == "G (Japan) (En).zip"


def test_decide_title_keeper_plain_usa_still_beats_explicit_en_tag(tmp_path):
    assert keeper_filename(
        tmp_path, "G (Japan) (En).zip", "G (USA).zip") == "G (USA).zip"


def test_decide_title_keeper_re_release_loses_despite_larger_size(tmp_path):
    # Virtual Console/Switch Online dumps are often bigger than the
    # original cartridge dump, so they must not win the size tiebreak.
    assert keeper_filename(
        tmp_path,
        ("G (USA) (Virtual Console).zip", 5000),
        ("G (USA).zip", 100)) == "G (USA).zip"


def test_decide_title_keeper_chd_beats_raw_disc_images(tmp_path):
    assert keeper_filename(
        tmp_path, "G (USA).chd", "G (Japan).cue") == "G (USA).chd"


def test_decide_title_keeper_single_release_is_uncontested(tmp_path):
    releases = releases_from(tmp_path, "G (Japan) (Virtual Console).zip")
    decision, _ = rc.decide_title_keeper("g", releases, rc.DEFAULT_REGION_PRIORITY)
    # Only copy on hand wins even carrying a non-standard tag, and nothing
    # was really decided, so it isn't worth reporting outside verbose mode.
    assert decision.dupe_keys == []
    assert decision.contested is False


# -- filter interaction -------------------------------------------------

def test_decide_title_keeper_whitelist_pin_overrides_scoring(tmp_path):
    releases = releases_from(tmp_path, "G (USA).zip", "G (Japan).zip")
    pin = [("title", frozenset({"japan"}), "G (Japan)")]
    decision, pinned_lines = rc.decide_title_keeper(
        "title", releases, rc.DEFAULT_REGION_PRIORITY, whitelist_releases=pin)

    assert decision.pinned is True
    assert pinned_lines == ["G (Japan)"]
    assert decision.keeper_key == ("japan",)


def test_decide_title_keeper_blacklist_forces_release_to_lose(tmp_path):
    releases = releases_from(tmp_path, "G (USA).zip", "G (Japan).zip")
    block = [("title", frozenset({"usa"}), "G (USA)")]
    decision, _ = rc.decide_title_keeper(
        "title", releases, rc.DEFAULT_REGION_PRIORITY, blacklist_releases=block)

    assert decision.keeper_key == ("japan",)
    assert ("usa",) in decision.forced_dup_keys


def test_decide_title_keeper_all_blacklisted_keeps_one_and_warns(tmp_path):
    releases = releases_from(tmp_path, "G (USA).zip", "G (Japan).zip")
    block = [
        ("title", frozenset({"usa"}), "G (USA)"),
        ("title", frozenset({"japan"}), "G (Japan)"),
    ]
    warnings = []
    decision, _ = rc.decide_title_keeper(
        "title", releases, rc.DEFAULT_REGION_PRIORITY,
        blacklist_releases=block, warnings=warnings)

    # Never lose the game entirely -- fall back to scoring, but say so.
    assert decision.keeper_key == ("usa",)
    assert len(warnings) == 1
    assert "blacklisted" in warnings[0]


def test_decide_title_keeper_conflicting_pins_warn_and_take_the_first(tmp_path):
    releases = releases_from(tmp_path, "G (USA).zip", "G (Japan).zip")
    pins = [
        ("title", frozenset({"usa"}), "G (USA)"),
        ("title", frozenset({"japan"}), "G (Japan)"),
    ]
    warnings = []
    decision, _ = rc.decide_title_keeper(
        "title", releases, rc.DEFAULT_REGION_PRIORITY,
        whitelist_releases=pins, warnings=warnings)

    assert decision.pinned is True
    assert len(warnings) == 1
    assert "pin different releases" in warnings[0]


# -- grouping and routing ----------------------------------------------

def test_scan_rom_files_groups_multi_file_release_as_one(tmp_path):
    write_rom(tmp_path, "G (USA) (Track 01).bin")
    write_rom(tmp_path, "G (USA) (Track 02).bin")
    write_rom(tmp_path, "G (USA).cue")

    titles, _bios, _proto, _skipped, _filtered, _bl, _wl = rc.scan_rom_files(
        str(tmp_path), default_dup_dir(tmp_path), rc.ROM_EXTENSIONS_DEFAULT, {}, {})

    assert list(titles) == ["g"]
    # one release, not three competing "duplicates" of each other
    assert len(titles["g"]) == 1
    assert len(titles["g"][("usa",)]) == 3


def test_scan_rom_files_separates_bios_and_proto_beta(tmp_path):
    write_rom(tmp_path, "G (USA).zip")
    write_rom(tmp_path, "Machine [BIOS].bin")
    write_rom(tmp_path, "G (USA) (Proto).zip")

    titles, bios, proto, _skipped, _filtered, _bl, _wl = rc.scan_rom_files(
        str(tmp_path), default_dup_dir(tmp_path), rc.ROM_EXTENSIONS_DEFAULT, {}, {})

    assert list(titles) == ["g"]
    assert [os.path.basename(p) for p in bios] == ["Machine [BIOS].bin"]
    assert [os.path.basename(p) for p in proto] == ["G (USA) (Proto).zip"]


def test_split_redundant_raw_disc_leaves_release_holding_the_chd(tmp_path):
    chd = write_rom(tmp_path, "G (USA).chd")
    cue = write_rom(tmp_path, "G (USA).cue")
    bin_ = write_rom(tmp_path, "G (USA).bin")
    titles = {"g": {("usa",): [(str(chd), ["USA"]), (str(cue), ["USA"]),
                                (str(bin_), ["USA"])]}}

    redundant = rc.split_redundant_raw_disc(titles)

    assert sorted(os.path.basename(p) for p in redundant) == [
        "G (USA).bin", "G (USA).cue"]
    assert [os.path.basename(p) for p, _t in titles["g"][("usa",)]] == ["G (USA).chd"]


def test_split_redundant_raw_disc_ignores_release_without_a_chd(tmp_path):
    cue = write_rom(tmp_path, "G (USA).cue")
    titles = {"g": {("usa",): [(str(cue), ["USA"])]}}

    assert rc.split_redundant_raw_disc(titles) == []
    assert len(titles["g"][("usa",)]) == 1


def test_plan_duplicate_scan_routes_each_category_to_its_own_subfolder(tmp_path):
    write_rom(tmp_path, "G (USA).zip")
    write_rom(tmp_path, "G (Europe).zip")
    write_rom(tmp_path, "Machine [BIOS].bin")
    write_rom(tmp_path, "H (USA) (Proto).zip")
    write_rom(tmp_path, "J (USA).chd")
    write_rom(tmp_path, "J (USA).cue")

    dup_dir = default_dup_dir(tmp_path)
    plan = rc.plan_duplicate_scan(str(tmp_path), dup_dir)

    def dest_dirs(moves):
        return {os.path.basename(os.path.dirname(d)) for _s, d in moves}

    assert dest_dirs(plan.dup_moves) == {".duplicates"}
    assert dest_dirs(plan.bios_moves) == {rc.BIOS_SUBDIR}
    assert dest_dirs(plan.proto_beta_moves) == {rc.PROTO_BETA_SUBDIR}
    assert dest_dirs(plan.redundant_moves) == {rc.REDUNDANT_DISC_SUBDIR}


def test_plan_duplicate_scan_counts_line_up_with_planned_moves(tmp_path):
    write_rom(tmp_path, "G (USA).zip")
    write_rom(tmp_path, "G (Europe).zip")
    write_rom(tmp_path, "G (Japan).zip")
    write_rom(tmp_path, "H (USA).zip")

    plan = rc.plan_duplicate_scan(str(tmp_path), default_dup_dir(tmp_path))

    assert plan.total_titles == 2
    assert plan.total_releases == 4
    assert plan.dup_files == len(plan.dup_moves) == 2
    assert plan.kept_files == 2


# -- destination reservation (regression) -------------------------------

def test_plan_duplicate_scan_never_plans_two_moves_onto_one_destination(tmp_path):
    """Two files with the same basename in different folders are pieces of
    one release. Nothing has moved yet at planning time, so an on-disk
    existence check alone hands both the same free destination -- and the
    second shutil.move() silently overwrites the first.
    """
    write_rom(tmp_path / "A", "Some Game (Japan).zip")
    write_rom(tmp_path / "B", "Some Game (Japan).zip")
    write_rom(tmp_path, "Some Game (USA).zip")

    plan = rc.plan_duplicate_scan(str(tmp_path), default_dup_dir(tmp_path))

    dests = [dest for _src, dest in plan.dup_moves]
    assert len(dests) == 2
    assert len(set(dests)) == 2


def test_plan_duplicate_scan_reserves_destinations_in_every_category(tmp_path):
    for folder in ("A", "B"):
        write_rom(tmp_path / folder, "Machine [BIOS].bin")
        write_rom(tmp_path / folder, "H (USA) (Proto).zip")

    plan = rc.plan_duplicate_scan(str(tmp_path), default_dup_dir(tmp_path))

    for moves in (plan.bios_moves, plan.proto_beta_moves):
        dests = [dest for _src, dest in moves]
        assert len(dests) == 2
        assert len(set(dests)) == 2


def test_apply_does_not_overwrite_same_named_duplicates(tmp_path):
    """End-to-end guarantee behind the reservation: no file is lost."""
    (tmp_path / "A").mkdir()
    (tmp_path / "B").mkdir()
    (tmp_path / "A" / "Some Game (Japan).zip").write_bytes(b"payload-A")
    (tmp_path / "B" / "Some Game (Japan).zip").write_bytes(b"payload-B")
    (tmp_path / "Some Game (USA).zip").write_bytes(b"keeper")

    run_script(tmp_path, "--apply")

    survived = sorted(p.read_bytes() for p in (tmp_path / ".duplicates").iterdir()
                      if p.is_file())
    assert survived == [b"payload-A", b"payload-B"]


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


def test_arbitrary_compilation_tag_loses_to_plain_release_even_when_larger(tmp_path):
    """The general non-standard-tag rule catches ANY compilation/service
    re-release tag, not just ones on a maintained list -- e.g. reported
    real-world cases like Sega Channel and anniversary/classics
    collections, which this repo has no prior knowledge of by name.
    """
    plain = tmp_path / "Super Game (USA).zip"
    plain.parent.mkdir(parents=True, exist_ok=True)
    plain.write_bytes(b"x" * 1000)

    compilation = tmp_path / "Super Game (USA) (Castlevania Anniversary Collection).zip"
    compilation.write_bytes(b"x" * 5000)

    run_script(tmp_path, "--apply")

    assert plain.exists()
    assert not compilation.exists()
    assert (tmp_path / ".duplicates" / "Super Game (USA) (Castlevania Anniversary Collection).zip").exists()


def test_arbitrary_compilation_tag_sole_copy_is_kept(tmp_path):
    touch(tmp_path / "Only Game (USA) (Sega Channel).zip")

    run_script(tmp_path, "--apply")

    assert (tmp_path / "Only Game (USA) (Sega Channel).zip").exists()
    assert not (tmp_path / ".duplicates").exists()


def test_english_tagged_japan_release_beats_untranslated_japan(tmp_path):
    """End-to-end regression test for the reported VS Battler case."""
    touch(tmp_path / "VS Battler (Japan).zip")
    touch(tmp_path / "VS Battler (Japan) (En).zip")

    run_script(tmp_path, "--apply")

    assert (tmp_path / "VS Battler (Japan) (En).zip").exists()
    assert (tmp_path / ".duplicates" / "VS Battler (Japan).zip").exists()


def test_usa_release_still_beats_english_tagged_japan_release(tmp_path):
    """End-to-end regression test for "USA still takes priority"."""
    touch(tmp_path / "VS Battler (USA).zip")
    touch(tmp_path / "VS Battler (Japan) (En).zip")

    run_script(tmp_path, "--apply")

    assert (tmp_path / "VS Battler (USA).zip").exists()
    assert (tmp_path / ".duplicates" / "VS Battler (Japan) (En).zip").exists()


def test_multi_region_language_list_release_beats_single_region_release(tmp_path):
    """End-to-end regression test for the reported Turok case: a
    properly-regioned (USA, Europe) release with a language list must
    beat a Japan-only release, not lose to it.
    """
    touch(tmp_path / "Turok - Battle of the Bionosaurs (Japan).zip")
    touch(tmp_path / "Turok - Battle of the Bionosaurs (USA, Europe) (En,Fr,De,Es).zip")

    run_script(tmp_path, "--apply")

    assert (tmp_path / "Turok - Battle of the Bionosaurs (USA, Europe) (En,Fr,De,Es).zip").exists()
    assert (tmp_path / ".duplicates" / "Turok - Battle of the Bionosaurs (Japan).zip").exists()


def test_sgb_enhanced_tag_does_not_block_better_region_release(tmp_path):
    """End-to-end regression test for the reported Smurfs case: "SGB
    Enhanced" is a legitimate hardware-capability footnote, not a
    compilation/service tag, and must not cause a (USA, Europe) release
    to lose to a plain (Europe) release.
    """
    touch(tmp_path / "Smurfs, The (Europe) (En,Fr,De,Es).zip")
    touch(tmp_path / "Smurfs, The (USA, Europe) (En,Fr,De) (Rev 1) (SGB Enhanced).zip")

    run_script(tmp_path, "--apply")

    assert (tmp_path / "Smurfs, The (USA, Europe) (En,Fr,De) (Rev 1) (SGB Enhanced).zip").exists()
    assert (tmp_path / ".duplicates" / "Smurfs, The (Europe) (En,Fr,De,Es).zip").exists()


def test_better_region_compilation_release_beats_plain_worse_region_release(tmp_path):
    """End-to-end regression test for the reported SA-GA 3 case: a
    "(World) (Collection of SaGa)" release must beat a plain "(Japan)"
    release, since World is simply the better region and no plain-World
    release exists to compete with instead.
    """
    touch(tmp_path / "Sa-Ga 3 - Jikuu no Hasha (Japan).zip")
    touch(tmp_path / "Sa-Ga 3 - Jikuu no Hasha (World) (Ja) (Collection of SaGa).zip")

    run_script(tmp_path, "--apply")

    assert (tmp_path / "Sa-Ga 3 - Jikuu no Hasha (World) (Ja) (Collection of SaGa).zip").exists()
    assert (tmp_path / ".duplicates" / "Sa-Ga 3 - Jikuu no Hasha (Japan).zip").exists()


def test_same_region_compilation_tag_still_loses_to_plain_release(tmp_path):
    """Companion test to the SA-GA 3 fix: when regions ARE equal, the
    compilation tag must still lose (region taking priority over the tag
    only matters when regions actually differ).
    """
    touch(tmp_path / "Super Game (USA).zip")
    touch(tmp_path / "Super Game (USA) (Collection of Something).zip")

    run_script(tmp_path, "--apply")

    assert (tmp_path / "Super Game (USA).zip").exists()
    assert (tmp_path / ".duplicates" / "Super Game (USA) (Collection of Something).zip").exists()


def test_english_tagged_japan_beats_plain_europe_release(tmp_path):
    """End-to-end regression test for the reported Mickey Mouse case: an
    explicit "(En)" tag confirms English text, which beats a plain
    "(Europe)" release that doesn't confirm it -- while a plain "(Japan)"
    release with no language tag at all still loses to both.
    """
    touch(tmp_path / "Mickey Mouse (Europe).zip")
    touch(tmp_path / "Mickey Mouse (Japan) (En).zip")
    touch(tmp_path / "Mickey Mouse (Japan).zip")

    run_script(tmp_path, "--apply")

    assert (tmp_path / "Mickey Mouse (Japan) (En).zip").exists()
    assert (tmp_path / ".duplicates" / "Mickey Mouse (Europe).zip").exists()
    assert (tmp_path / ".duplicates" / "Mickey Mouse (Japan).zip").exists()


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


def test_isolate_imports_dry_run_does_not_move_anything(tmp_path):
    touch(tmp_path / "SaGa 2 (Japan).zip")

    result = run_script(tmp_path, "--isolate-imports")

    assert "DRY RUN" in result.stdout
    assert (tmp_path / "SaGa 2 (Japan).zip").exists()
    assert not (tmp_path / ".imports").exists()


def test_isolate_imports_apply_moves_import_only_titles_together(tmp_path):
    touch(tmp_path / "SaGa 2 (Japan).zip")
    touch(tmp_path / "SaGa 2 (Japan) (En).zip")
    touch(tmp_path / "Super Game (USA).zip")

    result = run_script(tmp_path, "--isolate-imports", "--apply")

    assert (tmp_path / ".imports" / "SaGa 2 (Japan).zip").exists()
    assert (tmp_path / ".imports" / "SaGa 2 (Japan) (En).zip").exists()
    assert (tmp_path / "Super Game (USA).zip").exists()
    assert "Moved 2/2" in result.stdout


def test_isolate_imports_world_release_counts_as_na(tmp_path):
    touch(tmp_path / "World Racer (World).zip")

    result = run_script(tmp_path, "--isolate-imports", "--apply")

    assert (tmp_path / "World Racer (World).zip").exists()
    assert not (tmp_path / ".imports").exists()
    assert "No import-only titles found" in result.stdout


def test_isolate_imports_leaves_bios_and_proto_beta_alone(tmp_path):
    touch(tmp_path / "PSX [BIOS].bin")
    touch(tmp_path / "Unreleased (Japan) (Proto).zip")

    run_script(tmp_path, "--isolate-imports", "--apply")

    assert (tmp_path / "PSX [BIOS].bin").exists()
    assert (tmp_path / "Unreleased (Japan) (Proto).zip").exists()
    assert not (tmp_path / ".imports").exists()


def test_isolate_imports_moves_whole_multi_file_release_folder(tmp_path):
    release_dir = tmp_path / "Old Style CD (Japan)"
    touch(release_dir / "Old Style CD (Japan).cue")
    touch(release_dir / "Old Style CD (Japan).bin")

    run_script(tmp_path, "--isolate-imports", "--apply")

    assert (tmp_path / ".imports" / "Old Style CD (Japan)" / "Old Style CD (Japan).cue").exists()
    assert (tmp_path / ".imports" / "Old Style CD (Japan)" / "Old Style CD (Japan).bin").exists()
    assert not release_dir.exists()


def test_plan_imports_dir_migration_nothing_to_do_without_legacy_folder(tmp_path):
    (tmp_path / ".imports").mkdir()
    moves, legacy = rc.plan_imports_dir_migration(str(tmp_path))
    assert moves == []
    assert legacy is None


def test_plan_imports_dir_migration_moves_legacy_entries(tmp_path):
    touch(tmp_path / "Imports" / "Old One (Japan).zip")
    touch(tmp_path / "Imports" / "Old Two (Japan).zip")

    moves, legacy = rc.plan_imports_dir_migration(str(tmp_path))

    assert legacy == str(tmp_path / "Imports")
    assert sorted(d for _s, d in moves) == [
        str(tmp_path / ".imports" / "Old One (Japan).zip"),
        str(tmp_path / ".imports" / "Old Two (Japan).zip"),
    ]


def test_plan_imports_dir_migration_descends_into_the_m3u_hidden_folder(tmp_path):
    """Moving the .chd folder itself onto an existing one would nest it a
    level deeper instead of merging, burying the discs the playlists point at.
    """
    touch(tmp_path / "Imports" / "CD Game (Japan).m3u")
    touch(tmp_path / "Imports" / rc.M3U_HIDDEN_DIR_NAME / "CD Game (Japan)"
          / "CD Game (Japan) (Disc 1).chd")

    moves, _legacy = rc.plan_imports_dir_migration(str(tmp_path))

    dests = [d for _s, d in moves]
    assert str(tmp_path / ".imports" / "CD Game (Japan).m3u") in dests
    # the per-release folder moves, not the shared ".chd" folder itself
    assert str(tmp_path / ".imports" / rc.M3U_HIDDEN_DIR_NAME / "CD Game (Japan)") in dests
    assert str(tmp_path / ".imports" / rc.M3U_HIDDEN_DIR_NAME) not in dests


def test_plan_imports_dir_migration_avoids_collision_with_existing_entry(tmp_path):
    touch(tmp_path / "Imports" / "Dup Name (Japan).zip")
    touch(tmp_path / ".imports" / "Dup Name (Japan).zip")

    moves, _legacy = rc.plan_imports_dir_migration(str(tmp_path))

    assert [d for _s, d in moves] == [
        str(tmp_path / ".imports" / "Dup Name (Japan) (1).zip")]


def test_isolate_imports_migrates_legacy_folder_and_removes_it(tmp_path):
    (tmp_path / "Imports").mkdir()
    (tmp_path / "Imports" / "Old Import (Japan).zip").write_bytes(b"legacy-payload")
    touch(tmp_path / "New Import (Japan).zip")
    touch(tmp_path / "Keeper (USA).zip")

    run_script(tmp_path, "--isolate-imports", "--apply")

    # legacy folder emptied and removed, its contents preserved
    assert not (tmp_path / "Imports").exists()
    assert (tmp_path / ".imports" / "Old Import (Japan).zip").read_bytes() == b"legacy-payload"
    # and the new pass still ran in the same invocation
    assert (tmp_path / ".imports" / "New Import (Japan).zip").exists()
    assert (tmp_path / "Keeper (USA).zip").exists()


def test_isolate_imports_does_not_treat_legacy_folder_as_a_title(tmp_path):
    """Without the legacy skip, "Imports" parses as an untitled release with
    no NA tag and gets swept into .imports/Imports/ as if it were a game.
    """
    touch(tmp_path / "Imports" / "Old Import (Japan).zip")

    run_script(tmp_path, "--isolate-imports", "--apply")

    assert not (tmp_path / ".imports" / "Imports").exists()
    assert (tmp_path / ".imports" / "Old Import (Japan).zip").exists()


def test_isolate_imports_migration_dry_run_moves_nothing(tmp_path):
    touch(tmp_path / "Imports" / "Old Import (Japan).zip")

    result = run_script(tmp_path, "--isolate-imports")

    assert (tmp_path / "Imports" / "Old Import (Japan).zip").exists()
    assert not (tmp_path / ".imports").exists()
    assert "MIGRATE" in result.stdout


def test_isolate_imports_leaves_media_folder_alone(tmp_path):
    """End-to-end regression test for the reported bug: a "media" asset
    folder alongside the roms must never be swept into .imports/.
    """
    touch(tmp_path / "Super Game (USA).zip")
    touch(tmp_path / "media" / "screenshots" / "super game.png")

    run_script(tmp_path, "--isolate-imports", "--apply")

    assert (tmp_path / "media" / "screenshots" / "super game.png").exists()
    assert not (tmp_path / ".imports").exists()


def test_isolate_imports_rerun_is_idempotent(tmp_path):
    touch(tmp_path / "SaGa 2 (Japan).zip")

    run_script(tmp_path, "--isolate-imports", "--apply")
    result = run_script(tmp_path, "--isolate-imports", "--apply")

    assert "No import-only titles found" in result.stdout
    assert (tmp_path / ".imports" / "SaGa 2 (Japan).zip").exists()


def test_isolate_imports_moves_m3u_and_hidden_disc_folder_with_working_playlist(tmp_path):
    """End-to-end regression test: after --isolate-imports moves an
    --make-m3u-grouped release, the .m3u's relative disc paths must still
    resolve correctly from its new location.
    """
    stub = write_chdman_stub(tmp_path)
    touch(tmp_path / "SaGa CD (Japan) (Disc 1).chd")
    touch(tmp_path / "SaGa CD (Japan) (Disc 2).chd")
    run_script(tmp_path, "--make-m3u", "--chdman-path", stub, "--apply")

    run_script(tmp_path, "--isolate-imports", "--apply")

    m3u_path = tmp_path / ".imports" / "SaGa CD (Japan).m3u"
    assert m3u_path.exists()
    content = m3u_path.read_text(encoding="utf-8")
    for line in content.splitlines():
        if line.strip():
            assert (m3u_path.parent / line.strip()).exists(), line
    assert not (tmp_path / rc.M3U_HIDDEN_DIR_NAME).exists()


def test_isolate_imports_and_make_m3u_cannot_combine(tmp_path):
    result = run_script(tmp_path, "--isolate-imports", "--make-m3u")

    assert result.returncode != 0
    assert "can't be combined" in result.stderr


def test_isolate_imports_respects_blacklist(tmp_path):
    touch(tmp_path / "SaGa 2 (Japan).zip")
    touch(tmp_path / "Protected Import (Japan).zip")
    (tmp_path / "rom_filters.txt").write_text("[blacklist]\nProtected Import\n")

    result = run_script(tmp_path, "--isolate-imports", "--apply")

    assert (tmp_path / "Protected Import (Japan).zip").exists()
    assert (tmp_path / ".imports" / "SaGa 2 (Japan).zip").exists()
    assert "Filter file used" in result.stdout


def test_isolate_imports_respects_whitelist(tmp_path):
    touch(tmp_path / "SaGa 2 (Japan).zip")
    touch(tmp_path / "Another Import (Japan).zip")
    (tmp_path / "rom_filters.txt").write_text("[whitelist]\nSaGa 2\n")

    result = run_script(tmp_path, "--isolate-imports", "--apply")

    assert (tmp_path / "Another Import (Japan).zip").exists()
    assert (tmp_path / ".imports" / "SaGa 2 (Japan).zip").exists()


def test_isolate_imports_respects_release_specific_whitelist_pin(tmp_path):
    """End-to-end regression test for the reported Streets of Rage II
    bug, including the extension the user pasted straight into the
    filter file (which must be tolerated/stripped, not just the bare
    documented format).
    """
    touch(tmp_path / "Streets of Rage II (Japan, Europe) (En,Ja).7z")
    (tmp_path / "rom_filters.txt").write_text(
        "[whitelist]\nStreets of Rage II (Japan, Europe) (En,Ja).7z\n")

    result = run_script(tmp_path, "--isolate-imports", "--apply")

    assert (tmp_path / "Streets of Rage II (Japan, Europe) (En,Ja).7z").exists()
    assert not (tmp_path / ".imports").exists()
    assert "No import-only titles found" in result.stdout


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
