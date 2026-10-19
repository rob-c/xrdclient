"""The fake XRootD server's request handlers, grouped by what they serve.

:mod:`xrdclient.testing.server` is the facade and the part a test touches:
:class:`~xrdclient.testing.server.FakeServer` (the in-memory state and the
listening socket), ``_Connection`` (one client link: the handshake, framing,
redirect and wait injection, and dispatch), the ``_HANDLERS`` table that maps
each opcode to a handler, and the ``python -m xrdclient.testing`` entry point.
Everything a test imports from ``xrdclient.testing.server`` - public or
private - is still importable from there.

The handlers themselves live here, one module per area of the protocol, so a
maintainer looking for "what does the fake answer to ``kXR_dirlist``" opens
one short file instead of scrolling a long one:

``wire``
    Frame builders and the small shared vocabulary every handler uses
    (``frame``/``error``/``pgwrite_cse``, ``_clean``, ``_NotFound``). A leaf:
    it imports nothing else from the fake.
``session``
    Protocol negotiation, login, authentication rounds, ``kXR_bind``, ping,
    ``kXR_endsess`` and ``kXR_set``.
``namespace``
    Stat, directory listing, mkdir/rm/rmdir/mv/chmod/truncate, and the
    vendor namespace extensions (symlink, setattr, link, readlink).
``files``
    Open (with its creation and template-colocation rules), close, and all
    data I/O: read, write, sync, vector, clone, paged I/O and checkpoints.
``queries``
    ``kXR_query`` and its subtypes (checksum, config, space, prepare status,
    Fcntl), ``kXR_locate`` and ``kXR_prepare``.
``xattrs``
    ``kXR_fattr``: list, get, set and delete of extended attributes.

Every handler has the same signature, ``(connection, streamid, params,
body) -> Iterator[bytes]``, and yields raw response frames. A handler module
never imports the facade at module level, so the modules load in any order;
the few names tests monkeypatch on the facade (``_checksum``,
``CHECKPOINT_CAPACITY``, and the ``_HANDLERS`` table a checkpoint re-enters)
are looked up through :mod:`xrdclient.testing.server` at call time, which is
what keeps such a patch effective.
"""
