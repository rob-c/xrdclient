"""``fsspec.caching``: the cache classes and their registry."""

from collections.abc import Callable
from typing import Any

Fetcher = Callable[[int, int], bytes]

class BaseCache:
    name: str
    blocksize: int
    fetcher: Fetcher
    size: int
    def __init__(self, blocksize: int, fetcher: Fetcher, size: int, **kwargs: Any) -> None: ...
    def _fetch(self, start: int | None, stop: int | None) -> bytes: ...

caches: dict[str | None, type[BaseCache]]
