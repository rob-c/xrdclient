"""The programs in ``examples/`` are documentation that runs.

PyTorch is not installed here, so they cannot be run in this suite; what can be
checked without it is that they parse, that every name they take out of this
library is still a name this library has - which is what would rot first - and
that the page describing them describes all of them.
"""

from __future__ import annotations

import ast
import importlib
import pathlib

import pytest

REPO = pathlib.Path(__file__).parent.parent
EXAMPLES = sorted((REPO / "examples").glob("*.py"))


def test_every_example_is_one_of_the_documented_playbooks_and_parses():
    trees = {path.name: ast.parse(path.read_text()) for path in EXAMPLES}
    assert sorted(trees) == [
        "cifar10_autoencoder.py",
        "fashion_mnist_cnn.py",
        "jarvis_2d_visualize.py",
        "mnist_easy.py",
        "mnist_mlp.py",
    ]
    assert all(ast.get_docstring(tree) for tree in trees.values())


@pytest.mark.parametrize("path", EXAMPLES, ids=lambda path: path.stem)
def test_every_name_an_example_takes_from_this_library_is_still_there(path):
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[0] == "xrd":
            module = importlib.import_module(node.module)
            missing = [alias.name for alias in node.names if not hasattr(module, alias.name)]
            assert not missing, f"{path.name} imports {missing} from {node.module}"


@pytest.mark.parametrize("path", EXAMPLES, ids=lambda path: path.stem)
def test_the_playbooks_page_shows_each_example_and_the_url_it_reads(path):
    page = (REPO / "docs" / "playbooks.md").read_text()
    assert f"examples/{path.name}" in page
    assert "root://127.0.0.1:21094" in path.read_text()
