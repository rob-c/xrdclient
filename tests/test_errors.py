from __future__ import annotations

import errno
import pickle

import pytest

from xrd import errors as e


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
    assert e.raise_for_status(0, "") is None


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
    assert isinstance(back, FileNotFoundError)
    assert (back.code, back.path, back.errno) == (e.kXR_NotFound, "/p", errno.ENOENT)


def test_a_server_timeout_is_caught_by_the_same_except_as_a_client_one():
    """"It was slow" reads the same to a caller whichever end gave up first,
    and both are worth retrying."""
    with pytest.raises(e.TimeoutError) as info:
        e.raise_for_status(e.kXR_ReqTimedOut, "took too long", path="/a")
    assert isinstance(info.value, e.TransientError)
    assert info.value.errno == errno.ETIMEDOUT
    assert info.value.filename == "/a"
    # And it names the code, as every other server error does, rather than
    # falling back to OSError's "[Errno 110] took too long".
    assert str(info.value) == "kXR_ReqTimedOut: took too long [/a]"


def test_tls_required_names_the_fix():
    """"Permission denied" would send someone to chmod; the real fix is one
    scheme change, so the message says which."""
    with pytest.raises(PermissionError) as info:
        e.raise_for_status(e.kXR_TLSRequired, "TLS required", path="/store/f")
    assert isinstance(info.value, e.TLSRequiredError)
    assert info.value.errno == errno.EACCES
    assert "roots://" in str(info.value)
    assert str(info.value).startswith("kXR_TLSRequired: TLS required [/store/f]")


def test_tls_required_survives_pickling():
    back = pickle.loads(pickle.dumps(e.TLSRequiredError(e.kXR_TLSRequired, "TLS", path="/p")))
    assert isinstance(back, PermissionError)
    assert (back.code, back.path, back.errno) == (e.kXR_TLSRequired, "/p", errno.EACCES)
    assert str(back).startswith("kXR_TLSRequired")


def test_every_error_code_the_protocol_defines_has_a_name():
    """``kXR_ArgInvalid`` (3000) through ``kXR_TimerExpired`` (3035), the last
    before XProtocol.hh's kXR_ERRFENCE - a gap here prints as a number."""
    for code in range(3000, 3036):
        assert not e._CODE_NAMES[code].startswith("kXR_unknown")
