"""An S3 endpoint that runs in the test process.

:class:`FakeS3Server` is to :mod:`xrdclient.s3` what :class:`FakeDAVServer` is to
:mod:`xrdclient.http`: enough of an object store - ``ListObjectsV2`` with a
delimiter and continuation tokens, ranged ``GET``, ``PUT``, server-side copy,
multipart upload, ``DELETE`` - to test a client against, with knobs for the
cases a real bucket will not produce on demand.

    >>> with FakeS3Server(objects={"d/a.root": b"hello"}) as srv:
    ...     xrdclient.s3.S3FileSystem(srv.url, endpoint=srv.endpoint).listdir("/d")
    ['a.root']

Since 0.4.0 the server is xrdhttp's shared fake storage element,
:class:`xrdhttp.testing.StorageServer`, with its S3 dialect on (the one
xgfalclient's suite runs against too). Signatures are checked there rather
than trusted, written out again from the specification against the request
as it arrived on the wire, so a client that signs the wrong string is
caught rather than agreed with. :class:`FakeS3Server` is the thin preset
that keeps this package's names: one bucket, its objects as a plain dict
(:attr:`~FakeS3Server.objects`), the request log as
:attr:`~FakeS3Server.seen`, and the dialect's knobs (``forbidden``,
``complete_fails``, ``failing_parts``, ...) as attributes of the server.

The server is path-style only (``http://host:port/bucket/key``): the client
chooses path style whenever it is given an explicit endpoint, which is how
every test reaches this server. Every object claims the same modification
time, so listings and stats are reproducible.
"""

from __future__ import annotations

from typing import Generic, Self, TypeVar, overload

from xrdhttp.testing import S3, MemoryStore, StorageServer

from ..url import XRootDURL, parse

__all__ = ["FakeS3Server"]

#: The stamp every object claims to have been written at (2024-01-02T03:04:05Z).
_MODIFIED_EPOCH = 1704164645

_T = TypeVar("_T")


class _Knob(Generic[_T]):
    """One field of the server's :class:`~xrdhttp.testing.S3` state under its flat name.

    The dialect's state lives on :attr:`FakeS3Server.s3`, shared with every
    other client's preset; this keeps ``server.forbidden = True`` meaning
    what it always has.
    """

    def __set_name__(self, owner: type, name: str) -> None:
        """Remember which field this is: the attribute's own name."""
        self.name = name

    @overload
    def __get__(self, server: None, owner: type) -> Self: ...

    @overload
    def __get__(self, server: FakeS3Server, owner: type) -> _T: ...

    def __get__(self, server: FakeS3Server | None, owner: type) -> Self | _T:
        """The field's value (the descriptor itself, read from the class)."""
        if server is None:
            return self
        value: _T = getattr(server.dialect, self.name)
        return value

    def __set__(self, server: FakeS3Server, value: _T) -> None:
        """Set the field."""
        setattr(server.dialect, self.name, value)


class FakeS3Server(StorageServer):
    """An S3 endpoint on loopback, holding one bucket, populated from a dict.

    State lives in plain public attributes - :attr:`objects`, :attr:`seen`,
    :attr:`uploads` - so a test arranges a scenario by assigning to them and
    checks the outcome by reading them back; the fault knobs (``forbidden``,
    ``complete_fails``, ``failing_parts`` ...) are attributes for the same
    reason, and may be flipped while the server runs. The port is bound
    lazily and released by :meth:`stop`; the serving threads are daemons, so
    a test that forgets to stop the server cannot hang the interpreter on exit.
    """

    #: What a signature must be made with; ``access_key = ""`` accepts anything.
    access_key: _Knob[str] = _Knob()
    secret_key: _Knob[str] = _Knob()
    #: Keys per listing page, which is how a test reaches the second one.
    page_size: _Knob[int] = _Knob()
    #: Multipart uploads in flight: id to ``{part number: bytes}``.
    uploads: _Knob[dict[str, dict[int, bytes]]] = _Knob()
    #: Uploads that were abandoned, so a test can see that they were.
    aborted: _Knob[list[str]] = _Knob()
    #: Begin a multipart upload without naming it, as nothing sane does.
    nameless_uploads: _Knob[bool] = _Knob()
    #: Report a failure in the body of an otherwise successful completion.
    complete_fails: _Knob[bool] = _Knob()
    #: Part numbers to refuse with a 500, once each.
    failing_parts: _Knob[set[int]] = _Knob()
    #: Report a failure in the body of an otherwise successful copy.
    copy_fails: _Knob[bool] = _Knob()
    #: Refuse every request, whatever it is signed with.
    forbidden: _Knob[bool] = _Knob()

    def __init__(
        self,
        *,
        bucket: str = "test-bucket",
        objects: dict[str, bytes] | None = None,
        access_key: str = "AKIAIOSFODNN7EXAMPLE",
        secret_key: str = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
        host: str = "127.0.0.1",
        port: int = 0,
    ) -> None:
        """Hold ``objects`` in ``bucket``; nothing is bound until first use.

        The default keys are the documentation example pair AWS publishes,
        so a signature computed here can be compared against the worked
        examples in the SigV4 specification. ``access_key=""`` turns the
        signature check off for tests that are not about authentication.
        Any region a signature's scope names is accepted.
        """
        self._memory = MemoryStore(buckets={bucket: dict(objects or {})}, mtime=_MODIFIED_EPOCH)
        super().__init__(self._memory, host=host, port=port, name="xrdclient-fake-s3")
        #: The one bucket this endpoint serves.
        self.bucket = bucket
        self.s3 = S3(access_key=access_key, secret_key=secret_key, region=None)

    @property
    def dialect(self) -> S3:
        """The S3 state the knobs are fields of."""
        assert self.s3 is not None  # set in __init__; the preset is always S3
        return self.s3

    @property
    def objects(self) -> dict[str, bytes]:
        """Key to contents. Mutated by the client, readable (and writable) by the test."""
        return self._memory.buckets[self.bucket]

    @property
    def etags(self) -> dict[str, str]:
        """Key to ``ETag`` for the objects that arrived as a multipart upload."""
        prefix = f"{self.bucket}/"
        tags = self.dialect.etags
        return {name[len(prefix) :]: tag for name, tag in tags.items() if name.startswith(prefix)}

    @property
    def seen(self) -> list[tuple[str, str, str]]:
        """``(method, path, query)`` for every request served, in order, as sent."""
        seen = []
        for request in list(self.requests):
            path, _, query = request.target.partition("?")
            seen.append((request.method, path, query))
        return seen

    def contents(self, path: str) -> bytes:
        """The object stored at key ``path``; a leading ``/`` is forgiven, as paths carry one."""
        return self.objects[path.lstrip("/")]

    @property
    def endpoint(self) -> str:
        """``http://host:port`` - what :class:`~xrdclient.s3.S3FileSystem` talks to."""
        return self.base

    @property
    def url(self) -> XRootDURL:
        """``s3://bucket/`` - what the bucket is called."""
        return parse(f"s3://{self.bucket}/")

    def __repr__(self) -> str:
        """Where it listens and how much it holds, without binding a port to say so."""
        where = self._where()
        return f"FakeS3Server({where}, bucket={self.bucket!r}, objects={len(self.objects)})"
