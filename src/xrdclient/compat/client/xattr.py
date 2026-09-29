"""``setXAttrAdler32``: leave an adler32 on a local file where xrootd will find it.

A server exporting a directory reads a file's checksum from the ``XrdCks``
extended attribute before it computes one, so writing the attribute ahead of
time saves the server reading the whole file. The record is XrdCks's own
96-byte ``XrdCksData``, laid out exactly as the bindings write it: the
algorithm's name, the file's mtime, a checksum-time delta, the length, and the
value.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import os
import struct
import sys
import time

__all__ = ["setXAttrAdler32"]

#: ``char Name[16]; long long fmTime; int csTime; short Rsvd1; char Rsvd2;
#: char Length; char Value[64]`` - big-endian, as XrdCks stores it.
_RECORD = struct.Struct(">16sqihbb64s")


def setXAttrAdler32(path: str, checksum: str) -> None:
    """Record ``checksum`` (eight hex digits) as ``path``'s adler32 for xrootd."""
    value = bytes.fromhex(checksum)
    if len(value) != 4:
        raise ValueError(f"an adler32 is eight hex digits, not {checksum!r}")
    mtime = int(os.stat(path).st_mtime)
    # ``csTime`` is when the checksum was taken, as seconds after the mtime.
    taken = int(time.time()) - mtime
    record = _RECORD.pack(b"adler32", mtime, taken, 0, 0, len(value), value)
    _setxattr(path, "XrdCks.adler32", record)


def _setxattr(path: str, name: str, value: bytes) -> None:
    """Set an extended attribute where Linux and macOS each keep them."""
    if hasattr(os, "setxattr"):
        # Linux: unprivileged attributes live in the ``user.`` namespace,
        # which XrdSys prefixes for itself.
        os.setxattr(path, f"user.{name}", value)
        return
    if sys.platform != "darwin":  # pragma: no cover - neither Linux nor macOS
        raise NotImplementedError("extended attributes are not supported on this platform")
    libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
    # macOS: setxattr(path, name, value, size, position, options).
    done = libc.setxattr(os.fsencode(path), name.encode(), value, len(value), 0, 0)
    if done != 0:
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code), path)
