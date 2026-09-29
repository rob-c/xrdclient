#!/usr/bin/env python3
"""Client settings by XrdCl's names: EnvPutInt, EnvGetInt, EnvPutString, EnvGetString.

A value put here applies to every FileSystem and File made afterwards. As with
XrdCl, a setting already in the shell as ``XRD_<NAME>`` wins: the put returns
``False`` and the shell's value is what is read back.

Usage: python env_settings.py [root://host:port] [/working/dir]
"""

from __future__ import annotations

import os
import sys

from xrdclient.compat import client  # was: from XRootD import client

URL = sys.argv[1] if len(sys.argv) > 1 else "root://localhost:1094"
WORK = sys.argv[2] if len(sys.argv) > 2 else "/tmp/pyxrootd-examples"

# The runner's shell must not already set these, or the puts below are refused.
for name in ("XRD_REQUESTTIMEOUT", "XRD_CPCHUNKSIZE", "XRD_REDIRECTLIMIT"):
    assert name not in os.environ, name

print("defaults:")
for key in ("RequestTimeout", "ConnectionWindow", "ConnectionRetry", "RedirectLimit"):
    print(f"   {key} = {client.EnvGetInt(key)}")

print("put RequestTimeout 120:", client.EnvPutInt("RequestTimeout", 120))
print("put RedirectLimit 4:", client.EnvPutInt("RedirectLimit", 4))
print("RequestTimeout now", client.EnvGetInt("RequestTimeout"))
print("RedirectLimit now", client.EnvGetInt("RedirectLimit"))

# The settings are in force: a call made now uses them.
status, _ = client.FileSystem(URL).ping()
print("ping with the new settings:", status.ok)
