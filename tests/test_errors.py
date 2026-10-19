from __future__ import annotations

import errno
import pickle

import pytest

from xrdclient import errors as e


@pytest.mark.parametrize(
    "code, cls, builtin",
    [
        (e.kXR_NotFound, e.NotFoundError, FileNotFoundError),
        (e.kXR_NotAuthorized, e.PermissionError_, PermissionError),
        (e.kXR_isDirectory, e.IsADirectoryError_, IsADirectoryError),
        (e.kXR_NoSpace, e.NoSpaceError, OSError),
        (e.kXR_FSError, e.IOError_, OSError),
        (e.kXR_Unsupported, e.UnsupportedError, OSError),
        (e.kXR_InvalidRequest, e.InvalidArgumentError, OSError),
        (e.kXR_SigVerErr, e.PermissionError_, PermissionError),
        (e.kXR_DecryptErr, e.PermissionError_, PermissionError),
        (e.kXR_BadPayload, e.InvalidArgumentError, OSError),
        (e.kXR_noReplicas, e.NotFoundError, FileNotFoundError),
        (e.kXR_ReqTimedOut, e.ServerTimeoutError, TimeoutError),
        (e.kXR_TimerExpired, e.ServerTimeoutError, TimeoutError),
    ],
)
def test_server_codes_map_to_builtin_oserrors(code, cls, builtin):
    """A caller that only knows ``FileNotFoundError`` still catches ours."""
    with pytest.raises(builtin) as info:
        e.raise_for_status(code, "nope", path="/a/b")
    assert isinstance(info.value, cls)
    assert isinstance(info.value, e.ServerError)


def test_oserror_attributes_are_populated():
    with pytest.raises(e.NotFoundError) as info:
        e.raise_for_status(e.kXR_NotFound, "no such file", path="/a/b")
    exc = info.value
    assert isinstance(exc, FileNotFoundError)
    assert exc.errno == errno.ENOENT
    assert exc.filename == "/a/b"
    assert "no such file" in str(exc)
    assert exc.code == e.kXR_NotFound


def test_file_exists_maps_to_the_builtin():
    with pytest.raises(FileExistsError):
        e.raise_for_status(e.kXR_ItExists, "already there", path="/a")


def test_unknown_codes_still_raise_a_server_error():
    with pytest.raises(e.ServerError) as info:
        e.raise_for_status(31337, "mystery")
    assert info.value.code == 31337


def test_ok_does_not_raise():
    e.raise_for_status(0, "")  # returns rather than raising


def test_everything_descends_from_xrootderror():
    for cls in (
        e.ProtocolError,
        e.ConnectionError,
        e.TimeoutError,
        e.TransientError,
        e.AuthenticationError,
        e.RedirectLimitError,
        e.ChecksumMismatchError,
        e.ServerError,
    ):
        assert issubclass(cls, e.XRootDError)


def test_connection_and_timeout_are_also_builtins():
    assert issubclass(e.ConnectionError, OSError)
    assert issubclass(e.TimeoutError, OSError)


def test_no_mechanism_error_reports_what_was_offered_and_why():
    exc = e.NoMechanismError(offered=["gsi", "unix"], tried={"gsi": "no proxy"})
    assert exc.offered == ["gsi", "unix"]
    assert exc.tried == {"gsi": "no proxy"}
    assert "gsi" in str(exc)


def test_checksum_mismatch_shows_both_values():
    exc = e.ChecksumMismatchError("adler32", "deadbeef", "cafebabe")
    assert exc.algorithm == "adler32"
    assert "deadbeef" in str(exc) and "cafebabe" in str(exc)


def test_transient_error_records_progress():
    exc = e.TransientError("gave up", attempts=4, committed=8192)
    assert (exc.attempts, exc.committed) == (4, 8192)


@pytest.mark.parametrize(
    "exc",
    [
        e.NotFoundError(e.kXR_NotFound, "gone", path="/p"),
        e.ServerError(e.kXR_ServerError, "boom", path="/p"),
        e.TransientError("gave up", attempts=2, committed=7),
        e.NoMechanismError(offered=["gsi"], tried={"gsi": "no proxy"}),
        e.ChecksumMismatchError("adler32", "aa", "bb"),
    ],
)
def test_errors_survive_pickling(exc):
    """Process pools re-raise in the parent; the payload must travel intact."""
    back = pickle.loads(pickle.dumps(exc))
    assert type(back) is type(exc)
    assert str(back) == str(exc)


def test_pickled_server_error_keeps_code_and_path():
    back = pickle.loads(pickle.dumps(e.NotFoundError(e.kXR_NotFound, "gone", path="/p")))
    assert isinstance(back, e.ServerError) and isinstance(back, FileNotFoundError)
    assert (back.code, back.path, back.errno) == (e.kXR_NotFound, "/p", errno.ENOENT)


def test_a_server_timeout_is_caught_by_the_same_except_as_a_client_one():
    """ "It was slow" reads the same to a caller whichever end gave up first,
    and both are worth retrying."""
    with pytest.raises(e.TimeoutError) as info:
        e.raise_for_status(e.kXR_ReqTimedOut, "took too long", path="/a")
    assert isinstance(info.value, e.TransientError)
    assert info.value.errno == errno.ETIMEDOUT
    assert info.value.filename == "/a"
    # And it names the code, as every other server error does, rather than
    # falling back to OSError's "[Errno 110] took too long".
    assert str(info.value).startswith("The server request timed out.")
    assert "XRootD error 3034" in str(info.value)
    assert isinstance(info.value, e.ServerError) and info.value.message == "took too long"


def test_tls_required_names_the_fix():
    """ "Permission denied" would send someone to chmod; the real fix is one
    scheme change, so the message says which."""
    with pytest.raises(PermissionError) as info:
        e.raise_for_status(e.kXR_TLSRequired, "TLS required", path="/store/f")
    assert isinstance(info.value, e.TLSRequiredError)
    assert info.value.errno == errno.EACCES
    assert "roots://" in str(info.value)
    assert str(info.value).startswith("The server requires an encrypted connection.")
    assert info.value.message == "TLS required"
    assert info.value.path == "/store/f"


def test_tls_required_survives_pickling():
    back = pickle.loads(pickle.dumps(e.TLSRequiredError(e.kXR_TLSRequired, "TLS", path="/p")))
    assert isinstance(back, e.TLSRequiredError)
    assert (back.code, back.path, back.errno) == (e.kXR_TLSRequired, "/p", errno.EACCES)
    assert str(back).startswith("The server requires an encrypted connection.")


def test_every_error_code_the_protocol_defines_has_a_name():
    """``kXR_ArgInvalid`` (3000) through ``kXR_TimerExpired`` (3035), the last
    before XProtocol.hh's kXR_ERRFENCE - a gap here prints as a number."""
    for code in range(3000, 3036):
        assert not e._CODE_NAMES[code].startswith("kXR_unknown")


# ---------------------------------------------------------------------------
# The join with xrdcore.errors (docs/errors.md)
# ---------------------------------------------------------------------------

#: xrdclient's name -> the xrdcore class it *is*.
SHARED = {
    "XRootDError": "XrdError",
    "ProtocolError": "ProtocolError",
    "MetalinkError": "MetalinkError",
    "ConnectionError": "ConnectionError",
    "TransientError": "TransientError",
    "TimeoutError": "TimeoutError",
    "AuthenticationError": "AuthenticationError",
    "CredentialError": "CredentialError",
    "TokenExpiredError": "TokenExpiredError",
    "InsecureCredentialError": "InsecureCredentialError",
}


@pytest.mark.parametrize(("ours", "theirs"), sorted(SHARED.items()))
def test_the_shared_vocabulary_is_xrdcores_own_classes(ours, theirs):
    from xrdcore import errors as core

    assert getattr(e, ours) is getattr(core, theirs)


@pytest.mark.parametrize("name", sorted(set(SHARED) - {"XRootDError"}))
def test_what_a_library_raises_is_caught_by_the_client_name(name):
    """``except xrdclient.errors.X`` catches the X xrdcore, xrdsec or xrdhttp raise."""
    from xrdcore import errors as core

    library_error = getattr(core, SHARED[name])
    raised = (
        library_error("proxy is world-readable", "/tmp/x509up_u1")
        if name == "InsecureCredentialError"
        else library_error("from a library")
    )
    with pytest.raises(getattr(e, name)):
        raise raised
    with pytest.raises(e.XRootDError):
        raise raised


def test_the_client_only_errors_join_the_shared_base():
    """XRootD's own errors are what ``except xrdcore.errors.XrdError`` catches too."""
    from xrdcore import errors as core

    for cls in (e.ServerError, e.NotFoundError, e.AbortedError, e.NoMechanismError):
        assert issubclass(cls, core.XrdError)
    assert issubclass(e.ServerTimeoutError, core.TimeoutError)
    assert issubclass(e.WaitLimitError, core.TransientError)
    assert issubclass(e.ServerTimeoutError, TimeoutError)  # still the builtin as well
    with pytest.raises(core.TimeoutError):
        e.raise_for_status(e.kXR_ReqTimedOut, "slow", path="/a")


def test_a_shared_error_pickles_with_its_state():
    """``InsecureCredentialError.path`` survives a process boundary (it did not before 0.4.0)."""
    error = e.InsecureCredentialError("too open", "/tmp/x509up_u1")
    back = pickle.loads(pickle.dumps(error))
    assert type(back) is e.InsecureCredentialError
    assert (str(back), back.path) == ("too open", "/tmp/x509up_u1")
    transient = pickle.loads(pickle.dumps(e.TimeoutError("late", attempts=3, committed=9)))
    assert (transient.attempts, transient.committed) == (3, 9)
