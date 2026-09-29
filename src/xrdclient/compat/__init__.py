"""A drop-in for the official XRootD Python bindings.

``xrdclient.compat.client`` is ``XRootD.client`` - the same classes, the same
signatures, the same ``(status, response)`` pairs - running on this library
instead of ``libXrdCl``. Porting is one line::

    from xrdclient.compat import client        # was: from XRootD import client

For code that cannot be edited at all, :func:`install` makes
``import XRootD`` itself resolve here, provided the real bindings are not
already imported.

The rest of this package is the native API, which raises rather than
returning a status; see ``docs/migrating.md`` for moving to it a call at a
time.
"""

from __future__ import annotations

import sys
import types

from . import client

__all__ = ["client", "install"]


def install() -> None:
    """Make ``import XRootD`` and ``from XRootD import client`` load this package.

    Only for code that cannot be changed: it answers for the ``XRootD`` name
    in :data:`sys.modules`, so it refuses when the real bindings are already
    imported rather than swapping one for the other under code that holds
    references to both.
    """
    existing = sys.modules.get("XRootD")
    if existing is not None and getattr(existing, "__xrdclient_compat__", False) is False:
        raise RuntimeError("the official XRootD bindings are already imported")
    package = types.ModuleType("XRootD")
    package.__path__ = []
    package.__xrdclient_compat__ = True  # type: ignore[attr-defined]
    package.client = client  # type: ignore[attr-defined]
    sys.modules["XRootD"] = package
    sys.modules["XRootD.client"] = client
    for name in (
        "_version",
        "copyprocess",
        "env",
        "file",
        "filesystem",
        "finalize",
        "flags",
        "glob_funcs",
        "responses",
        "tape",
        "url",
        "utils",
    ):
        sys.modules[f"XRootD.client.{name}"] = sys.modules[f"{client.__name__}.{name}"]
