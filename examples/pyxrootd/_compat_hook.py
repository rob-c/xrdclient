"""Make ``import XRootD`` resolve to xrdclient's compat layer (see ``install_hook.py``).

Imported for its effect, before anything imports the real bindings: in a
program that cannot be edited this is the one import its entry point gains.
"""

import xrdclient.compat

xrdclient.compat.install()
