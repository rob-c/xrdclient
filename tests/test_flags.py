"""Flags in words: what a person writes when they will not write bits.

Every enum in :mod:`xrdclient.flags` answers to its own member names, and the
keyword helpers turn an ordinary call's arguments into the flags an operation
sends. Between them, nothing above this module has to spell out an algebra.
"""

from __future__ import annotations

import pytest

from xrdclient.flags import (
    Access,
    DirListFlags,
    LocateFlags,
    OpenFlags,
    PrepareFlags,
    QueryCode,
    StatInfoFlags,
    dirlist_flags,
    locate_flags,
    open_flags,
    permissions,
    prepare_flags,
)

# ---------------------------------------------------------------------------
# Words instead of bits
# ---------------------------------------------------------------------------


def test_one_word_is_one_flag():
    assert PrepareFlags("stage") is PrepareFlags.STAGE


def test_several_words_are_several_flags():
    assert PrepareFlags("stage notify") == PrepareFlags.STAGE | PrepareFlags.NOTIFY


def test_the_separator_is_whatever_came_to_hand():
    both = DirListFlags.STAT | DirListFlags.ONLINE
    assert DirListFlags("stat online") == both
    assert DirListFlags("stat,online") == both
    assert DirListFlags("stat|online") == both
    assert DirListFlags(" stat + online ") == both


def test_case_and_hyphens_are_not_the_point():
    assert LocateFlags("no-wait") is LocateFlags.NO_WAIT
    assert LocateFlags("No_Wait") is LocateFlags.NO_WAIT


def test_an_enum_that_is_not_a_flag_set_takes_one_word():
    assert QueryCode("checksum") is QueryCode.CHECKSUM
    with pytest.raises(ValueError, match="QueryCode takes one name, not 2"):
        QueryCode("checksum space")


def test_a_name_that_is_no_name_at_all():
    with pytest.raises(ValueError, match="PrepareFlags needs a name"):
        PrepareFlags("   ")


def test_a_near_miss_is_told_what_it_nearly_was():
    with pytest.raises(ValueError, match="PrepareFlags has no 'stag'; did you mean 'stage'"):
        PrepareFlags("stag")


def test_a_wild_miss_is_told_the_whole_vocabulary():
    with pytest.raises(ValueError, match="the names are backup_exists"):
        StatInfoFlags("elephant")


def test_composing_two_members_still_makes_a_third():
    """The non-string way into ``_missing_``, which the base class handles."""
    assert int(OpenFlags.WRITE | OpenFlags.UPDATE) == 0x8020


def test_printing_a_flag_prints_the_words():
    assert str(StatInfoFlags.IS_DIR) == "is_dir"
    assert str(StatInfoFlags.IS_DIR | StatInfoFlags.IS_READABLE) == "is_dir|is_readable"
    assert int(StatInfoFlags.IS_DIR) == 2  # and the wire still sees a number


def test_printing_a_bit_nobody_has_defined_prints_the_number():
    """A server a version ahead of us can set one; it is kept, not dropped."""
    assert str(StatInfoFlags(1 << 30)) == str(1 << 30)


# ---------------------------------------------------------------------------
# Permissions
# ---------------------------------------------------------------------------


def test_a_mode_the_way_ls_prints_it():
    assert Access("rwxr-x---") == Access(0o750)


def test_a_mode_the_way_chmod_takes_it():
    assert Access("755") == Access(0o755)


def test_a_mode_by_the_names_of_its_bits():
    assert Access("owner_read owner_write") == Access(0o600)


def test_nine_characters_that_are_not_a_mode_are_still_words():
    with pytest.raises(ValueError, match="Access has no"):
        Access("rwxr-x--z")


def test_permissions_read_a_mode_however_it_was_written():
    assert permissions(0o644) == permissions("644") == permissions("rw-r--r--") == 0o644


def test_permissions_drop_what_is_not_a_permission():
    """setuid over a network is a slip far more often than an intention."""
    assert permissions(0o4755) == 0o755


# ---------------------------------------------------------------------------
# Keywords instead of flags
# ---------------------------------------------------------------------------


def test_open_flags_from_a_mode_string():
    assert open_flags("r") is OpenFlags.READ
    assert open_flags("w") == OpenFlags.UPDATE | OpenFlags.DELETE | OpenFlags.MAKEPATH


def test_open_flags_from_the_protocols_own_names():
    assert open_flags("new makepath") == OpenFlags.NEW | OpenFlags.MAKEPATH


def test_open_flags_from_the_bits_themselves():
    assert open_flags(OpenFlags.READ | OpenFlags.REFRESH) == OpenFlags.READ | OpenFlags.REFRESH
    assert open_flags(int(OpenFlags.READ)) is OpenFlags.READ


def test_a_listing_asks_for_the_stat_unless_told_not_to():
    assert dirlist_flags() is DirListFlags.STAT
    assert dirlist_flags(stat=False) is DirListFlags.NONE


def test_a_listing_that_wants_digests_wants_the_stat_with_them():
    assert dirlist_flags(algorithm="adler32") == DirListFlags.STAT | DirListFlags.CKSUM


def test_a_listing_can_ask_which_entries_are_on_disk():
    assert dirlist_flags(online=True) == DirListFlags.STAT | DirListFlags.ONLINE


def test_listing_flags_spelled_out_are_taken_as_they_stand():
    """An expert who names the bits means them, whatever the keywords say."""
    assert dirlist_flags(stat=False, flags="stat recursive") == (
        DirListFlags.STAT | DirListFlags.RECURSIVE
    )


def test_locate_asks_for_nothing_in_particular_by_default():
    assert locate_flags() is LocateFlags.NONE
    assert locate_flags(refresh=True, no_wait=True, add_peers=True, prefer_name=True) == (
        LocateFlags.REFRESH | LocateFlags.NO_WAIT | LocateFlags.ADD_PEERS | LocateFlags.PREFER_NAME
    )
    assert locate_flags(flags=LocateFlags.FOR_DIRLIST) is LocateFlags.FOR_DIRLIST


def test_prepare_stages_until_another_verb_is_named():
    assert prepare_flags() is PrepareFlags.STAGE
    assert prepare_flags(evict=True) is PrepareFlags.EVICT
    assert prepare_flags(stage=False) is PrepareFlags.NONE
    assert prepare_flags(stage=True, evict=True) == PrepareFlags.STAGE | PrepareFlags.EVICT
    assert prepare_flags(notify=True, fresh=True) == (
        PrepareFlags.STAGE | PrepareFlags.NOTIFY | PrepareFlags.FRESH
    )
    assert prepare_flags(flags="evict") is PrepareFlags.EVICT
