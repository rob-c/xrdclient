"""Public flag and enum types.

``IntFlag``/``IntEnum`` so they compose with ``|``, compare as ints on the
wire, and have a readable ``repr`` in tracebacks.

Nobody has to spell bits, though. Every one of these answers to its own
member names, in words, so the algebra is optional::

    >>> PrepareFlags("stage notify") is PrepareFlags.STAGE | PrepareFlags.NOTIFY
    True
    >>> QueryCode("checksum")
    <QueryCode.CHECKSUM: 3>
    >>> Access("rwxr-x---") == Access(0o750)
    True

A name that is not one of them says so, and says which ones there are, rather
than raising the enum's own ``0 is not a valid ...``.
"""

from __future__ import annotations

import difflib
import re
from enum import IntEnum, IntFlag
from typing import TYPE_CHECKING, Any, cast

from ._compat import flag_members, zip_strict

__all__ = [
    "OpenFlags",
    "Access",
    "DirListFlags",
    "QueryCode",
    "MkDirFlags",
    "StatInfoFlags",
    "PrepareFlags",
    "LocateFlags",
    "FattrCode",
    "ChkPointCode",
    "permissions",
    "parse_mode",
    "flags_for_mode",
    "open_flags",
    "dirlist_flags",
    "locate_flags",
    "prepare_flags",
]

#: What separates one word from the next: ``"stage notify"``, ``"stage,notify"``
#: and ``"stage|notify"`` all mean the same thing.
_SEPARATORS = re.compile(r"[\s,|+]+")


class _Words:
    """Mixed into every enum here so that words work wherever bits do.

    Case, hyphens and the separator are all up to the caller;
    ``"no-errors"``, ``"NO_ERRORS"`` and ``"no errors"`` are one flag.
    """

    @classmethod
    def _missing_(cls, value: object) -> Any:
        if not isinstance(value, str):
            # Composing two flags with ``|`` arrives here with the combined
            # int, and it is the base class that knows how to make one.
            return cast(Any, super())._missing_(value)
        enum = cast(Any, cls)
        many = issubclass(enum, IntFlag)
        words = _words(value)
        _validate_words(enum, words, many, value)
        return enum(_word_bits(enum, words))

    def __str__(self) -> str:
        """The words, not the number.

        Python 3.11 made ``str`` on an :class:`~enum.IntFlag` print the
        integer, which is right for a value going onto the wire and wrong for
        one going in front of a person: ``print(entry.stat.flags)`` should say
        ``is_dir|is_readable``. The integer is still one ``int()`` away, and
        that is what everything here sends.
        """
        flag = cast(Any, self)
        if flag._name_ is not None:
            return str(flag._name_).lower()
        parts = flag_members(flag)
        return "|".join(str(part._name_).lower() for part in parts) or str(int(flag))


def _no_such(enum: Any, word: str) -> str:
    """Why that word is not a flag, and what the caller probably meant."""
    known = [name.lower() for name in enum.__members__]
    near = difflib.get_close_matches(word.lower(), known, n=1)
    hint = f"did you mean {near[0]!r}?" if near else f"the names are {', '.join(sorted(known))}"
    return f"{enum.__name__} has no {word.lower()!r}; {hint}"


def _words(value: str) -> list[str]:
    normalized = value.strip().upper().replace("-", "_")
    return [word for word in _SEPARATORS.split(normalized) if word]


def _validate_words(enum: Any, words: list[str], many: bool, value: str) -> None:
    if not words:
        raise ValueError(f"{enum.__name__} needs a name: it was given {value!r}")
    if len(words) > 1 and not many:
        raise ValueError(f"{enum.__name__} takes one name, not {len(words)}: {value!r}")


def _word_bits(enum: Any, words: list[str]) -> int:
    bits = 0
    for word in words:
        member = enum.__members__.get(word)
        if member is None:
            raise ValueError(_no_such(enum, word))
        bits |= int(member)
    return bits


class OpenFlags(_Words, IntFlag):
    """``kXR_open`` options."""

    if TYPE_CHECKING:
        # ``_missing_`` accepts the member names as well as the bits, and a
        # type checker cannot see that from the members alone: without this
        # declaration ``OpenFlags("new makepath")`` runs but does not check.
        # The block does not exist at runtime.
        def __new__(cls, value: object = 0) -> OpenFlags: ...

    NONE = 0
    COMPRESS = 0x0001
    DELETE = 0x0002
    FORCE = 0x0004
    NEW = 0x0008
    READ = 0x0010
    UPDATE = 0x0020
    REFRESH = 0x0080
    MAKEPATH = 0x0100
    APPEND = 0x0200
    RETSTAT = 0x0400
    REPLICA = 0x0800
    POSC = 0x1000
    NOWAIT = 0x2000
    SEQIO = 0x4000
    WRITE = 0x8000


class Access(_Words, IntFlag):
    """``kXR_open`` / ``kXR_mkdir`` / ``kXR_chmod`` mode bits (POSIX order).

    Permissions come the way people write them down as well as the way the
    protocol wants them: ``Access("rwxr-x---")`` is what ``ls -l`` shows,
    ``Access("750")`` is what ``chmod`` takes, and both are ``0o750``.
    """

    if TYPE_CHECKING:  # words as well as bits - see OpenFlags

        def __new__(cls, value: object = 0) -> Access: ...

    NONE = 0
    OTHER_EXEC = 0o001
    OTHER_WRITE = 0o002
    OTHER_READ = 0o004
    GROUP_EXEC = 0o010
    GROUP_WRITE = 0o020
    GROUP_READ = 0o040
    OWNER_EXEC = 0o100
    OWNER_WRITE = 0o200
    OWNER_READ = 0o400

    @classmethod
    def _missing_(cls, value: object) -> Any:
        if isinstance(value, str):
            text = value.strip()
            if len(text) == 9 and set(text) <= set("rwx-"):
                bits = 0
                for character, bit in zip_strict(text, _RWX):
                    bits |= bit if character != "-" else 0
                return cls(bits)
            if text.isdigit():
                return cls(int(text, 8))
        return super()._missing_(value)


#: ``rwxrwxrwx`` bit by bit, owner first, for reading a mode off a listing.
_RWX = (0o400, 0o200, 0o100, 0o040, 0o020, 0o010, 0o004, 0o002, 0o001)


def permissions(mode: int | str | Access) -> int:
    """The nine POSIX permission bits, however they were written down.

    ``0o755``, ``"755"`` and ``"rwxr-xr-x"`` are the same directory; so is
    ``"owner_read owner_write"`` for a file nobody else may see. Anything
    outside the nine bits is dropped, because a caller who means to set the
    setuid bit over a network is far more likely to have made a mistake.
    """
    return int(Access(mode)) & 0o777 if isinstance(mode, str) else int(mode) & 0o777


def dirlist_flags(
    *,
    stat: bool = True,
    online: bool = False,
    algorithm: str = "",
    flags: DirListFlags | int | str | None = None,
) -> DirListFlags:
    """Listing options from what the caller asked for in words.

    ``flags``, when given, is used as it stands: an expert who has spelled
    the bits out means them.
    """
    if flags is not None:
        return DirListFlags(flags)
    chosen = DirListFlags.STAT if stat else DirListFlags.NONE
    if online:
        chosen |= DirListFlags.ONLINE
    if algorithm:
        # A digest arrives beside the stat, so asking for one asks for both.
        chosen |= DirListFlags.STAT | DirListFlags.CKSUM
    return chosen


def locate_flags(
    *,
    refresh: bool = False,
    no_wait: bool = False,
    add_peers: bool = False,
    prefer_name: bool = False,
    flags: LocateFlags | int | str | None = None,
) -> LocateFlags:
    """Locate options from what the caller asked for in words."""
    if flags is not None:
        return LocateFlags(flags)
    chosen = LocateFlags.NONE
    for asked, flag in (
        (refresh, LocateFlags.REFRESH),
        (no_wait, LocateFlags.NO_WAIT),
        (add_peers, LocateFlags.ADD_PEERS),
        (prefer_name, LocateFlags.PREFER_NAME),
    ):
        if asked:
            chosen |= flag
    return chosen


def prepare_flags(
    *,
    stage: bool | None = None,
    evict: bool = False,
    notify: bool = False,
    fresh: bool = False,
    flags: PrepareFlags | int | str | None = None,
) -> PrepareFlags:
    """Staging options from what the caller asked for in words.

    Staging is what a bare :meth:`~xrd.FileSystem.prepare` is for, so it is
    the default until another verb is named: ``evict=True`` on its own drops
    the disk copy rather than asking for one and dropping it in the same
    breath. Saying ``stage=True`` alongside it means both, for a caller who
    really does want the file recalled and then released.
    """
    if flags is not None:
        return PrepareFlags(flags)
    chosen = PrepareFlags.STAGE if (not evict if stage is None else stage) else PrepareFlags.NONE
    for asked, flag in (
        (evict, PrepareFlags.EVICT),
        (notify, PrepareFlags.NOTIFY),
        (fresh, PrepareFlags.FRESH),
    ):
        if asked:
            chosen |= flag
    return chosen


#: The letters a mode string is made of. Anything else in a string of open
#: options is a word - ``"new makepath"`` - and not a mode.
_MODE_LETTERS = frozenset("rwxabt+U")


def open_flags(flags: OpenFlags | int | str) -> OpenFlags:
    """``kXR_open`` options, however the caller chose to say them.

    A string of mode letters means what it means to :func:`open`, so
    ``"r"``, ``"w"``, ``"a"``, ``"x"`` and ``"r+"`` all work; any other
    string is read as the protocol's own option names, so ``"new makepath"``
    is :attr:`OpenFlags.NEW` with :attr:`OpenFlags.MAKEPATH`.
    """
    if isinstance(flags, str):
        return flags_for_mode(flags) if _MODE_LETTERS.issuperset(flags) else OpenFlags(flags)
    return OpenFlags(int(flags))


def parse_mode(mode: str) -> tuple[str, bool, bool]:
    """Split a :func:`open` mode string into ``(base, binary, updating)``."""
    cleaned = mode.replace("U", "")
    binary = "b" in cleaned
    text = "t" in cleaned
    if binary and text:
        raise ValueError(f"can't have text and binary mode at once: {mode!r}")
    updating = "+" in cleaned
    bases = [ch for ch in cleaned if ch in "rwxa"]
    if len(bases) != 1:
        raise ValueError(f"must have exactly one of create/read/write/append mode: {mode!r}")
    unknown = set(cleaned) - set("rwxa+bt")
    if unknown:
        raise ValueError(f"invalid mode: {mode!r}")
    # Text is the default, exactly as it is for the builtin.
    return bases[0], binary, updating


def flags_for_mode(mode: str, *, posc: bool = False, makepath: bool = True) -> OpenFlags:
    """Translate a Python mode string into ``kXR_open`` options.

    ``posc`` asks for persist-on-successful-close: the server discards a
    partially written file if the connection dies, which is what you want for
    a transfer and not what you want for a long-lived append.
    """
    base, _, updating = parse_mode(mode)
    flags = OpenFlags.NONE
    if base == "r":
        flags |= OpenFlags.UPDATE if updating else OpenFlags.READ
    elif base == "w":
        flags |= OpenFlags.UPDATE | OpenFlags.DELETE
    elif base == "x":
        flags |= OpenFlags.UPDATE | OpenFlags.NEW
    elif base == "a":
        flags |= OpenFlags.UPDATE | OpenFlags.APPEND
    if base != "r":
        if makepath:
            flags |= OpenFlags.MAKEPATH
        if posc:
            flags |= OpenFlags.POSC
    return flags


class DirListFlags(_Words, IntFlag):
    if TYPE_CHECKING:  # words as well as bits - see OpenFlags

        def __new__(cls, value: object = 0) -> DirListFlags: ...

    NONE = 0
    ONLINE = 0x01
    STAT = 0x02
    CKSUM = 0x04
    RECURSIVE = 0x08


class MkDirFlags(_Words, IntFlag):
    if TYPE_CHECKING:  # words as well as bits - see OpenFlags

        def __new__(cls, value: object = 0) -> MkDirFlags: ...

    NONE = 0
    MAKEPATH = 0x01


class QueryCode(_Words, IntEnum):
    if TYPE_CHECKING:  # words as well as bits - see OpenFlags

        def __new__(cls, value: object = 0) -> QueryCode: ...

    STATS = 1
    PREPARE = 2
    CHECKSUM = 3
    XATTR = 4
    SPACE = 5
    CHECKSUM_CANCEL = 6
    CONFIG = 7
    VISA = 8
    OPAQUE = 16
    OPAQUE_FILE = 32
    OPAQUE_GROUP = 64


class StatInfoFlags(_Words, IntFlag):
    if TYPE_CHECKING:  # words as well as bits - see OpenFlags

        def __new__(cls, value: object = 0) -> StatInfoFlags: ...

    NONE = 0
    X_SET = 0x01
    IS_DIR = 0x02
    OTHER = 0x04
    OFFLINE = 0x08
    IS_READABLE = 0x10
    IS_WRITABLE = 0x20
    POSC_PENDING = 0x40
    BACKUP_EXISTS = 0x80


class PrepareFlags(_Words, IntFlag):
    """``kXR_prepare`` options.

    Everything up to ``USE_TCP`` is a bit of the options byte. ``EVICT``
    arrived later and lives in ``optionX``, the extended half-word, so it is
    spelled here one byte up and :class:`~xrd.proto.requests.Prepare` puts it
    where the protocol wants it. Combining the two still works::

        fs.prepare(paths, flags=PrepareFlags.EVICT | PrepareFlags.NOTIFY)
    """

    if TYPE_CHECKING:  # words as well as bits - see OpenFlags

        def __new__(cls, value: object = 0) -> PrepareFlags: ...

    NONE = 0
    CANCEL = 1
    NOTIFY = 2
    NO_ERRORS = 4
    STAGE = 8
    WRITE_MODE = 16
    COLOCATE = 32
    FRESH = 64
    USE_TCP = 128
    EVICT = 1 << 8


class LocateFlags(_Words, IntFlag):
    if TYPE_CHECKING:  # words as well as bits - see OpenFlags

        def __new__(cls, value: object = 0) -> LocateFlags: ...

    NONE = 0
    ADD_PEERS = 1 << 0
    REFRESH = 1 << 7
    PREFER_NAME = 1 << 8
    #: ``kXR_4dirlist``: this locate is the prelude to a directory listing, so
    #: a redirector should answer with the servers that can list it rather
    #: than the ones that happen to hold a file of that name.
    FOR_DIRLIST = 1 << 10
    NO_WAIT = 1 << 13


class FattrCode(_Words, IntEnum):
    if TYPE_CHECKING:  # words as well as bits - see OpenFlags

        def __new__(cls, value: object = 0) -> FattrCode: ...

    DEL = 0
    GET = 1
    LIST = 2
    SET = 3


class ChkPointCode(_Words, IntEnum):
    if TYPE_CHECKING:  # words as well as bits - see OpenFlags

        def __new__(cls, value: object = 0) -> ChkPointCode: ...

    BEGIN = 0
    COMMIT = 1
    QUERY = 2
    ROLLBACK = 3
    XEQ = 4
