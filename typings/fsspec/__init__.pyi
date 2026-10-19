"""Stubs for the part of fsspec this project uses (fsspec ships no types).

Signatures follow fsspec's own source; anything not used here is left out,
so a new use shows up as a mypy error rather than as an unchecked ``Any``.
"""

from typing import Any

from .core import OpenFile as OpenFile
from .spec import AbstractFileSystem as AbstractFileSystem

def open(
    urlpath: str,
    mode: str = "rb",
    compression: str | None = None,
    encoding: str = "utf8",
    errors: str | None = None,
    protocol: str | None = None,
    newline: str | None = None,
    expand: bool | None = None,
    **kwargs: Any,
) -> OpenFile: ...
def get_filesystem_class(protocol: str) -> type[AbstractFileSystem]: ...
def register_implementation(
    name: str,
    cls: type[AbstractFileSystem] | str,
    clobber: bool = False,
    errtxt: str | None = None,
) -> None: ...
