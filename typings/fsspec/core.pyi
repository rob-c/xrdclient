"""``fsspec.core.OpenFile``: the context manager :func:`fsspec.open` returns."""

from types import TracebackType
from typing import Any

class OpenFile:
    # What it yields is the filesystem's own file object, whatever that is.
    def __enter__(self) -> Any: ...
    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None: ...
