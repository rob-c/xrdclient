"""A mis-typed option or command in ``xrd-cp``, ``xrd-fs`` and ``xrd-proxy``.

The suggestion itself lives in :mod:`xrdcore.suggest` and is tested there; this
is the three commands on the stack answering through their own entry points,
which is what a user actually types.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from xrdclient.cli import cp, fs, proxy


def refused(capsys: pytest.CaptureFixture[str], command: Callable[[], int]) -> str:
    """The stderr a command prints for a usage error, which must be exit code 2."""
    with pytest.raises(SystemExit) as stopped:
        command()
    assert stopped.value.code == 2
    return capsys.readouterr().err


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (["--quiett", "a", "b"], "'--quiet'"),
        (["--verfy", "a", "b"], "'--verify'"),
        (["--streamz", "4", "a", "b"], "'--streams'"),
    ],
)
def test_xrd_cp_names_the_option_that_was_meant(
    capsys: pytest.CaptureFixture[str], argv: list[str], expected: str
) -> None:
    assert expected in refused(capsys, lambda: cp.main(argv))


@pytest.mark.parametrize(
    ("typo", "meant"),
    [("statt", "stat"), ("cheksum", "checksum"), ("rmdirr", "rmdir")],
)
def test_xrd_fs_names_the_command_that_was_meant(
    capsys: pytest.CaptureFixture[str], typo: str, meant: str
) -> None:
    assert f"did you mean '{meant}'?" in refused(capsys, lambda: fs.main([typo]))


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (["stat", "--quiett", "root://h//f"], "'--quiet'"),
        (["stat", "--verboze", "root://h//f"], "'--verbose'"),
    ],
)
def test_xrd_fs_names_the_option_that_was_meant(
    capsys: pytest.CaptureFixture[str], argv: list[str], expected: str
) -> None:
    assert expected in refused(capsys, lambda: fs.main(argv))


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        # the top-level parser reports a subcommand's option, so it must know them
        (["init", "-vaild", "1:00"], "'-valid'"),
        (["inti"], "'init'"),
        (["init", "--profile", "arcx"], "'arc'"),
    ],
)
def test_xrd_proxy_names_the_name_that_was_meant(
    capsys: pytest.CaptureFixture[str], argv: list[str], expected: str
) -> None:
    assert f"did you mean {expected}?" in refused(capsys, lambda: proxy.main(argv))


def test_nothing_is_offered_for_a_name_that_resembles_nothing(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert "did you mean" not in refused(capsys, lambda: proxy.main(["--zzzz"]))
