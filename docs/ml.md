# Machine learning

You have a URL and you want to train something on it. Three lines:

```python
import xrd.ml

data = xrd.ml.load("root://eos.example.org//store/mnist.root")

for images, labels in data.train.batches(256):
    loss = criterion(model(images), labels)
```

Nothing was downloaded. Every minibatch is a read of one basket out of the
file wherever it lives, so a file larger than the machine trains the same way
a small one does, and the URL can be a storage element, an HTTPS server, an S3
bucket or a path — see [Files and paths](files.md) for what a URL may be.

`xrd.ml` is the friendly face of [`xrd.root.ml`](root.md#into-pytorch-and-tensorflow),
which is where the tensors are actually made. Everything on this page can be
done a layer down with more control and more typing; nothing here prevents
going there later.

## What it works out for itself

```pycon
>>> data = xrd.ml.load("datasets/mnist.root")
>>> print(data)
datasets/mnist.root: 70,000 rows, 10 classes
  inputs   image: 784 x uint8, scaled to 0-1
  answer   label: int32
  splits   train 60,000 rows, test 10,000 rows
```

Three things, each of which can be said outright instead:

**Which rows are which.** Trees named `train_0` … `train_9` and `test_0` …
`test_9` — which is what [`xrd.root.datasets`](root.md#the-datasets-everyone-teaches-with)
writes — make a `train` split and a `test` split of ten classes apiece. A file
of one tree has one split, called `all`. `train`, `validation`, `valid`,
`val`, `dev`, `eval` and `test` are the prefixes recognised.

**Which column is the answer.** The one called `label`, `target`, `class` or
`y`, whichever the file has. A file with none of them is a dataset with no
answer, which is what an autoencoder wants; its batches are the inputs alone
rather than a pair.

**Which columns are the question.** Every other column of numbers, less the
bookkeeping ones — `index`, `entry` — that say where a row came from. Columns
of strings or of objects are not numbers and are left out.

When a file's names are its own, say so:

```python
data = xrd.ml.load(url, inputs=["pt", "eta", "phi"], answer="is_signal")
```

## Looking before training

None of this imports a framework, so a file can be opened and understood on a
machine with no PyTorch installed at all:

```pycon
>>> data.train.counts()
{'0': 5923, '1': 6742, '2': 5958, '3': 6131, '4': 5842, '5': 5421, ...}
>>> data.train.head(2)
[{'image': [0, 0, 0, ...], 'label': 0}, {'image': [...], 'label': 0}]
>>> print(data.train.preview())
label 0
               .+%+.
              .%%%%%
             .%%%%%%:
            :#%%%#:%%=
           +%%%%%%-*%+
          .%%%*=%%.:@+
         .%%%* :=   %%.
        .+%%#:      %%+
        *%%:        %%*
       :%%:         %%*
```

`counts` is free when the file keeps a tree per class; otherwise it reads the
answer column and nothing else — the pictures stay on the server. `head` hands
back plain Python: lists, ints and floats, in the shape the file holds them.
`preview` draws any column of bytes whose length is a perfect square, which is
how a picture is recognised without being told.

`data.head()` and `data.preview()` without a split are the training rows, and
`data.classes`, `len(data)`, `data["test"]` and `"validation" in data` answer
the rest of what a new file raises.

## Batches

```python
for images, labels in data.train.batches(256, device="cuda"):
    ...
```

Each batch is a pair of tensors, or the inputs alone when the file has no
answer column. What is in them is decided the way a person would decide it:

* every input column becomes a `(rows, features)` block of `float32`, side by
  side if there are several;
* a column of bytes is a picture, so it is divided by 255 into the nought-to-one
  that networks expect — `load(url, scale=False)` to be handed the file's own
  numbers instead;
* the answer comes back as whole numbers for a classifier and as floats when
  the column is floating-point, because that is what the loss functions of each
  take.

A picture stays flat, 784 numbers wide, because that is how the file holds it;
a convolutional net wants `images.view(-1, 1, 28, 28)` and that is the one
reshape left to you.

For the rest of the PyTorch world, `loader` hands back a real `DataLoader`:

```python
loader = data.train.loader(256, workers=4)
```

`workers` reads with that many processes, each taking its own share of every
tree and opening its own connection to the server — nothing is shared between
them but the name of the file, which is what keeps four readers from reading
over each other. The batching happens on this side of the loader — one basket
read serves thousands of rows — so the loader is built with `batch_size=None`,
and `data.train.dataset(256)` is the `IterableDataset` underneath it for
anyone who wants to build the loader themselves.

`shuffle=False` is for the pass that scores a model, where the order changes
nothing and being able to line rows up against the file helps.

## Holding some back

A test split is for the end. To carve a validation set out of the training
rows:

```python
train, valid = data.train.split(0.9)
```

The cut is made in every tree, so both halves hold every class in the same
proportion, and neither reads the other's rows — `valid` is entries 54,000
onwards of each tree and nothing else is fetched for it.

```pycon
>>> print(train, valid, sep="\n")
train (first 90%): 54,000 rows in 10 trees, 10 classes
train (last 10%): 6,000 rows in 10 trees, 10 classes
```

## What a run costs

Memory, not disk. Rows are read in pools — `step` of them from each tree at
once, shuffled together and cut into minibatches — so what a training loop
holds is that pool and the baskets being read, whatever the file's size. The
default aims at sixty-four megabytes and is worked out from the width of the
row and the number of trees:

```pycon
>>> data.step
8516
```

Halve it and the memory halves and the shuffling narrows; raise it and the
reads get longer and the shuffle wider. `xrd.ml.load(url, step=2048)` says it
outright. [Training playbooks](playbooks.md) measures all of this on real
files rather than asserting it.

## A whole program

```python
import torch
from torch import nn

import xrd.ml

with xrd.ml.load("root://127.0.0.1:21094//mnist.root") as data:
    print(data)
    model = nn.Sequential(nn.Linear(784, 128), nn.ReLU(), nn.Linear(128, 10))
    optimiser = torch.optim.Adam(model.parameters(), lr=1e-3)

    for epoch in range(5):
        for images, labels in data.train.batches(256):
            optimiser.zero_grad(set_to_none=True)
            loss = nn.functional.cross_entropy(model(images), labels)
            loss.backward()
            optimiser.step()

    right = sum(
        int((model(images).argmax(1) == labels).sum())
        for images, labels in data.test.batches(1000, shuffle=False)
    )
    print(f"accuracy {right / len(data.test):.4f}")
```

That is `examples/mnist_easy.py`, which runs on a laptop in about a minute.
The dataset holds the file open, so use it in a `with` block; a script that
forgets is closed for it when the dataset is collected.

## Files to train on

`xrd.root.datasets` writes 588 published sets into this shape — MNIST,
CIFAR-10 and -100, Fashion-MNIST, and a long tail of tabular and audio sets,
each with what it is licensed under. See
[the datasets everyone teaches with](root.md#the-datasets-everyone-teaches-with),
and [Training playbooks](playbooks.md) for making one and serving it on a port
you can bind with no daemon and no login.

## Loading by name

```bash
export XRD_CATALOGUE=https://data.example.org
```

```python
data = xrd.ml.load("mnist")
```

A bare name — no scheme, no slash, no file of that name here — is looked up
in the catalogue: the `index.json` a [datasets site](datasets-site.md)
serves, which maps names to files. Anything that could be a place is treated
as one, so a real path or URL is never shadowed by a catalogue entry. The
catalogue can also be set per call with `Config(catalogue=...)`, and can be a
local directory as easily as a URL.

## Keeping a local copy

Streaming is the default because it is usually the faster answer: a minibatch
is a read of the baskets it needs, so the first batch arrives without waiting
for the last byte of the file, and a dataset bigger than the machine is not a
problem. But a copy on disk wins when the same data is read over and over — a
hyperparameter sweep, an epoch loop on a small set — or when the link is worse
than the disk.

```python
data = xrd.ml.load("mnist", cache=True)      # pull once, then read locally
```

```python
path = xrd.ml.download("mnist")              # or take the file itself
```

`download` returns the path the file now lives at, under `cache_dir`
(`$XRD_CACHE`, or `~/.cache/xrd/datasets`) unless `into=` says otherwise. A
second call for the same source transfers nothing; a local path is its own
cache and comes straight back, uncopied. Pass a whole directory to `cache=`
instead of `True` to put one dataset somewhere of its own.

The pull lands in a `.part` file and is renamed onto the target only once the
bytes are all there and the catalogue's size and `adler32` agree, so an
interrupted or corrupted transfer never leaves something a later run would
mistake for the dataset. That is also why a cache hit is cheap: the length is
checked, but nothing that reached its final name was ever unverified, so the
digest is not recomputed on every open. `refresh=True` pulls again over
whatever is there.

## When to go a layer down

[`xrd.root.ml`](root.md#into-pytorch-and-tensorflow) is the expert layer, and
what it offers that this does not:

* TensorFlow, through `tf_dataset` — this page is PyTorch only;
* batches as a `dict` of named tensors, rather than an `(inputs, answers)`
  pair, when a model takes columns in some other arrangement;
* jagged columns — a variable number of values a row — padded to a width you
  choose;
* trees mixed by hand, `step` and `batch` set apart from each other, and any
  tree in any file rather than a split worked out from names.

Nothing is lost by starting here: `data.train.dataset()` is one of its objects
already, and `xrd.root.open_root` opens the same file for both.
