"""The guard rails: what this client refuses to do to somebody by accident.

Everything here is a behaviour the stock XRootD tools do not have. They are
gathered in one file because they are one promise - that the obvious mistake
costs an error message rather than a machine, a dataset, or a night.
"""

from __future__ import annotations

import pytest

import xrdclient
from xrdclient.cli import confirm, interactive
from xrdclient.cli import cp as cp_cli
from xrdclient.cli import fs as fs_cli
from xrdclient.config import Config
from xrdclient.errors import TooLargeError
from xrdclient.testing import FakeDAVServer, FakeServer

BODY = b"hello world"


def run(argv, capsys):
    code = fs_cli.main(argv)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


# ---------------------------------------------------------------------------
# Reading a whole file into memory
# ---------------------------------------------------------------------------


@pytest.fixture
def small(server):
    """The fixture server, with a ceiling low enough to bump into."""
    with xrdclient.FileSystem(server.url, Config(max_read_size=4, auth_order=("host",))) as fs:
        yield fs


def test_a_read_that_never_said_how_much_it_wanted_is_bounded(small, server):
    """``read()`` on something big is the mistake nobody sees coming."""
    with pytest.raises(TooLargeError) as caught:
        small.read_bytes("/data/a.root")
    message = str(caught.value)
    assert "/data/a.root is 11 bytes" in message
    assert "over the 4 byte ceiling" in message
    assert "xrdclient.copy()" in message and "max_read_size" in message
    assert (caught.value.size, caught.value.limit) == (11, 4)


def test_asking_for_a_number_of_bytes_is_always_answered(small):
    """The ceiling is for reads that named no size; a size is a decision."""
    with small.open("/data/a.root", "rb") as handle:
        assert handle.read(11) == BODY
        handle.seek(0)
        assert handle.read(1 << 30) == BODY


def test_the_ceiling_can_be_lifted_and_then_the_whole_file_arrives(server):
    """A dataset that really is meant to be in memory only has to say so."""
    for limit in (0, 1 << 20):
        settings = Config(max_read_size=limit, auth_order=("host",))
        with xrdclient.FileSystem(server.url, settings) as fs:
            assert fs.read_bytes("/data/a.root") == BODY


def test_the_same_ceiling_holds_over_http(monkeypatch):
    """HTTP counts it as it arrives - there is no length to ask for first."""
    with FakeDAVServer(files={"/d/a.root": BODY}) as dav:
        config = Config(max_read_size=4, chunk_size=2, verify_tls=False)
        with xrdclient.FileSystem(dav.url, config) as fs:
            with pytest.raises(TooLargeError):
                fs.read_bytes("/d/a.root")
            with fs.open("/d/a.root", "rb") as handle:
                assert handle.read(4) == BODY[:4]


def test_a_ceiling_survives_being_pickled_like_every_other_error():
    """A worker process that hits it reports it back intact."""
    import pickle

    thrown = TooLargeError(1 << 40, 1 << 30, path="/store/huge.root")
    again = pickle.loads(pickle.dumps(thrown))
    assert (again.size, again.limit, again.path) == (1 << 40, 1 << 30, "/store/huge.root")
    assert str(again) == str(thrown)


def test_an_unbounded_read_of_something_that_fits_is_left_alone():
    """Under the ceiling nothing changes, including the empty file."""
    with FakeServer(files={"/e.bin": b"", "/tiny": b"ab"}) as srv:
        with xrdclient.FileSystem(srv.url, Config(max_read_size=4, auth_order=("host",))) as fs:
            assert fs.read_bytes("/e.bin") == b""
            assert fs.read_bytes("/tiny") == b"ab"


# ---------------------------------------------------------------------------
# Overwriting
# ---------------------------------------------------------------------------


def test_a_copy_does_not_overwrite_what_is_there_unless_told_to(server, tmp_path, capsys):
    """``cp`` over a file is a keystroke; asking for ``-f`` is a decision."""
    url = str(server.url)
    target = tmp_path / "out.root"
    target.write_bytes(b"keep")

    assert cp_cli.main([url + "data/a.root", str(target)]) == 1
    assert target.read_bytes() == b"keep"
    assert "xrd-cp:" in capsys.readouterr().err

    assert cp_cli.main(["-q", "-f", url + "data/a.root", str(target)]) == 0
    assert target.read_bytes() == BODY


def test_asking_both_to_overwrite_and_not_to_is_a_usage_error(server, tmp_path, capsys):
    assert cp_cli.main(["-f", "-n", str(server.url) + "data/a.root", str(tmp_path / "x")]) == 2
    assert "cannot both be what you meant" in capsys.readouterr().err


def test_continuing_a_transfer_still_writes_into_what_is_already_there(server, tmp_path, capsys):
    """``--continue`` is the one thing that needs the partial file kept."""
    target = tmp_path / "part.root"
    target.write_bytes(BODY[:4])
    assert cp_cli.main(["-q", "-c", str(server.url) + "data/a.root", str(target)]) == 0
    assert target.read_bytes() == BODY
    capsys.readouterr()


# ---------------------------------------------------------------------------
# Removing a tree
# ---------------------------------------------------------------------------


def test_a_recursive_remove_of_a_whole_namespace_is_refused(server, capsys):
    """``rm -r root://host//store`` is a slip, not an instruction."""
    server.add_file("/store/user/f.bin", b"x")
    for path in ("", "store"):
        code, _out, err = run(["rm", "-r", str(server.url) + path], capsys)
        assert code == 1
        assert "top of a namespace" in err
    assert server.contents("/store/user/f.bin") == b"x"


def test_the_refusal_can_be_overruled_by_somebody_who_means_it(server, capsys):
    server.add_file("/store/user/f.bin", b"x")
    assert run(["rm", "-r", "--yes", str(server.url) + "store"], capsys)[0] == 0
    assert "/store/user/f.bin" not in server.files


def test_a_terminal_is_asked_before_a_tree_goes(server, capsys, monkeypatch):
    """With somebody watching, the count of what is about to go is shown."""
    server.add_file("/data/tree/a.bin", b"x")
    server.add_file("/data/tree/b.bin", b"y")
    monkeypatch.setattr("xrdclient.cli.fs.interactive", lambda: True)

    monkeypatch.setattr("builtins.input", lambda: "n")
    code, _out, err = run(["rm", "-r", str(server.url) + "data/tree"], capsys)
    assert (code, "the 2 entries under it" in err) == (0, True)
    assert server.contents("/data/tree/a.bin") == b"x"  # still there

    monkeypatch.setattr("builtins.input", lambda: "yes")
    assert run(["rm", "-r", str(server.url) + "data/tree"], capsys)[0] == 0
    assert not [p for p in server.files if p.startswith("/data/tree")]


def test_a_batch_job_is_never_stopped_by_a_question_it_cannot_answer(server, capsys):
    """Nothing here waits on a prompt that no terminal is there to show."""
    server.add_file("/data/tree/a.bin", b"x")
    assert run(["rm", "-r", str(server.url) + "data/tree"], capsys)[0] == 0
    assert "/data/tree/a.bin" not in server.files


@pytest.mark.parametrize(
    ("answer", "agreed"), [("y", True), ("YES", True), ("n", False), ("", False)]
)
def test_only_yes_means_yes(answer, agreed, monkeypatch, capsys):
    monkeypatch.setattr("builtins.input", lambda: answer)
    assert confirm("really?") is agreed
    assert "really? [y/N]" in capsys.readouterr().err


def test_a_closed_input_means_no(monkeypatch, capsys):
    def gone():
        raise EOFError

    monkeypatch.setattr("builtins.input", gone)
    assert confirm("really?") is False
    capsys.readouterr()


def test_a_captured_stream_is_not_a_person(capsys):
    """Under pytest neither stream is a terminal, which is the point."""
    assert interactive() is False
    capsys.readouterr()
