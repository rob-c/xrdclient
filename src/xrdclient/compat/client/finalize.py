"""``XRootD.client.finalize``: what runs at interpreter exit.

The bindings register :func:`finalize` with :mod:`atexit` to close every
file still open and stop XrdCl's threads before Python tears itself down.
There are no XrdCl threads here, but open files and the connections compat
files share are just as worth closing politely, so importing this module
registers the same function, and calling it early is harmless: the next
open simply connects again.
"""

from __future__ import annotations

import atexit
import contextlib
import gc

from . import _channels
from .file import File

__all__ = ["finalize"]


@atexit.register
def finalize() -> None:
    """Close every open compat :class:`~.file.File`, then every shared connection."""
    for obj in gc.get_objects():
        with contextlib.suppress(ReferenceError):
            if isinstance(obj, File) and obj.is_open():
                obj.close()
    _channels.close_all()
