"""Moving bytes between endpoints.

    >>> from xrdclient import copy
    >>> copy("root://eos.example.org//store/f.root", "/scratch/f.root")
    CopyResult(...)

Any pair works - remote to local, local to remote, remote to remote, or an
open file object on either side - because every endpoint is reduced to a
binary stream before the pump ever sees it. :func:`third_party` is the
exception: the servers move the bytes between themselves, and
:func:`failed_end` says which of them refused a failed one.

``xrdclient.copy`` - the attribute of the top-level package - is the
:func:`copy` *function*, not this subpackage (see ``xrdclient._Package``), so
reach a name here as ``xrdclient.<name>`` or ``from xrdclient.copy import
<name>``; ``xrdclient.copy.tpc.failed_end`` is not an attribute path.
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # bound at runtime by ``__getattr__`` below
    from .engine import CopyResult, SyncMode, copy, copy_tree
    from .limits import CopyTimeoutError, RateThresholdError
    from .replicas import NoMoreReplicasError
    from .tpc import failed_end, third_party
    from .zip import append_zip
del TYPE_CHECKING

#: Public name -> the submodule that defines it. Imported on first use, so
#: that a leaf such as :mod:`xrdclient.copy.limits` can be imported without the
#: engine, the session layer and TLS.
_LAZY = {
    "CopyResult": "engine",
    "SyncMode": "engine",
    "copy": "engine",
    "copy_tree": "engine",
    "CopyTimeoutError": "limits",
    "RateThresholdError": "limits",
    "NoMoreReplicasError": "replicas",
    "third_party": "tpc",
    "failed_end": "tpc",
    "append_zip": "zip",
}


def __getattr__(name: str) -> object:
    """Bind a public name the first time it is asked for, then never again."""
    module = _LAZY.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(importlib.import_module(f"{__name__}.{module}"), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    """What is bound already, plus every public name still waiting to be."""
    return sorted(set(globals()) | set(__all__))


__all__ = [
    "CopyResult",
    "CopyTimeoutError",
    "NoMoreReplicasError",
    "RateThresholdError",
    "SyncMode",
    "append_zip",
    "copy",
    "copy_tree",
    "failed_end",
    "third_party",
]
