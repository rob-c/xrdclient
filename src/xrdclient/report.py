"""The storage-client-report: :mod:`xrdcore.report`, with xrdclient's URL type registered.

The versioned ``--json``/``--xml`` document that xrdclient's ``xrd-cp``,
``xrd-fs`` and ``xrd-proxy``, xgfalclient's ``gfal-*`` commands and xrdarc's
``xrdarc`` all write. The implementation moved to xrdcore in 0.4.0 so no tool
has to import a client to produce it; docs/output.md stays the
consumer-facing contract.

What this module adds is the one thing the shared module cannot know: an
:class:`~xrdclient.url.XRootDURL` is a dataclass, but a report should carry it
as its string (``root://host:1094//path``), not field by field. Importing this
module registers it (:func:`xrdcore.report.register_text_type`), then replaces
itself in ``sys.modules`` with :mod:`xrdcore.report`, so ``xrdclient.report is
xrdcore.report``: one context variable holds the active report whichever
library's command set it. Every xrdclient command imports this module (``from
.. import report as output``) rather than xrdcore's directly, so the
registration is in place before the first record. (The pre-0.4.0 private name
``xrdclient.cli._output`` is gone.) The explicit re-exports are for type
checkers, which do not see the swap.

``tests/test_import_cost.py`` holds this module to the cheap-import rule: it
must never import the client, TLS or crypto stack, because it runs before
``--help`` has been answered. :mod:`xrdclient.url` is a leaf, so registering
its type costs nothing a ``--help`` notices.
"""

import sys

from xrdcore import report as _core
from xrdcore.report import SCHEMA as SCHEMA
from xrdcore.report import VERSION as VERSION
from xrdcore.report import BinaryOutput as BinaryOutput
from xrdcore.report import Parser as Parser
from xrdcore.report import Report as Report
from xrdcore.report import TextOutput as TextOutput
from xrdcore.report import __all__ as __all__
from xrdcore.report import current as current
from xrdcore.report import dumps as dumps
from xrdcore.report import error as error
from xrdcore.report import flags as flags
from xrdcore.report import identify as identify
from xrdcore.report import message as message
from xrdcore.report import parse as parse
from xrdcore.report import payload as payload
from xrdcore.report import record as record
from xrdcore.report import register_text_type as register_text_type
from xrdcore.report import requested as requested
from xrdcore.report import run_cli as run_cli

from .url import XRootDURL

_core.register_text_type(XRootDURL)
sys.modules[__name__] = _core
