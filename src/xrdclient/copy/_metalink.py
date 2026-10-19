"""Metalink sources: resolve the descriptor, then fail over between replicas.

:func:`~xrdclient.copy.engine.copy` hands a source here when
``config.metalink_processing`` is on and the source is a Metalink descriptor.
:func:`_copy_metalink` reads and parses it, picks the declared checksum, and
runs one ordinary copy per replica - :func:`~xrdclient.copy.engine._copy_one`,
with the declared checksum enforced - until one succeeds.

This is a client of the engine rather than part of it, and it calls back into
it: ``_copy_one`` and :class:`~xrdclient.copy.engine.CopyResult` are looked up
on :mod:`xrdclient.copy.engine` at call time (a function-level import), both
because the engine imports this module and because tests replace
``engine._copy_one`` to watch the failover.
"""

from __future__ import annotations

from collections.abc import Sequence
from contextlib import ExitStack
from dataclasses import replace
from typing import TYPE_CHECKING, Any, cast

from ..config import Config
from ..errors import XRootDError
from ..log import get_logger
from ..types import ChecksumInfo
from ..url import XRootDURL, parse
from ._endpoints import _reader, _remove, _target_url, _zip_member

if TYPE_CHECKING:
    from .engine import CopyResult, _CopyOptions

# The engine's logger, not this module's: these messages were always the copy
# engine's, and a logging configuration that names it should keep seeing them.
_log = get_logger("xrdclient.copy.engine")


def _copy_metalink(source: Any, target: Any, options: _CopyOptions) -> CopyResult:
    """Resolve one XrdCl-style Metalink source and fail over between replicas.

    ``options`` are the caller's :func:`~xrdclient.copy.engine.copy`
    controls. The descriptor itself is read here, and ``dry_run`` and
    ``remove_source`` apply to it - the catalogue, not a replica: a dry run
    opens no replica, and a move deletes the descriptor it was given.
    """
    config = options.config
    descriptor_url, catalog_url, selected_member, descriptor = _load_metalink(source, config)
    if options.dry_run:
        return _metalink_dry_run(descriptor_url, target, descriptor.size)
    checksum = _metalink_checksum(descriptor, selected_member, options.algorithm, config)
    # What every replica attempt shares: a declared checksum is always
    # verified, in its own algorithm, and no attempt removes its replica.
    each = replace(
        options,
        verify=True if checksum is not None else options.verify,
        algorithm=checksum.algorithm if checksum is not None else options.algorithm,
        dry_run=False,
        remove_source=False,
        declared=checksum,
    )
    result, replica = _copy_metalink_replicas(
        _metalink_urls(descriptor.replicas, config), selected_member, target, each
    )
    if options.remove_source:
        _remove(catalog_url, config)
    return replace(result, source=str(descriptor_url), replica=replica)


def _load_metalink(source: Any, config: Config) -> tuple[XRootDURL, XRootDURL, str | None, Any]:
    """Read and parse a descriptor, excluding the internal ZIP selector."""
    from ..metalink import MAX_DESCRIPTOR_SIZE, parse_metalink

    descriptor_url = _target_url(source)
    assert descriptor_url is not None
    selected_member = _zip_member(descriptor_url)
    catalog_url = descriptor_url.evolve(
        query={name: value for name, value in descriptor_url.query.items() if name != "xrdcl.unzip"}
    )
    with ExitStack() as stack:
        reader, _ = _reader(catalog_url, config, stack)
        document = reader.read(MAX_DESCRIPTOR_SIZE + 1)
    return (
        descriptor_url,
        catalog_url,
        selected_member,
        parse_metalink(document, base_url=str(catalog_url)),
    )


def _metalink_dry_run(descriptor_url: XRootDURL, target: Any, size: int | None) -> CopyResult:
    """Describe a Metalink copy without opening any concrete replica."""
    from . import engine

    target_url = _target_url(target)
    target_name = str(target_url) if target_url is not None else repr(target)
    return engine.CopyResult(str(descriptor_url), target_name, size or 0, 0.0)


def _metalink_checksum(
    descriptor: Any,
    selected_member: str | None,
    algorithm: str | None,
    config: Config,
) -> ChecksumInfo | None:
    """Choose metadata integrity, respecting ZIP's opt-in semantics."""
    available = selected_member is None or config.zip_metalink_checksum
    if not available:
        return None
    if algorithm is None:
        return cast("ChecksumInfo | None", descriptor.checksum)
    wanted = algorithm.lower().replace("-", "")
    return next((item for item in descriptor.checksums if item.algorithm == wanted), None)


def _metalink_urls(replicas: Sequence[str], config: Config) -> tuple[str, ...]:
    """Apply the optional transport upgrade once, before failover."""
    if not config.tls_metalink:
        return tuple(replicas)
    return tuple(_secure_metalink_url(url) for url in replicas)


def _copy_metalink_replicas(
    replicas: Sequence[str],
    selected_member: str | None,
    target: Any,
    options: _CopyOptions,
) -> tuple[CopyResult, str]:
    """Try concrete replicas in order, retaining the final useful error.

    Only the first attempt honours ``overwrite=False``: a later one may find
    the half-written target an earlier replica left, which is this copy's
    own and must be replaced. Every attempt but the last also has its
    server-requested waits capped (see :func:`_metalink_replica_config`),
    since another replica is a better use of the time than waiting.
    """
    from .replicas import NoMoreReplicasError

    failures: list[Exception] = []
    for index, replica in enumerate(replicas):
        attempt = replace(
            options,
            config=_metalink_replica_config(options.config, another=index + 1 < len(replicas)),
            overwrite=options.overwrite if index == 0 else True,
        )
        try:
            result = _copy_metalink_replica(replica, selected_member, target, attempt)
        except (OSError, XRootDError) as exc:
            failures.append(exc)
            _log.debug("Metalink replica %s failed: %s", replica, exc)
            continue
        return result, replica
    last = failures[-1] if failures else None
    detail = f": {last}" if last is not None else ""
    raise NoMoreReplicasError(f"Metalink: no more replicas to try{detail}") from last


def _copy_metalink_replica(
    replica: str, selected_member: str | None, target: Any, options: _CopyOptions
) -> CopyResult:
    """Run one ordinary copy with the catalogue's endpoint and integrity.

    ``options`` are already this attempt's (see :func:`_copy_metalink_replicas`).
    """
    from . import engine  # the engine imports this module; see the docstring

    source = _metalink_endpoint(replica, selected_member)
    return engine._copy_one(source, target, options)


def _metalink_endpoint(replica: str, selected_member: str | None) -> str | XRootDURL:
    """Apply a selected ZIP member without losing local-URL query state."""
    if selected_member is None:
        return replica
    return parse(replica).with_query(**{"xrdcl.unzip": selected_member})


def _metalink_replica_config(config: Config, *, another: bool) -> Config:
    """Limit server-requested waits only while failover remains possible."""
    if not another:
        return config
    limit = max(config.max_metalink_wait, 0.0)
    if config.wait_budget > 0:
        limit = min(limit, config.wait_budget)
    # ``wait_budget=0`` means unlimited, whereas XrdCl's
    # ``MaxMetalinkWait=0`` means fail over on the first positive wait.
    return config.evolve(wait_budget=limit or 1e-9)


def _secure_metalink_url(url: str) -> str:
    """``root://``/``xroot://`` as ``roots://``/``xroots://``; other schemes unchanged.

    XrdCl's ``TlsMetalink``: replicas a metalink lists inherit no TLS
    requirement from the URL that named the metalink, so with the setting
    on, plaintext XRootD replicas are upgraded. HTTP replicas are left as
    the catalogue wrote them - rewriting ``http`` to ``https`` would change
    the port, not just the transport.
    """
    parsed = parse(url)
    if parsed.scheme in ("root", "xroot"):
        return str(parsed.evolve(scheme=parsed.scheme + "s"))
    return url
