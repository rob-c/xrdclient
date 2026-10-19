"""The one backoff every retry loop waits with: :mod:`xrdcore.retry`, by xrdclient's name.

Exponential, capped, deadline-aware, full-jitter waits (:func:`delay`,
:func:`pause`), the swappable jitter source (:func:`use_rng`) and HTTP
``Retry-After`` (:func:`retry_after`). The implementation moved to xrdcore in
0.4.0 so that xrdclient's sessions, xrdhttp's connections and xgfalclient's
plug-ins all draw their jitter from one source: a test that pins it with
:func:`use_rng` pins every loop on the stack at once.

This module replaces itself in ``sys.modules`` with :mod:`xrdcore.retry`, so
``xrdclient.retry is xrdcore.retry``. The explicit re-exports are for type
checkers, which do not see the swap."""

import sys

from xrdcore import retry as _core
from xrdcore.retry import Uniform as Uniform
from xrdcore.retry import __all__ as __all__
from xrdcore.retry import ceiling as ceiling
from xrdcore.retry import delay as delay
from xrdcore.retry import pause as pause
from xrdcore.retry import retry_after as retry_after
from xrdcore.retry import use_rng as use_rng

sys.modules[__name__] = _core
