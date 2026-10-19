"""``fsspec.spec.AbstractFileSystem``: the members the adapter relies on."""

from collections.abc import MutableMapping
from typing import Any

class AbstractFileSystem:
    protocol: str | tuple[str, ...]
    root_marker: str
    sep: str
    blocksize: int
    dircache: MutableMapping[str, Any]
    def __init__(self, *args: Any, **storage_options: Any) -> None: ...
    @classmethod
    def clear_instance_cache(cls) -> None: ...
    def unstrip_protocol(self, name: str) -> str: ...
    def invalidate_cache(self, path: str | None = None) -> None: ...
    def info(self, path: str, **kwargs: Any) -> dict[str, Any]: ...
    def open(
        self,
        path: str,
        mode: str = "rb",
        block_size: int | None = None,
        cache_options: dict[str, Any] | None = None,
        compression: str | None = None,
        **kwargs: Any,
    ) -> Any: ...
