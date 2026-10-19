"""``Expect: 100-continue``: :mod:`xrdhttp.expect`, under xrdclient's name.

Waiting for the go-ahead before a large upload, and the early answer when a
head node (EOS's) redirects before reading the body, moved to xrdhttp in
0.4.0. :class:`~xrdclient.http.client.HTTPClient` calls :func:`wants_expect`
and :func:`await_continue`; xgfalclient imports :func:`await_continue` and
:class:`InterimAnswer` by this name.

This module replaces itself in ``sys.modules`` with :mod:`xrdhttp.expect`, so
``xrdclient.http.expect is xrdhttp.expect``: one set of classes for
``isinstance``, and a test that patches a name through either path patches
what every caller calls. The explicit re-exports are for type checkers, which do not
see the swap. New code should import :mod:`xrdhttp.expect` directly.
"""

import sys

from xrdhttp import expect as _lib
from xrdhttp.expect import CONTINUE_WAIT as CONTINUE_WAIT
from xrdhttp.expect import EXPECT_OVER as EXPECT_OVER
from xrdhttp.expect import InterimAnswer as InterimAnswer
from xrdhttp.expect import __all__ as __all__
from xrdhttp.expect import await_continue as await_continue
from xrdhttp.expect import wants_expect as wants_expect

sys.modules[__name__] = _lib
