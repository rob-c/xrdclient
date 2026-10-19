"""Grid credential discovery and safe credential files: :mod:`xrdsec.credentials`.

Where the proxy, the user certificate and key, the bearer token and the CA
directory are - by the conventions every grid tool shares - and reading or
writing a credential file without trusting what an attacker could have put
there.

The implementation moved to xrdsec in 0.4.0, so that xrdclient, xgfalclient
and xrdarc share one copy of the grid's security code. This module replaces
itself in ``sys.modules`` with :mod:`xrdsec.credentials`, so
``xrdclient.credentials is xrdsec.credentials``: a class is the same class
whichever name it was imported under, an ``except`` written against this name
catches what xrdsec raises, and a test that patches a name here patches what
xrdsec calls.

It imports neither :mod:`ssl` nor ``cryptography``, so it stays on every cheap
import path (``tests/test_import_cost.py``). The Config-shaped adapters the
transfer code calls - ``_gridpaths``, ``_tokens``, ``_usercred`` - stay in
xrdclient, built on it.

New code imports :mod:`xrdsec.credentials`; this name is kept through 0.x. The
explicit re-exports are for type checkers, which do not see the swap.
"""

import sys

from xrdsec import credentials as _sec
from xrdsec.credentials import DEFAULT_CA_PATH as DEFAULT_CA_PATH
from xrdsec.credentials import GRID_SECURITY_DIRS as GRID_SECURITY_DIRS
from xrdsec.credentials import MAX_CREDENTIAL_SIZE as MAX_CREDENTIAL_SIZE
from xrdsec.credentials import TMP_DIR as TMP_DIR
from xrdsec.credentials import __all__ as __all__
from xrdsec.credentials import bearer_token_files as bearer_token_files
from xrdsec.credentials import default_ca_path as default_ca_path
from xrdsec.credentials import default_proxy_path as default_proxy_path
from xrdsec.credentials import find_bearer_token as find_bearer_token
from xrdsec.credentials import find_proxy as find_proxy
from xrdsec.credentials import find_user_credential as find_user_credential
from xrdsec.credentials import grid_ca_path as grid_ca_path
from xrdsec.credentials import grid_voms_dir as grid_voms_dir
from xrdsec.credentials import read_private_file as read_private_file
from xrdsec.credentials import write_private_file as write_private_file

sys.modules[__name__] = _sec
