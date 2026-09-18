"""Moving bytes between endpoints.

    >>> from xrdclient import copy
    >>> copy("root://eos.example.org//store/f.root", "/scratch/f.root")
    CopyResult(...)

Any pair works - remote to local, local to remote, remote to remote, or an
open file object on either side - because every endpoint is reduced to a
binary stream before the pump ever sees it.
"""

from __future__ import annotations

from .engine import CopyResult, SyncMode, copy, copy_tree
from .tpc import third_party

__all__ = ["copy", "copy_tree", "third_party", "CopyResult", "SyncMode"]
