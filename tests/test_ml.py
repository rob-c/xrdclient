"""A file of rows, in the shape a training loop wants it.

:mod:`xrd.ml` is the layer a physicist meets first: a URL in, minibatches
out, and nothing about baskets, offsets or tensor dtypes in between. What is
checked here is that everything it works out for itself - which trees are the
training rows, which column is the answer, how much to hold at once - it works
out the way the file says, and that it says so plainly when it cannot.

PyTorch is not installed in this suite, so the batching tests stand a small
module in for it, as :mod:`tests.test_root_ml` does; the tests above them use
no framework at all, which is the point of that half of the API.
"""

from __future__ import annotations

import gc
import json
import pathlib
import sys
import types

import pytest

import xrd
from xrd.ml import Column, Dataset, _pool, download, load
from xrd.root import Histogram, create, open_root

DATA = pathlib.Path(__file__).parent / "data"

#: A picture whose four pixels land on four different shades.
PIXELS = [0, 85, 170, 255]


def _write(path, columns, trees):
    """A ROOT file of the given trees, each a list of rows."""
    with create(str(path)) as out:
        for name, rows in trees.items():
            out.tree(name, columns).extend(rows)
    return str(path)


@pytest.fixture
def digits(tmp_path):
    """Pictures separated by class, as :mod:`xrd.root.datasets` writes them."""
    columns = {"image": ("B", 4), "label": "i", "index": "i"}
    trees = {
        f"{split}_{digit}": [{"image": PIXELS, "label": digit, "index": n} for n in range(rows)]
        for split, rows in (("train", 4), ("test", 2))
        for digit in range(3)
    }
    return _write(tmp_path / "digits.root", columns, trees)


@pytest.fixture
def tabular(tmp_path):
    """One tree, two columns of numbers and an answer: the other common shape."""
    rows = [{"first": n, "second": n * 2, "label": n % 2} for n in range(6)]
    columns = {"first": "f", "second": "f", "label": "i"}
    return _write(tmp_path / "tabular.root", columns, {"Events": rows})


@pytest.fixture
def flat():
    """A tree of physics: jagged columns, and nothing that looks like an answer."""
    return str(DATA / "small-flat-tree.root")


# ---------------------------------------------------------------------------
# What the file says it holds
# ---------------------------------------------------------------------------


def test_the_trees_say_which_rows_are_for_training(digits):
    with load(digits) as data:
        assert list(data.splits) == ["train", "test"]
        assert (len(data.train), len(data.test), len(data)) == (12, 6, 18)
        assert data.classes == ["0", "1", "2"]


def test_the_columns_say_which_is_the_question_and_which_the_answer(digits):
    with load(digits) as data:
        assert (data.inputs, data.answer) == (["image"], "label")
        assert str(data.columns["image"]) == "image: 4 x uint8"


def test_the_bookkeeping_column_is_not_something_to_learn_from(digits):
    with load(digits) as data:
        assert "index" not in data.inputs


def test_the_summary_is_the_file_in_five_lines(digits):
    with load(digits) as data:
        printed = str(data).splitlines()
    assert printed[0].endswith("digits.root: 18 rows, 3 classes")
    assert printed[1:] == [
        "  inputs   image: 4 x uint8, scaled to 0-1",
        "  answer   label: int32",
        "  splits   train 12 rows, test 6 rows",
    ]


def test_a_file_with_nothing_to_predict_is_summarised_without_an_answer(flat):
    """An autoencoder's file has no answer, so the summary does not claim one."""
    with load(flat, inputs=["Int32"]) as data:
        printed = str(data).splitlines()
    assert printed[1:] == ["  inputs   Int32: int32", "  splits   all 100 rows"]


def test_a_table_of_many_columns_is_counted_rather_than_listed(tmp_path):
    """Fifty measurements are one line of a summary, not fifty."""
    columns = {f"m{n}": "f" for n in range(6)} | {"label": "i"}
    path = _write(tmp_path / "wide.root", columns, {"rows": [dict.fromkeys(columns, 1)]})
    with load(path) as data:
        assert str(data).splitlines()[1] == (
            "  inputs   6 columns - m0: float32, m1: float32, m2: float32, and 3 more"
        )


def test_the_repr_is_the_short_version(digits):
    with load(digits) as data:
        assert repr(data).endswith("of 18 rows in 2 splits>")
        assert repr(data.train) == "<Split 'train' of 12 rows>"
        assert str(data.train) == "train: 12 rows in 3 trees, 3 classes"


def test_a_file_that_knows_nothing_of_splits_is_all_one(flat):
    with load(flat, inputs=["Int32"]) as data:
        assert list(data.splits) == ["all"]
        assert data.default is data["all"]
        assert data.classes == []
        assert str(data.default) == "all: 100 rows in 1 trees"


def test_asking_for_rows_that_are_not_there_says_which_are(digits):
    with load(digits) as data:
        assert "train" in data
        assert "validation" not in data
        with pytest.raises(KeyError, match="it has train, test"):
            data["validation"]


def test_a_file_with_no_trees_is_not_a_dataset(tmp_path):
    with create(str(tmp_path / "hist.root")) as out:
        out["counts"] = Histogram.new("counts", [0, 1, 2], [1, 2])
    with pytest.raises(ValueError, match=r"holds no trees.*it holds counts"):
        load(tmp_path / "hist.root")


def test_a_tree_with_no_numbers_in_it_is_not_a_dataset():
    with pytest.raises(ValueError, match="no columns of numbers to learn from"):
        load(DATA / "string-example.root")


def test_a_file_of_nothing_but_bookkeeping_has_nothing_to_learn_from(tmp_path):
    path = _write(
        tmp_path / "empty.root",
        {"label": "i", "index": "i"},
        {"Events": [{"label": 1, "index": 0}]},
    )
    with pytest.raises(ValueError, match="nothing to learn from"):
        load(path)


def test_the_columns_can_be_said_outright(digits):
    with load(digits, inputs=["index"], answer="label") as data:
        assert (data.inputs, data.answer) == (["index"], "label")


def test_a_column_that_is_not_there_is_a_mistake_worth_naming(digits):
    with pytest.raises(ValueError, match="no column of numbers called 'digit'"):
        load(digits, answer="digit")
    with pytest.raises(ValueError, match="no column of numbers called 'pixels'"):
        load(digits, inputs=["pixels"])


def test_a_dataset_can_be_read_out_of_a_file_already_open(digits):
    with open_root(digits) as handle:
        data = Dataset(handle)
        data.close()  # not this dataset's file to close
        assert len(data.head(1)) == 1


def test_the_pool_is_sized_from_the_row_and_bounded_both_ways():
    """Whatever the rows are, the memory a run costs stays a laptop's worth."""
    wide = {"x": Column("x", "float64", "d", 1_000_000)}
    narrow = {"x": Column("x", "uint8", "B", 1)}
    assert _pool(wide, ["x"], 1) == 256
    assert _pool(narrow, ["x"], 1) == 16_384
    assert _pool({"x": Column("x", "float64", "d", 1024)}, ["x"], 4) == 2048


def test_the_pool_can_be_said_outright(digits):
    with load(digits, step=7) as data:
        assert data.step == 7


def test_a_column_says_what_it_is(flat):
    with load(flat, inputs=["Int32", "SliceInt32"]) as data:
        assert str(data.columns["Int32"]) == "Int32: int32"
        assert str(data.columns["SliceInt32"]).endswith("a different number of values each row")
        assert data.columns["Int32"].itemsize == 4
        assert not data.columns["SliceInt32"].is_picture


# ---------------------------------------------------------------------------
# Looking at it, with no framework installed
# ---------------------------------------------------------------------------


def test_the_first_rows_come_back_as_plain_python(digits):
    with load(digits) as data:
        assert data.head(1) == [{"image": PIXELS, "label": 0}]


def test_the_first_rows_carry_on_into_the_next_class(digits):
    with load(digits) as data:
        assert [row["label"] for row in data.train.head(6)] == [0, 0, 0, 0, 1, 1]
        assert len(data.train.head(99)) == 12


def test_a_jagged_column_comes_back_as_the_list_it_is(flat):
    with load(flat, inputs=["SliceInt32"]) as data:
        assert data.head(3) == [{"SliceInt32": []}, {"SliceInt32": [1]}, {"SliceInt32": [2, 2]}]


def test_a_picture_is_drawn_in_characters(digits):
    with load(digits) as data:
        assert data.preview() == "label 0\n -\n*@"


def test_rows_that_are_not_pictures_are_listed_as_numbers(tabular):
    with load(tabular) as data:
        assert data.preview(2) == "label 0: first 0.0, second 0.0\n\nlabel 1: first 1.0, second 2.0"


def test_rows_with_no_answer_are_listed_without_one(flat):
    with load(flat, inputs=["Int32"]) as data:
        assert data.preview() == "Int32 0"


def test_the_classes_are_counted_from_the_tree_names(digits):
    with load(digits) as data:
        assert data.train.counts() == {"0": 4, "1": 4, "2": 4}


def test_the_classes_are_counted_by_reading_when_the_names_do_not_say(tabular):
    with load(tabular) as data:
        assert data.default.counts() == {0: 3, 1: 3}


def test_counting_classes_needs_something_that_says_what_they_are(flat):
    with load(flat, inputs=["Int32"]) as data:
        with pytest.raises(ValueError, match="says nothing about classes"):
            data.default.counts()


def test_classes_come_out_in_the_order_a_person_reads_them(tmp_path):
    columns = {"first": "f", "label": "i"}
    path = _write(
        tmp_path / "many.root",
        columns,
        {f"train_{n}": [{"first": 1.0, "label": n}] for n in (10, 2, 1)},
    )
    with load(path) as data:
        assert data.classes == ["1", "2", "10"]


def test_classes_with_names_come_out_in_their_own_order(tmp_path):
    columns = {"first": "f", "label": "i"}
    path = _write(
        tmp_path / "pets.root",
        columns,
        {"train_dog": [{"first": 1.0, "label": 1}], "train_cat": [{"first": 2.0, "label": 0}]},
    )
    with load(path) as data:
        assert data.classes == ["cat", "dog"]
        assert data.train.counts() == {"cat": 1, "dog": 1}


# ---------------------------------------------------------------------------
# Cutting it up
# ---------------------------------------------------------------------------


def test_a_split_cuts_every_tree_in_the_same_proportion(digits):
    with load(digits) as data:
        learn, held = data.train.split(0.75)
        assert (len(learn), len(held)) == (9, 3)
        assert learn.classes == held.classes == ["0", "1", "2"]
        assert str(learn).startswith("train (first 75%): 9 rows")
        assert str(held).startswith("train (last 25%): 3 rows")


def test_a_fraction_outside_the_ends_would_leave_one_side_empty(digits):
    with load(digits) as data, pytest.raises(ValueError, match="between 0 and 1"):
        data.train.split(1.0)


# ---------------------------------------------------------------------------
# Training on it
# ---------------------------------------------------------------------------


class Tensor:
    """Enough of a tensor to see what was asked for."""

    def __init__(self, values, dtype, shape=None, device=None):
        self.values = list(values)
        self.dtype = dtype
        self.shape = shape
        self.device = device

    def __len__(self):
        return self.shape[0] if self.shape else len(self.values)

    @property
    def width(self):
        return self.shape[1] if self.shape else 1

    def rows(self):
        return [self.values[at : at + self.width] for at in range(0, len(self.values), self.width)]

    def __getitem__(self, key):
        rows = self.rows()
        taken = rows[key] if isinstance(key, slice) else [rows[at] for at in key.values]
        shape = (len(taken), self.width) if self.shape else None
        return Tensor([value for row in taken for value in row], self.dtype, shape, self.device)

    def __truediv__(self, divisor):
        return Tensor(
            [value / divisor for value in self.values], self.dtype, self.shape, self.device
        )

    def reshape(self, rows, width):
        return Tensor(self.values, self.dtype, (rows, width), self.device)

    def to(self, device):
        return Tensor(self.values, self.dtype, self.shape, device)

    def float(self):
        return Tensor([float(value) for value in self.values], "float32", self.shape, self.device)

    def long(self):
        return Tensor([int(value) for value in self.values], "int64", self.shape, self.device)


def _cat(tensors, dim=0):
    """The tensors one after another, down the rows or across them."""
    if dim == 0:
        return _cat_rows(tensors)
    return _cat_columns(tensors)


def _cat_rows(tensors):
    first = tensors[0]
    shape = (sum(len(tensor) for tensor in tensors), first.width) if first.shape else None
    return Tensor(
        [value for tensor in tensors for value in tensor.values],
        first.dtype,
        shape,
        first.device,
    )


def _cat_columns(tensors):
    first = tensors[0]
    rows = [
        [value for tensor in tensors for value in tensor.rows()[at]] for at in range(len(first))
    ]
    width = sum(tensor.width for tensor in tensors)
    return Tensor(
        [value for row in rows for value in row],
        first.dtype,
        (len(rows), width),
        first.device,
    )


@pytest.fixture
def torch(monkeypatch):
    """A stand-in for PyTorch, in the place ``import torch`` looks."""
    module = types.ModuleType("torch")
    for name in ("int8", "uint8", "int16", "int32", "int64", "float32", "float64"):
        setattr(module, name, name)
    module.frombuffer = lambda values, dtype: Tensor(values, dtype)
    module.cat = _cat
    module.randperm = lambda rows, device=None: Tensor(
        reversed(range(rows)), "int64", device=device
    )

    data = types.ModuleType("torch.utils.data")

    class IterableDataset:
        pass

    class DataLoader:
        """Enough of a loader to see what it was handed."""

        def __init__(self, dataset, batch_size=None, num_workers=0):
            self.dataset, self.batch_size, self.num_workers = dataset, batch_size, num_workers

        def __iter__(self):
            return iter(self.dataset)

        def __len__(self):
            return len(self.dataset)

    data.IterableDataset = IterableDataset
    data.DataLoader = DataLoader
    data.get_worker_info = lambda: None
    utils = types.ModuleType("torch.utils")
    utils.data = data
    module.utils = utils

    monkeypatch.setitem(sys.modules, "torch", module)
    monkeypatch.setitem(sys.modules, "torch.utils", utils)
    monkeypatch.setitem(sys.modules, "torch.utils.data", data)
    return module


def test_a_batch_is_the_pair_a_training_loop_unpacks(torch, digits):
    with load(digits) as data:
        images, labels = next(iter(data.train.batches(6, shuffle=False)))
    assert (images.shape, images.dtype) == ((6, 4), "float32")
    assert images.rows()[0] == [0.0, 85 / 255, 170 / 255, 1.0]
    # A pool holds every class at once, and unshuffled it hands them over in
    # the order the trees are in - which is exactly why shuffling is the default.
    assert (labels.dtype, labels.values) == ("int64", [0, 0, 0, 0, 1, 1])


def test_the_numbers_are_the_files_own_when_nothing_is_scaled(torch, digits):
    with load(digits, scale=False) as data:
        images, _ = next(iter(data.train.batches(2, shuffle=False)))
    assert images.rows()[0] == [0.0, 85.0, 170.0, 255.0]


def test_several_input_columns_arrive_side_by_side(torch, tabular):
    with load(tabular) as data:
        features, labels = next(iter(data.default.batches(3, shuffle=False)))
    assert (features.shape, features.rows()[1]) == ((3, 2), [1.0, 2.0])
    assert labels.values == [0, 1, 0]


def test_an_answer_that_is_measured_rather_than_named_stays_a_float(torch, tmp_path):
    path = _write(
        tmp_path / "energy.root",
        {"first": "f", "target": "d"},
        {"Events": [{"first": 1.0, "target": 2.5}]},
    )
    with load(path) as data:
        _, answers = next(iter(data.default.batches(1, shuffle=False)))
    assert (answers.dtype, answers.values) == ("float32", [2.5])


def test_a_file_with_no_answer_hands_over_the_inputs_alone(torch, flat):
    with load(flat, inputs=["Int32"]) as data:
        batch = next(iter(data.default.batches(4, shuffle=False)))
    assert batch.values == [0, 1, 2, 3]


def test_the_shortcuts_train_on_the_training_rows(torch, digits):
    with load(digits) as data:
        assert len(list(data.batches(4))) == len(list(data.train.batches(4)))
        assert len(data.loader(4)) == len(data.train.dataset(4))


def test_a_loader_is_what_the_rest_of_pytorch_expects(torch, digits):
    with load(digits) as data:
        loader = data.train.loader(4, workers=2, device="cuda")
        assert (loader.batch_size, loader.num_workers) == (None, 2)
        assert next(iter(loader))[0].device == "cuda"


def test_the_batches_are_counted_before_any_of_them_are_read(torch, digits):
    with load(digits) as data:
        batches = data.train.dataset(5)
        assert len(batches) == len(list(batches))


def test_only_the_rows_of_a_split_are_read(torch, digits):
    with load(digits) as data:
        learn, held = data.train.split(0.5)
        rows = sum(len(labels) for _, labels in held.batches(2, shuffle=False))
    assert (rows, len(held), len(learn)) == (6, 6, 6)


def test_the_pictures_are_shuffled_by_pytorchs_own_shuffling(torch, digits):
    with load(digits) as data:
        _, labels = next(iter(data.train.batches(6)))
    assert labels.values == [2, 2, 2, 2, 1, 1]  # the stand-in reverses the pool


# ---------------------------------------------------------------------------
# Putting it down
# ---------------------------------------------------------------------------


def test_the_file_closes_with_the_dataset(digits):
    data = load(digits)
    data.close()
    with pytest.raises(ValueError, match="closed file"):
        data.head(1)


def test_a_dataset_nobody_closed_closes_itself(digits):
    data = load(digits)
    handle = data._file
    del data
    gc.collect()
    with pytest.raises(ValueError, match="closed file"):
        handle["train_0"].arrays(["label"])


def test_the_module_is_one_attribute_away(monkeypatch):
    """``xrd.ml`` resolves lazily, so nobody pays for the ROOT reader."""
    monkeypatch.delattr(xrd, "ml", raising=False)
    assert xrd.ml is sys.modules["xrd.ml"]


# ---------------------------------------------------------------------------
# Loading by name
# ---------------------------------------------------------------------------


@pytest.fixture
def catalogue(digits, tmp_path):
    """A directory the shape ``xrd-datasets build`` leaves behind."""
    index = {"format": 1, "datasets": [{"name": "digits", "file": "digits.root"}]}
    (tmp_path / "index.json").write_text(json.dumps(index))
    return str(tmp_path)


def test_a_bare_name_is_found_in_the_catalogue(catalogue):
    with load("digits", config=xrd.Config(catalogue=catalogue)) as data:
        assert len(data) == 18


def test_a_catalogue_file_shard_must_be_selected_by_publisher_split(digits, tmp_path):
    index = {
        "format": 2,
        "datasets": [
            {
                "name": "digits_sharded",
                "files": [
                    {"split": "train", "file": pathlib.Path(digits).name},
                    {"split": "test", "file": pathlib.Path(digits).name},
                ],
            }
        ],
    }
    (tmp_path / "index.json").write_text(json.dumps(index))
    config = xrd.Config(catalogue=str(tmp_path))
    with pytest.raises(ValueError, match="pass split="):
        load("digits_sharded", config=config)
    with load("digits_sharded", split="train", config=config) as data:
        assert len(data) == 18


def test_the_catalogue_can_come_from_the_environment(catalogue, monkeypatch):
    monkeypatch.setenv("XRD_CATALOGUE", catalogue)
    with load("digits") as data:
        assert len(data) == 18


def test_a_catalogue_is_read_over_http(digits):
    from xrd.testing import FakeDAVServer

    index = json.dumps({"format": 1, "datasets": [{"name": "digits", "file": "digits.root"}]})
    files = {"/d/index.json": index.encode(), "/d/digits.root": pathlib.Path(digits).read_bytes()}
    with FakeDAVServer(files=files) as server:
        with load("digits", config=xrd.Config(catalogue=f"{server.url}d")) as data:
            assert len(data) == 18


def test_a_name_with_no_catalogue_set_says_how_to_set_one(monkeypatch):
    monkeypatch.delenv("XRD_CATALOGUE", raising=False)
    with pytest.raises(ValueError, match="XRD_CATALOGUE"):
        load("digits")


def test_a_name_the_catalogue_has_not_got_names_what_it_has(catalogue):
    with pytest.raises(ValueError, match=r"has digits, and no 'mnist'"):
        load("mnist", config=xrd.Config(catalogue=catalogue))


def test_a_file_that_exists_is_never_shadowed_by_the_catalogue(digits, monkeypatch):
    """A bare name that names a real file here is that file, catalogue or not."""
    monkeypatch.delenv("XRD_CATALOGUE", raising=False)
    here = pathlib.Path(digits)
    plain = here.rename(here.with_name("digits"))
    monkeypatch.chdir(plain.parent)
    with load("digits") as data:  # no catalogue set, and none needed
        assert len(data) == 18


# ---------------------------------------------------------------------------
# Pulling a dataset once, and reading it from disk after
# ---------------------------------------------------------------------------


@pytest.fixture
def served(digits):
    """A one-dataset catalogue over HTTP, with the index a build would write."""
    from xrd.crypto import checksum_file
    from xrd.testing import FakeDAVServer

    raw = pathlib.Path(digits).read_bytes()
    entry = {
        "name": "digits",
        "file": "digits.root",
        "bytes": len(raw),
        "adler32": checksum_file("adler32", [raw]),
    }
    index = json.dumps({"format": 1, "datasets": [entry]})
    files = {"/d/index.json": index.encode(), "/d/digits.root": raw}
    with FakeDAVServer(files=files) as server:
        yield types.SimpleNamespace(url=f"{server.url}d", raw=raw, entry=entry)


def test_a_dataset_is_pulled_to_the_cache_and_named_after_itself(served, tmp_path):
    where = download("digits", into=tmp_path, config=xrd.Config(catalogue=served.url))
    assert where.parent == tmp_path
    assert where.name.endswith("-digits.root")
    assert where.read_bytes() == served.raw


def test_a_dataset_pulled_once_is_read_without_the_server(served, tmp_path):
    """The point of the cache: the second run does not need the network at all."""
    config = xrd.Config(catalogue=served.url)
    first = download("digits", into=tmp_path, config=config)
    with load(first) as data:
        assert len(data) == 18
    # The catalogue is gone; only the copy on disk can answer now.
    again = download(str(served.url) + "/digits.root", into=tmp_path)
    assert again.read_bytes() == served.raw


def test_the_cache_directory_is_where_the_config_says(served, tmp_path):
    config = xrd.Config(catalogue=served.url, cache_dir=str(tmp_path / "c"))
    where = download("digits", config=config)
    assert where.parent == tmp_path / "c" / "datasets"


def test_a_file_already_here_is_its_own_cache(digits, tmp_path):
    """Nothing is copied for a path that is local already."""
    cache = tmp_path / "cache"
    assert download(digits, into=cache) == pathlib.Path(digits)
    assert not cache.exists()


def test_two_catalogues_offering_one_name_do_not_land_on_each_other():
    from xrd.ml import _cache_name

    a = _cache_name("https://one.example.org/mnist.root")
    b = _cache_name("https://two.example.org/mnist.root")
    assert (a.endswith("-mnist.root"), b.endswith("-mnist.root"), a == b) == (True, True, False)


def test_a_source_with_no_file_name_left_in_it_still_names_a_file():
    from xrd.ml import _cache_name

    assert _cache_name("/").endswith("-dataset.root")


def test_a_second_pull_of_what_is_there_transfers_nothing(served, tmp_path, monkeypatch):
    config = xrd.Config(catalogue=served.url)
    download("digits", into=tmp_path, config=config)

    def refuse(*args, **options):
        raise AssertionError("the cached copy should have been enough")

    # ``xrd.copy`` the name is the function; the module it shadows is the one
    # ``download`` imports from, so that is the one to take the copy out of.
    monkeypatch.setattr(sys.modules["xrd.copy"], "copy", refuse)
    assert download("digits", into=tmp_path, config=config).read_bytes() == served.raw


def test_refresh_pulls_over_what_is_already_there(served, tmp_path):
    config = xrd.Config(catalogue=served.url)
    where = download("digits", into=tmp_path, config=config)
    where.write_bytes(b"not the dataset any more")
    assert download("digits", into=tmp_path, config=config, refresh=True).read_bytes() == served.raw


def test_a_copy_of_the_wrong_length_is_refused_and_not_kept(served, tmp_path, monkeypatch):
    """The catalogue said how big it is, so a short file is a failed pull."""
    monkeypatch.setattr(
        "xrd.ml._agrees",
        lambda part, entry, source: (_ for _ in ()).throw(ValueError("truncated")),
    )
    with pytest.raises(ValueError, match="truncated"):
        download("digits", into=tmp_path, config=xrd.Config(catalogue=served.url))
    assert list(tmp_path.glob("*.part")) == []


def test_a_length_the_catalogue_disagrees_with_is_named(served, tmp_path):
    from xrd.ml import _agrees

    part = tmp_path / "short.root"
    part.write_bytes(b"nowhere near")
    with pytest.raises(ValueError, match="bytes long, and its catalogue says"):
        _agrees(part, served.entry, "digits")


def test_a_checksum_the_catalogue_disagrees_with_is_named(served, tmp_path):
    from xrd.ml import _agrees

    part = tmp_path / "wrong.root"
    part.write_bytes(b"x" * served.entry["bytes"])
    with pytest.raises(ValueError, match="adler32"):
        _agrees(part, served.entry, "digits")


def test_a_url_nobody_catalogued_is_taken_as_it_arrives(served, tmp_path):
    """With no entry there is nothing to hold the bytes to but the copy itself."""
    from xrd.ml import _agrees

    part = tmp_path / "loose.root"
    part.write_bytes(b"whatever this is")
    _agrees(part, {}, "https://host/loose.root")


def test_load_can_do_the_pull_itself(served, tmp_path):
    config = xrd.Config(catalogue=served.url, cache_dir=str(tmp_path))
    with load("digits", cache=True, config=config) as data:
        assert len(data) == 18
    assert (tmp_path / "datasets").is_dir()


def test_load_takes_a_directory_to_cache_into(served, tmp_path):
    config = xrd.Config(catalogue=served.url)
    with load("digits", cache=tmp_path, config=config) as data:
        assert len(data) == 18
    assert list(tmp_path.glob("*-digits.root"))


def test_caching_a_file_that_is_already_open_is_a_mistake_worth_naming(digits):
    with open(digits, "rb") as handle:
        with pytest.raises(ValueError, match="not an already-open file"):
            load(handle, cache=True)
