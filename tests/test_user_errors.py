"""User-oriented descriptions without losing wire codes or server detail."""

from __future__ import annotations

import pickle

import pytest

from xrdclient import errors


@pytest.mark.parametrize("code", sorted(errors._USER_ERRORS))
@pytest.mark.parametrize("detail", ["", "specific server explanation"])
def test_every_server_code_has_a_clear_summary_and_unchanged_payload(code, detail):
    error = errors.ServerError(code, detail, path="/store/data.root")
    message = str(error)
    assert error.code == code
    assert error.message == detail
    assert error.path == "/store/data.root"
    assert message.startswith(errors._USER_ERRORS[code])
    assert "kXR_" not in message
    assert f"XRootD error {code}" in message
    assert "/store/data.root" in message
    assert detail in message
    assert len(errors._USER_ERRORS[code]) < 120


@pytest.mark.parametrize("code", sorted(errors._USER_ERRORS))
@pytest.mark.parametrize("detail", ["", "specific server explanation"])
def test_friendly_server_errors_pickle_without_losing_details(code, detail):
    error = errors.ServerError(code, detail, path="/store/data.root")
    recovered = pickle.loads(pickle.dumps(error))
    assert (recovered.code, recovered.message, recovered.path) == (code, detail, error.path)
    assert str(recovered) == str(error)


def test_unknown_server_failure_does_not_guess_a_cause():
    error = errors.ServerError(9876, "unknown server response")
    assert str(error).startswith("The server could not complete the request.")
    assert "9876" in str(error)
    assert error.message == "unknown server response"


def test_optional_login_hint_is_still_visible():
    error = errors.ServerError(errors.kXR_NotAuthorized, "access denied")
    error.explain("Get a new proxy for this service.")
    assert "Get a new proxy" in str(error)
    assert error.message == "access denied"
