"""Safe XML parsing: :mod:`xrdcore.safexml`, under xrdclient's public name.

The implementation moved to xrdcore in 0.4.0 so that xrdclient, xgfalclient,
xrdhttp, xrdsec and xrdarc parse untrusted server XML through *one* guard
rather than copies of it. :func:`fromstring` refuses every ``<!DOCTYPE`` while
the tree is built - no entity can be declared, expanded or fetched - and
returns ElementTree's own :class:`~xml.etree.ElementTree.Element`.

This module replaces itself in ``sys.modules`` with :mod:`xrdcore.safexml`,
so ``xrdclient.safexml is xrdcore.safexml``:
an ``except xrdclient.safexml.UnsafeXML`` catches what any library raises, and
a test patching ``safexml._guard_expat`` patches the function every parse
calls. The explicit re-exports are for type checkers, which do not see the swap.
"""

import sys

from xrdcore import safexml as _core
from xrdcore.safexml import UnsafeXML as UnsafeXML
from xrdcore.safexml import __all__ as __all__
from xrdcore.safexml import fromstring as fromstring

sys.modules[__name__] = _core
