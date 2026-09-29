#!/usr/bin/env python3
"""Take URLs apart with client.URL: protocol, user, host, port, path, parameters.

Usage: python url_parsing.py [root://host:port] [/working/dir]
"""

from __future__ import annotations

import sys

from xrdclient.compat import client  # was: from XRootD import client

URL = sys.argv[1] if len(sys.argv) > 1 else "root://localhost:1094"
WORK = sys.argv[2] if len(sys.argv) > 2 else "/tmp/pyxrootd-examples"

EXAMPLES = [
    "root://eos.example.org//eos/user/f.root",
    "root://alice:secret@door.example.org:1095//store/f.root?svcClass=t0&x=1",
    "roots://door.example.org//store/tls.root",
    "xroot://door.example.org:2094/relative/path",
    "file:///local/file.txt",
    "/just/a/local/path",
    "not a url at all",
]

for text in EXAMPLES:
    url = client.URL(text)
    print(text)
    print(f"   valid={url.is_valid()} protocol={url.protocol!r} hostid={url.hostid!r}")
    print(f"   user={url.username!r} password={url.password!r}")
    print(f"   host={url.hostname!r} port={url.port} path={url.path!r}")
    print(f"   path_with_params={url.path_with_params!r}")

# A FileSystem reports the URL it was made with.
fs = client.FileSystem(URL)
print(
    "FileSystem url is a URL:",
    type(fs.url).__name__,
    "port matches",
    fs.url.port == int(URL.rsplit(":", 1)[1]),
)
