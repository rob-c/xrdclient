"""ROOT's block compression, both ways.

A compressed ROOT object is a run of blocks, each with a nine-byte header
saying which algorithm made it and how long it is either way. Blocks are at
most 16 MB, so a large basket is several of them back to back.

zlib and lzma come from the standard library, and so does the pre-2005 ROOT
algorithm, which turns out to be deflate with the wrapper left off. LZ4 is
decoded and encoded here, in Python, because there is no LZ4 in the standard
library and a physics file should not need a wheel - it is a small format,
and this is a small decoder and a small, honest encoder. zstd is used from
the standard library on Python 3.14 and later, or from the ``zstandard``
package if it happens to be installed, and is otherwise refused by name
rather than guessed at.

Writing an LZ4 block also means writing the XXH64 checksum ROOT stores in
front of it, so XXH64 is here too - and the test suite checks it against
checksums ROOT itself computed, stored in the LZ4 blocks of a donor file.
"""

from __future__ import annotations

import lzma
import zlib

from .errors import FormatError, UnsupportedFeatureError

__all__ = ["decompress", "compress", "algorithm", "CODES"]

HEADER = 9
#: The most a single block may hold: its lengths are three-byte fields.
BLOCK = 0xFFFFFF
#: The number ROOT files carry for each algorithm, in the header's fCompress.
CODES = {"zlib": 1, "lzma": 2, "lz4": 4, "zstd": 5}
#: What each algorithm is asked for when the caller does not say.
LEVELS = {"zlib": 6, "lzma": 6, "lz4": 1, "zstd": 3}
TAGS = {"zlib": b"ZL", "lzma": b"XZ", "lz4": b"L4", "zstd": b"ZS"}
#: The method byte each writer puts third in the block header, matching what
#: ROOT writes: zlib says 8 (deflate), lzma says 0, lz4 and zstd say 1.
METHODS = {"zlib": 8, "lzma": 0, "lz4": 1, "zstd": 1}
#: What each tag in the block header means, for messages and for refusing well.
NAMES = {
    b"ZL": "zlib",
    b"XZ": "lzma",
    b"L4": "lz4",
    b"ZS": "zstd",
    b"CS": "the pre-2005 ROOT algorithm",
}


def algorithm(data: bytes) -> str:
    """The name of whatever compressed ``data``, for a message or a report."""
    return NAMES.get(bytes(data[:2]), "an unknown algorithm")


try:  # pragma: no cover - depends on the interpreter and an optional extra
    from compression.zstd import decompress as _unzstd  # type: ignore[import-not-found]

    def _zstd(block: bytes, size: int) -> bytes:
        return bytes(_unzstd(block))

except ImportError:  # pragma: no cover - before 3.14, where zstandard fills in
    try:
        from zstandard import ZstdDecompressor

        def _zstd(block: bytes, size: int) -> bytes:
            return bytes(ZstdDecompressor().decompress(block, max_output_size=size))

    except ImportError:

        def _zstd(block: bytes, size: int) -> bytes:
            raise UnsupportedFeatureError(
                "this file is zstd-compressed, which needs Python 3.14 or the zstandard "
                "package: pip install zstandard, or ask for the file written another way"
            )


try:  # pragma: no cover - depends on the interpreter and an optional extra
    from compression.zstd import compress as _std_zstd

    def _zstd_pack(block: bytes, level: int) -> bytes:
        return bytes(_std_zstd(block, level))

except ImportError:  # pragma: no cover - before 3.14, where zstandard fills in
    try:
        from zstandard import ZstdCompressor

        def _zstd_pack(block: bytes, level: int) -> bytes:
            return bytes(ZstdCompressor(level=level).compress(block))

    except ImportError:

        def _zstd_pack(block: bytes, level: int) -> bytes:
            raise UnsupportedFeatureError(
                "writing zstd needs Python 3.14 or the zstandard package: "
                "pip install zstandard, or write with zlib, lzma or lz4 instead"
            )


def _lz4(src: bytes, size: int) -> bytes:
    """One LZ4 block, in Python.

    The format is a loop of tokens: a length of bytes to copy out verbatim,
    then a distance back into what has already been written and a length to
    repeat from there. The repeat is allowed to overlap what it is producing,
    which is how LZ4 spells a run, so that case copies byte by byte.
    """
    out = bytearray()
    pos, end = 0, len(src)
    try:
        while pos < end:
            token = src[pos]
            pos += 1
            literals, pos = _lz4_length(src, pos, token >> 4)
            out += src[pos : pos + literals]
            pos += literals
            if pos >= end:
                break  # the last sequence is literals only
            offset = src[pos] | (src[pos + 1] << 8)
            pos += 2
            match, pos = _lz4_length(src, pos, token & 0xF, base=4)
            _lz4_match(out, offset, match)
    except IndexError:
        raise FormatError("an LZ4 block ends in the middle of a sequence") from None
    if len(out) != size:
        raise FormatError(f"an LZ4 block gave {len(out)} bytes where {size} were promised")
    return bytes(out)


def _lz4_length(src: bytes, pos: int, nibble: int, *, base: int = 0) -> tuple[int, int]:
    length = nibble + base
    if nibble != 15:
        return length, pos
    while (more := src[pos]) == 255:
        length += 255
        pos += 1
    return length + more, pos + 1


def _lz4_match(out: bytearray, offset: int, length: int) -> None:
    start = len(out) - offset
    if start < 0:
        raise FormatError(f"an LZ4 match points {-start} bytes before the block")
    if offset >= length:
        out += out[start : start + length]
        return
    for index in range(start, start + length):
        out.append(out[index])


#: The five prime constants XXH64 is built from, straight from its definition.
_P1, _P2, _P3, _P4, _P5 = (
    0x9E3779B185EBCA87,
    0xC2B2AE3D27D4EB4F,
    0x165667B19E3779F9,
    0x85EBCA77C2B2AE63,
    0x27D4EB2F165667C5,
)
_M64 = (1 << 64) - 1


def _rot(x: int, r: int) -> int:
    return ((x << r) | (x >> (64 - r))) & _M64


def _xxh64(data: bytes) -> int:
    """The XXH64 of ``data``, seed zero - the checksum ROOT stores with LZ4.

    Pure Python and verified in the test suite against checksums ROOT itself
    computed, so an LZ4 block written here carries the same eight bytes ROOT
    would have put there.
    """
    length = len(data)
    pos = 0
    if length >= 32:
        v1, v2, v3, v4 = (_P1 + _P2) & _M64, _P2, 0, (-_P1) & _M64
        for pos in range(0, length - 31, 32):
            lanes = int.from_bytes(data[pos : pos + 32], "little")
            v1 = (_rot((v1 + (lanes & _M64) * _P2) & _M64, 31) * _P1) & _M64
            v2 = (_rot((v2 + ((lanes >> 64) & _M64) * _P2) & _M64, 31) * _P1) & _M64
            v3 = (_rot((v3 + ((lanes >> 128) & _M64) * _P2) & _M64, 31) * _P1) & _M64
            v4 = (_rot((v4 + (lanes >> 192) * _P2) & _M64, 31) * _P1) & _M64
        pos += 32
        acc = (_rot(v1, 1) + _rot(v2, 7) + _rot(v3, 12) + _rot(v4, 18)) & _M64
        for v in (v1, v2, v3, v4):
            acc = ((acc ^ (_rot((v * _P2) & _M64, 31) * _P1)) * _P1 + _P4) & _M64
    else:
        acc = _P5
    acc = (acc + length) & _M64
    while pos + 8 <= length:
        lane = int.from_bytes(data[pos : pos + 8], "little")
        acc = (_rot(acc ^ (_rot((lane * _P2) & _M64, 31) * _P1) & _M64, 27) * _P1 + _P4) & _M64
        pos += 8
    if pos + 4 <= length:
        lane = int.from_bytes(data[pos : pos + 4], "little")
        acc = (_rot(acc ^ (lane * _P1) & _M64, 23) * _P2 + _P3) & _M64
        pos += 4
    for byte in data[pos:]:
        acc = (_rot(acc ^ (byte * _P5) & _M64, 11) * _P1) & _M64
    acc ^= acc >> 33
    acc = (acc * _P2) & _M64
    acc ^= acc >> 29
    acc = (acc * _P3) & _M64
    return acc ^ (acc >> 32)


def _extend(out: bytearray, value: int) -> None:
    """The 255-a-byte continuation LZ4 uses once a length nibble hits 15."""
    value -= 15
    while value >= 255:
        out.append(255)
        value -= 255
    out.append(value)


def _lz4_pack(src: bytes) -> bytes:
    """One LZ4 block, greedily: take the most recent match of four bytes or
    more within the 65535-byte window, or fall through to a literal.

    The format's own end rules are what the two limits below are for: the
    last five bytes are always literals, and no match may begin within twelve
    bytes of the end. Greedy matching does not always find what the reference
    encoder finds, but every block it makes is a valid one, and the decoder
    above - and any other LZ4 - undoes it exactly.
    """
    n = len(src)
    out = bytearray()
    recent: dict[bytes, int] = {}
    anchor = pos = 0
    while pos < n - 12:
        seq = src[pos : pos + 4]
        found = recent.get(seq)
        recent[seq] = pos
        if found is None or pos - found > 0xFFFF:
            pos += 1
            continue
        length = 4
        while pos + length < n - 5 and src[found + length] == src[pos + length]:
            length += 1
        literals = pos - anchor
        out.append((min(literals, 15) << 4) | min(length - 4, 15))
        if literals >= 15:
            _extend(out, literals)
        out += src[anchor:pos]
        out += (pos - found).to_bytes(2, "little")
        if length - 4 >= 15:
            _extend(out, length - 4)
        pos += length
        anchor = pos
    literals = n - anchor
    out.append(min(literals, 15) << 4)
    if literals >= 15:
        _extend(out, literals)
    out += src[anchor:]
    return bytes(out)


def compress(data: bytes, algorithm: str = "zlib", level: int | None = None) -> bytes:
    """``data`` as ROOT-framed blocks, ready to sit behind a key.

    Whether the result is worth using is the caller's call: ROOT stores an
    object raw when compressing it did not make it smaller, and the writer
    here does the same by comparing lengths.
    """
    if algorithm not in TAGS:
        raise ValueError(f"algorithm must be one of {', '.join(TAGS)}, not {algorithm!r}")
    if level is None:
        level = LEVELS[algorithm]
    out = bytearray()
    for start in range(0, len(data), BLOCK) or (0,):
        chunk = data[start : start + BLOCK]
        if algorithm == "zlib":
            packed = zlib.compress(chunk, level)
        elif algorithm == "lzma":
            packed = lzma.compress(chunk, format=lzma.FORMAT_XZ, preset=level)
        elif algorithm == "lz4":
            body = _lz4_pack(chunk)
            packed = _xxh64(body).to_bytes(8, "big") + body
        else:
            packed = _zstd_pack(chunk, level)
        out += TAGS[algorithm]
        out.append(METHODS[algorithm])
        out += len(packed).to_bytes(3, "little")
        out += len(chunk).to_bytes(3, "little")
        out += packed
    return bytes(out)


def _old(block: bytes, size: int) -> bytes:
    """One pre-2005 ROOT block, which is deflate with nothing wrapped round it.

    ROOT of that age carried its own copy of the classic PKZIP inflate, and
    what it wrote is the bare deflate stream - the same bytes zlib produces,
    without zlib's two-byte header and trailing checksum. That is what a
    negative window size asks :mod:`zlib` for.
    """
    try:
        out = zlib.decompress(block, -zlib.MAX_WBITS)
    except zlib.error as exc:
        raise FormatError(f"a pre-2005 block would not inflate: {exc}") from None
    if len(out) != size:
        raise FormatError(f"a pre-2005 block gave {len(out)} bytes where {size} were promised")
    return out


def decompress(data: bytes, size: int) -> bytes:
    """The ``size`` bytes that ``data``'s blocks were made from."""
    out = bytearray()
    pos = 0
    while len(out) < size:
        if pos + HEADER > len(data):
            raise FormatError(f"the compressed object ran out after {len(out)} of {size} bytes")
        tag = data[pos : pos + 2]
        packed = int.from_bytes(data[pos + 3 : pos + 6], "little")
        unpacked = int.from_bytes(data[pos + 6 : pos + 9], "little")
        block = data[pos + HEADER : pos + HEADER + packed]
        if len(block) != packed:
            raise FormatError(f"a block says it is {packed} bytes and only {len(block)} are here")
        pos += HEADER + packed

        if tag == b"ZL":
            out += zlib.decompress(block)
        elif tag == b"CS":
            out += _old(block, unpacked)
        elif tag == b"XZ":
            out += lzma.decompress(block)
        elif tag == b"L4":
            out += _lz4(block[8:], unpacked)  # after the checksum, which we do not need
        elif tag == b"ZS":
            out += _zstd(block, unpacked)
        else:
            raise UnsupportedFeatureError(
                f"this file is compressed with {algorithm(tag)}, which this reader does not "
                f"undo; rewrite it with hadd, which will give you one of zlib, lzma, lz4 "
                f"and zstd, and all four are read here"
            )
    if len(out) != size:
        raise FormatError(f"decompressing gave {len(out)} bytes where {size} were promised")
    return bytes(out)
