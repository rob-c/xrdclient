# ROOT files

`xrd.root` opens a ROOT file and reads its trees in pure Python — over
`root://`, `https://`, WebDAV, `s3://` or a local path, with no ROOT, no
`uproot`, no `numpy` and no compiled extension anywhere in the way. It
[writes new files](#writing) too, and histograms and graphs
[draw themselves](#drawing), onto matplotlib axes or into plain characters.

```python
import xrd.root

with xrd.root.open_root("root://eos.example.org//store/events.root") as f:
    tree = f["Events"]
    print(len(tree), tree.keys())
    pt = tree["Muon_pt"].array(0, 10_000)
```

Nothing is downloaded. A tree is a set of *baskets* — compressed blocks of
consecutive entries, one branch at a time — and asking for entries 0 to 10 000
of one branch reads the baskets that hold them and nothing else. That is what
makes it reasonable to walk a hundred-gigabyte file from a laptop: the bytes
that cross the wire are the ones asked for.

## Opening

```python
xrd.root.open_root("/local/path/f.root")  # a path
xrd.root.open_root("davs://dav.example.org/store/f.root")  # any scheme this library speaks
xrd.root.open_root(open("f.root", "rb"))  # an open file, left open
```

A file is a mapping from name to object:

```python
f.keys()  # ['Events', 'metadata']
f.classnames()  # {'Events': 'TTree', 'metadata': 'TH1F'}
f.trees()  # ['Events']
f.tree()  # the only tree, or a KeyError naming the ones there are
f["dir/sub/Events"]  # directories nest, with a path
f["Events;1"]  # an older cycle, when a file kept several
```

`classnames()` is the first thing to try when something will not open: it says
what a name is without reading any of it.

A name that is not a tree is read too, when the file describes the class it
holds — which is the usual case for a C++ class somebody wrote, and for most of
ROOT's own kit as well:

```python
f["tlv"]  # {'TObject': {...}, 'fP': {'fX': 10.0, ...}, 'fE': 40.0}
f["FileSummaryRecord"]  # a std::string key is a str
f["written"]  # a TDatime is a datetime.datetime
f["h1d"]  # a histogram is a Histogram, and a graph a Graph
```

Objects held by pointer are followed, whether the class promises they are there
or writes the name of the class in front of them; a `TList`, `TObjArray`,
`TClonesArray` or `TArray` member is read the way it streams itself rather than
by the members it declares — a `TClonesArray` names the one class it holds at
the front, and a slot never filled comes back as `None` rather than shifting
the rest along; and a container of objects is read one object after another, or
a field at a time when it was written that way — every object's first member,
then every object's second — which is how ROOT writes a container of plain C++
objects and a `TClonesArray` asked to bypass its streamer. A `vector` of pairs
comes back as the list of tuples it is. A class
inside an entry that this reader cannot walk — one the file does not describe,
or one that streams itself some way of its own — comes back as the name of its
class, because the length written in front of it says how to step over it
without touching anything else.

The bytes have to account for themselves: a class that streams itself in some
way of its own leaves the object a different length than the layout says, and
that is refused by name rather than read into a plausible wrong answer.

## Histograms

A `TH1`, `TH2` or `TH3` of any bin type comes back as a `Histogram`: bins,
edges and what is in them, counted the way Python counts.

```python
h = f["h1d"]
h.name, h.title, h.shape  # ('h1d', 'h1d', (10,))
h.values()  # array('d', [6.6, 72.6, 543.4, ...])
h.errors()  # the same shape, from the sums of squared weights
h.edges()  # 11 edges for 10 bins
h.axes[0].centers()  # where a point would be drawn
h.sum(), h.entries  # 11000.0, 10004.0
len(h), len(h.axes[0])  # 10, 10
```

`values()[0]` is the first bin of the axis, not the underflow. ROOT keeps two
extra bins per axis for what fell off each end, and `values(flow=True)` gives
them back at the ends where ROOT keeps them — so `sum(flow=True)` is everything
that was ever filled and `sum()` is what landed on the axis.

A two-dimensional histogram gives a row per bin along x, so `values()[ix][iy]`
is the bin ROOT calls `GetBinContent(ix+1, iy+1)`, and a three-dimensional one
nests once more. Everything is an `array.array`, which `numpy.asarray` takes
without copying and which needs nothing installed to use as it is.

A histogram filled with weights keeps the sum of their squares, and that is
where `errors()` comes from; one filled without them keeps no such sum, and
then the error on a count of *n* is its square root, which is what ROOT itself
would give. An unevenly binned axis keeps every edge and those are given back
exactly; an evenly binned one keeps none, and they are worked out from the ends.

Every member the histogram was written with is still there under `h.members`,
under the name of the class that declared it, so nothing is hidden by being
tidied away.

## Graphs

A `TGraph`, `TGraphErrors`, `TGraphAsymmErrors` or `TGraphMultiErrors` comes
back as a `Graph`, which is a sequence of points:

```python
g = f["tge"]
len(g), g[0]  # 4, (1.0, 2.0)
for x, y in g:
    ...
g.x, g.y  # array('d', [...]) each, one value per point
g.points()  # [(1.0, 2.0), (2.0, 4.0), ...]
below, above = g.yerr  # the bars either side, or None if none were kept
```

`xerr` and `yerr` are always a pair, low side first, so the same code reads a
graph whose bars are symmetric and one whose are not — a graph that kept one
array for both sides gives that array twice. A `TGraph` proper kept none, and
both are `None`.

A `TGraphMultiErrors` keeps its y errors in layers — statistical and
systematic, say — and `g.layers` is those pairs of bars in the order they were
added. Asking such a graph for `yerr` raises rather than picking a layer or
summing them for you, because how to combine them is physics, not format. For
every other graph `layers` is simply `(yerr,)`, or `()` when none were kept.

A graph or histogram met *inside* another object — in the list a `TMultiGraph`
keeps, behind a pointer in a `TEfficiency` — comes back as a `Graph` or
`Histogram` too, the same as it would standing in a key of its own.

## Drawing

ROOT draws through a `TCanvas`, which this library does not carry. What it
has instead is the two ways Python usually looks at data. `plot()` draws onto
matplotlib axes — made on demand, or brought along — and returns them, so
styling and saving carry on where it left off:

```python
ax = f["h1d"].plot()  # steps for 1D, a shaded mesh for 2D
f["tge"].plot(ax=ax, color="crimson")  # points with their error bars
ax.figure.savefig("both.png")
```

matplotlib is not a dependency; `pip install pyxrootdclient[plot]` brings it,
and without it `plot()` refuses with both ways out by name. The other way is
`text()`, which needs nothing installed at all and goes anywhere a string
goes — a terminal, a log file, a CI transcript:

```python
print(f["h1d"].text())  # one line per bin: its edges, a bar and the value
print(f["h2d"].text())  # a shaded grid, y upward
print(f["tge"].text())  # a grid of stars with the axis ends labelled
```

A graph of layered error bars draws every layer over the same points; a
three-dimensional histogram has no honest flat picture and refuses both ways,
saying to slice `values()` down to the two dimensions you want to see.

## Columns

```python
tree.show()  # one line per column: name, type, variable or not
tree.typenames()  # {'Muon_pt': 'float32', 'nMuon': 'int32', ...}
tree.keys()  # every column
tree.readable()  # the ones this reader decodes
tree.unreadable  # {name: why not}, rather than quietly missing
```

A column comes back as one of three things, and which one is knowable in
advance from `tree[name].is_jagged` and `typename`:

| The column | What `array()` gives |
| --- | --- |
| a plain number (`x/F`) | an `array.array` of one value per entry |
| a fixed array (`x[10]/F`) | an `array.array`, `branch.length` values per entry |
| a variable one (`x[n]/F`) | a `Jagged` — rows of different lengths |
| a character leaf (`x/C`) | a `list[str]` |
| a string, `std::string` or `TString` | a `list[str]` |
| an STL container | a `list`, one Python object per entry |

```python
tree["nMuon"].array()  # array('i', [2, 0, 3, ...])
tree["Muon_pt"].array(0, 1000)  # <Jagged 1000 rows of 2431 f values>
jets = tree["Muon_pt"].array()
jets[7]  # array('f', [22.5, 19.0])
jets.lengths()  # [2, 0, 3, ...]
jets.tolist()
values, width = jets.padded()  # a flat rectangle and its width
```

Entry numbers behave like a Python slice, negatives included:
`branch.array(-1000)` is the last thousand entries.

## Reading a file that does not fit

`iterate` walks the tree in batches, holding one batch rather than one file:

```python
for batch in tree.iterate(["Muon_pt", "Muon_eta"], step=50_000):
    analyse(batch["Muon_pt"], batch["Muon_eta"])
```

`tree.arrays()` is the same for one range, and takes every readable column
when it is not told which.

## C++ classes and containers

A file written by a physics framework is usually a C++ class per entry, split
by ROOT into one branch per member. Those branches are columns here like any
other, under the names ROOT gave them:

```python
tree.keys()  # ['Muon.pt', 'Muon.eta', 'evt.N', 'evt.StlVecF64', ...]
tree["evt.StlVecF64"].array(0, 100)  # <Jagged 100 rows of ...>
```

The object itself is a branch too, holding nothing at all — every byte of it
is in the members — so asking for it gives back one dictionary per entry, and
an object nested inside it is a dictionary inside that:

```python
tree.groups()  # ['evt', 'P3'] - the objects that were split
tree["evt"].array(1, 2)
# [{'I32': 1, 'Str': 'evt-001', 'P3': {'Px': 0, 'Py': 1.0, 'Pz': 0}, ...}]
```

This reads exactly the same baskets as asking for the members, and costs the
same; it is a shape, not a shortcut. `tree.arrays()` and `tree.iterate()`
leave the split objects out, because their members are already there under
their own names and taking both would read every basket twice. A member this
reader will not decode is absent from the dictionary and named in
`tree["evt"].unreadable`, with the same sentence `tree.unreadable` gives it.

A file written with splitting turned off puts the whole object in one branch,
with nothing under it. That reads as the same dictionary per entry, walked
member by member in the order the class declares them, using the layout the
file's own streamer information gives:

```python
tree.keys()  # ['evt'] - the object, and nothing under it
tree["evt"].array(1, 2)
# [{'Beg': 'beg-001', 'I32': 1, 'P3': {'Px': 0, 'Py': 1.0, 'Pz': 0}, ...}]
```

A class that inherits keeps each base under the base's own name, because a
derived class is allowed to declare a member its base already declared and
flattening the two together would quietly drop one of them. `TObject`, which
nearly every ROOT class inherits, comes back as the identifier and bits it
really is:

```python
tree["p4"].array(0, 1)
# [{'TObject': {'fUniqueID': 0, 'fBits': 50331648},
#   'fP': {'TObject': {...}, 'fX': 0.0, 'fY': 1.0, 'fZ': 2.0}, 'fE': 3.0}]
```

Some classes stream themselves rather than being written out by the file's
streamer information - `TLorentzVector` is one - and then the entry is the
class's own record with a version in front of the members. Both are read, and
so is the older `TBranchObject`, which writes the name of the class in front
of every entry; a branch of that kind holding more than one class stops with a
message naming both rather than reading the one as the other.

The same events written the two ways read back the same values, member for
member. Splitting is still the cheaper way to have written them - a split file
lets you read one member without touching the rest, and an unsplit one cannot -
but neither is a file this reader has to refuse. A class the file's streamer
information does not describe is refused, because its layout is then not
knowable, and so is a member of a kind this reader has no reader for: an
unsplit object is read from first byte to last, so one member it cannot walk
past is the whole entry.

A member that is an STL container comes back as the Python thing it most
nearly is, one per entry:

| In the file | In Python |
| --- | --- |
| `std::vector<double>`, `list`, `deque`, `set` of numbers | a `Jagged` — rows of `array.array` |
| a container of strings | a `list[list[str]]` |
| `vector<vector<T>>` | a `list` of `list`s of `array.array` |
| `std::map<K, V>`, `unordered_map` | a `list[dict]`, one dict per entry |
| `std::string`, `TString` | a `list[str]` |
| `std::vector<bool>` | rows of 0 and 1, a byte an element, which is how ROOT wrote it |
| `ROOT::VecOps::RVec<T>` | whatever the same `std::vector<T>` gives, which is what it is written as |
| `std::bitset<N>` | rows of 0 and 1, `bs[0]` first, a byte a bit as ROOT wrote it |
| `TDatime` | a `datetime.datetime`, out of the one word it packs itself into |

`Double32_t` and `Float16_t` are floats squeezed into three or four bytes by a
recipe written as `[xmin,xmax,nbits]`, where the ends may be given in units of
`pi`. A branch of its own keeps the recipe in the leaf title; a member of a
split class keeps it in the trailing comment on the declaration, which is in
the file's streamer information — either way it is unpacked back to `float64`,
so `typenames()` says `float64` and nothing about the packing reaches you. A
packed member of a class the file does not describe is refused rather than
read at the default, because the wrong recipe gives plausible wrong numbers.

## Into PyTorch and TensorFlow

`xrd.root.ml` turns those batches into tensors. Neither framework is imported
until it is called, so nothing here costs anything on a machine with neither:

```python
import torch, xrd.root, xrd.root.ml

tree = xrd.root.open_root("root://eos.example.org//store/events.root").tree()
loader = torch.utils.data.DataLoader(
    xrd.root.ml.dataset(tree, ["Muon_pt", "Muon_eta"], step=8192),
    batch_size=None,  # each item is already a batch
    num_workers=4,  # each worker reads its own share of the entries
)

for batch in loader:
    model(batch["Muon_pt"])
```

`batch_size=None` is the point: one basket read serves thousands of rows, so
the batching belongs here and not in the loader. Several workers split the
entry range between them, so each reads a different part of the file rather
than all of them reading all of it.

A worker is a process of its own, and it opens the file again for itself
rather than reading down the handle it inherited — two processes seeking one
descriptor read over each other's shoulders, and two sharing one XRootD
session read each other's replies. Nothing has to be arranged for that: the
first read in a child notices whose process it is in, dials its own
connection and carries on. A tree opened from a file object cannot do this and
says so, because a handle somebody else opened cannot be reopened by name.

The sets in `xrd.root.datasets` keep one tree per class, which is the wrong
order to learn in: a loop over those trees in turn shows a model five thousand
cats and then five thousand dogs. `mixed` reads `step` entries from each tree,
shuffles that pool together and cuts `batch` rows off it at a time, so every
minibatch holds every class:

```python
with xrd.root.open_root("root://127.0.0.1:21094//cifar10.root") as handle:
    trees = [handle[name] for name in handle.trees() if name.startswith("train_")]
    loader = torch.utils.data.DataLoader(
        xrd.root.ml.mixed(trees, ["image", "label"], step=1024, batch=256, device="cuda"),
        batch_size=None,
    )
```

`step` is what the memory costs — a pool of that many rows from each tree, and
nothing else of the file. The shuffling is PyTorch's own, so `torch.manual_seed`
settles it; `shuffle=False` is for the pass that scores a model, where the
order makes no difference and the rows can be watched going by. Three worked
examples are in [Training playbooks](playbooks.md).

Fixed-size array columns arrive shaped `(entries, width)`. Variable ones are
padded to the widest row in the batch, or to a width you give:

```python
xrd.root.ml.to_tensor(jets, width=4, fill=0.0)  # (entries, 4)
xrd.root.ml.iter_tensors(tree, step=8192, device="cuda")
```

Unsigned columns wider than a byte are widened into the signed type that holds
them, because neither framework's unsigned types beyond `uint8` are supported
well enough to hand anybody; a value too large for `int64` is refused by name
rather than wrapped around.

TensorFlow takes the same tree through `tf_dataset`, which declares its shapes
and types up front — from what the file says the columns are, without a first
pass over the data — so the rest of `tf.data` works on it:

```python
data = xrd.root.ml.tf_dataset(tree, ["Muon_pt"], step=8192).prefetch(2)
for batch in data:
    model(batch["Muon_pt"])
```

With no column names, either dataset takes every numeric column and leaves the
strings and objects out, rather than leaving them in to fail later.

## Writing

`create` makes a new ROOT file anywhere this library can put bytes — a local
path, any URL scheme it writes, or an already-open binary file, used as it is
and left open:

```python
import xrd.root

with xrd.root.create("root://eos.example.org//store/user/me/out.root") as f:
    f["counts"] = xrd.root.Histogram.new("counts", edges, values)
    f["scan"] = xrd.root.Graph.new("scan", xs, ys, yerr=bars)
    f["note"] = "made from run 4711"
    f["weights"] = array.array("d", weights)
```

The file is a mapping from name to object, closed with `with` or `close()`. It
is the real thing — keys, directory, free list, and the streamer information
describing its classes exactly as the ROOT the layouts were harvested from
would — so ROOT, uproot and this library's own reader all read it back by its
own self-description. A name written twice becomes a second cycle of itself,
exactly as in ROOT, and reading back gives the newest. Everything is built in
memory and written out in one piece at a clean close; a `with` block that
raises writes nothing at all, on the principle that no file is better than
half a file.

What can be written is what can be written *correctly*: trees of numbers,
histograms and graphs — read from another file, or built from plain numbers —
plus strings and `array.array`s of signed integers or floats, which become the
matching `TArray`. `Histogram.new` takes every bin edge, one value per bin (two more
fills the flow bins), and optionally per-bin errors and an entry count;
evenly spaced edges are stored the compact way ROOT stores an even axis.
`Graph.new` picks its own class: plain points make a `TGraph`, one bar per
point a `TGraphErrors`, and any `(low, high)` pair of runs a
`TGraphAsymmErrors`.

Anything else is refused by name rather than guessed at — a
`TGraphMultiErrors`, an unsigned array ROOT has no class for, a histogram
with fits attached or an axis with labels (empty them first, rather than
have them silently dropped), a name a reader could never ask back for —
because a plausible-looking file that ROOT misreads is worse than an error
message. Files past 2 GB, which need ROOT's wide layout, are refused too:
split the output across files.

`create(..., compression="zstd")` chooses the algorithm — zlib unless said
otherwise, or `lzma`, `lz4`, `zstd`, `None` to store raw — and `level` the
effort; an object that did not get smaller is stored raw anyway, exactly as
ROOT does.

### Trees

`f.tree(name, columns)` declares a tree, and `fill` gives it entries:

```python
with xrd.root.create("out.root") as f:
    tree = f.tree("events", {"energy": float, "hits": ("i", 4), "ok": bool})
    for event in run:
        tree.fill(energy=event.energy, hits=event.hits, ok=event.passed)
```

A column is a Python `bool`, `int` or `float`, an [`array`][array] type code
such as `'f'` for a narrower number, or a pair of either and how many values
every entry holds — `("i", 4)` is four `int32`s per entry. A Python `int` gets
the widest ROOT has, because a Python int has no width of its own and a column
that quietly stopped fitting halfway down the file would be worse. `extend`
takes an iterable of mappings for the same thing in bulk.

[array]: https://docs.python.org/3/library/array.html

Entries go out a basket at a time as they gather — `basket_size` bytes of a
column at a time, 32 kB unless said otherwise — so a tree can be far larger
than memory, and reading a range back later costs one read per basket it
touches rather than one read of everything. Each column fills its own baskets
at its own rate, so a wide image column and a one-byte label column do not
force each other's geometry. The tree's own record says where every basket
landed, so it is written when the file closes.

An entry that does not fit its columns is refused whole — nothing is kept for
any column, so the tree is exactly as it was — and it is refused by name:
which column, what it holds, and what arrived instead. Columns are fixed-size
by design; rows of varying length, strings and split C++ objects are refused
rather than approximated, on the same principle as the rest of the writer.

The result is a tree laid out the way ROOT lays one out, down to the record
versions and the `fLeaves` references pointing at the very leaves the branches
hold, so ROOT, uproot and this library's own reader all walk it the same way.

### MNIST, end to end

`xrd.root.mnist` is the worked example: it fetches the handwritten digits —
through this library, so the source can be a URL, a path or bytes already in
hand — and writes them as one tree per class. It is one of the eight in
[`xrd.root.datasets`](#the-datasets-everyone-teaches-with) under its own name,
because it is the one everybody starts with.

```python
from xrd.root import create, mnist

with create("mnist.root") as f:
    for split in ("train", "test"):
        print(mnist.convert(f, split=split))
# {'train_0': 5923, 'train_1': 6742, ... 'train_9': 5949}
# {'test_0': 980, 'test_1': 1135, ... 'test_9': 1009}
```

That is all 70,000 images in one 11.6 MB file — as small as the gzipped
originals, and unlike them addressable a range at a time. Each tree holds
`image` (784 `uint8`, the 28×28 picture flat), `label`, and `index`, the
entry's place in the original file, so any row can be traced back. One tree
per digit is the shape a training loop wants: sampling a class reads one part
of the file rather than seeking all over it, and every tree carries its label
anyway, so concatenating all ten and shuffling works exactly as well.

Training on it is the loader from [above](#into-pytorch-and-tensorflow), with
nothing in between:

```python
import torch, xrd.root, xrd.root.ml

with xrd.root.open_root("mnist.root") as f:
    loaders = [
        torch.utils.data.DataLoader(
            xrd.root.ml.dataset(f[f"train_{digit}"], ["image", "label"], step=64),
            batch_size=None,
        )
        for digit in range(10)
    ]
    for parts in zip(*loaders):  # 64 of each digit: a balanced batch
        x = torch.cat([part["image"] for part in parts]).float().div_(255)
        y = torch.cat([part["label"] for part in parts]).long()
        loss = torch.nn.functional.cross_entropy(model(x.view(-1, 1, 28, 28)), y)
```

The `image` column arrives shaped `(entries, 784)` because the file says it is
784 wide, so that `view` is the only shaping anybody has to write. Taking one
batch from each loader is what the per-class layout buys: every batch is class
balanced without a sampler, and the epoch ends with the smallest class.
Training on the ten trees as one stream instead is `itertools.chain`, and
shuffling across them is whatever your training would do anyway.

Point `open_root` at a `root://` or `https://` URL and the same loop trains
straight off a storage element, reading the baskets it needs and nothing else.

### The datasets everyone teaches with

MNIST is one of 1,422 datasets admitted to a public mirror. Two more registered
sets below the source ceiling are available for private builds but have no
formal redistribution licence. `xrd.root.datasets` converts
the sets machine learning is actually taught and benchmarked with, all of them
the same way — one tree per class, the label beside the data, and the row's
place in the original file so any number can be traced back to where it came
from. Of the original 627, three hundred and fifty have a number to predict rather
than a class to sort into get one tree of every row instead, and the number to
predict is a column like any other. What ships here is the converter, not the
data: no dataset is redistributed in this repository, and `datasets/` is where
the files it writes are meant to go.

```python
from xrd.root import datasets

datasets.convert("cifar10", "cifar10.root", split="train")
# {'train_airplane': 5000, 'train_automobile': 5000, ... 'train_truck': 5000}

datasets.convert("iris", "iris.root")
# {'setosa': 50, 'versicolor': 50, 'virginica': 50}
```

`describe()` prints the lot, with the licence and the source of each:

| name | what it is | licence | converted |
| --- | --- | --- | --- |
| `mnist` | 70,000 handwritten digits, 28×28 greyscale, 10 classes | CC BY-SA 3.0 | 11.6 MB |
| `fashion_mnist` | 70,000 clothing photographs, 28×28 greyscale, 10 classes | MIT | 30.7 MB |
| `kmnist` | 70,000 classical Japanese characters, 28×28 greyscale, 10 classes | CC BY-SA 4.0 | 21.6 MB |
| `cifar10` | 60,000 photographs, 32×32 colour, 10 classes | see below | 169.1 MB |
| `cifar100` | 60,000 photographs, 32×32 colour, 100 classes in 20 superclasses | see below | 167.3 MB |
| `iris` | 150 iris flowers measured four ways, 3 species | CC BY 4.0 | 9 kB |
| `penguins` | 344 penguins measured at Palmer Station, 3 species | CC0 | 13 kB |
| `covertype` | 581,012 patches of Colorado forest, 54 features, 7 cover types | CC BY 4.0 | 8.3 MB |
| `emnist` | 131,600 handwritten characters, 28×28 greyscale, 47 balanced classes | US federal government work | 33.2 MB |
| `fsdd` | 3,000 recordings of spoken digits, 8 kHz mono, 6 speakers, 10 classes | CC BY-SA 4.0 | build-dependent |
| `adult` | 48,842 census records, 14 features, 2 income classes | CC BY 4.0 | 570 kB |
| `mushroom` | 8,124 mushrooms described 22 ways, edible or poisonous | CC BY 4.0 | 66 kB |
| `letter` | 20,000 printed capitals measured 16 ways, 26 classes | CC BY 4.0 | 320 kB |
| `digits` | 5,620 handwritten digits as 8×8 counts of ink, 10 classes | CC BY 4.0 | 380 kB |
| `wine` | 178 wines analysed 13 ways, 3 cultivars | CC BY 4.0 | 20 kB |
| `breast_cancer` | 569 cell-nucleus images measured 30 ways, benign or malignant | CC BY 4.0 | 111 kB |
| `dry_bean` | 13,611 beans measured 16 ways from photographs, 7 varieties | CC BY 4.0 | 1.4 MB |
| `seeds` | 210 wheat kernels measured 7 ways, 3 varieties | CC BY 4.0 | 18 kB |
| `miniboone` | 130,064 particle-identification events measured 50 ways, signal or background | CC BY 4.0 | 39.8 MB |
| `har` | 10,299 windows of phone accelerometer and gyroscope, 561 features, 6 activities | CC BY 4.0 | 40.2 MB |
| `semeion` | 1,593 handwritten digits as 16×16 black and white, 10 classes | CC BY 4.0 | 54 kB |
| `sms_spam` | 5,574 text messages, ham or spam | CC BY 4.0 | 337 kB |
| `wine_quality` | 6,497 Portuguese wines analysed 11 ways and scored out of ten | CC BY 4.0 | 175 kB |
| `spambase` | 4,601 e-mails counted 57 ways, spam or not | CC BY 4.0 | 216 kB |
| `ionosphere` | 351 radar returns measured 34 ways, good or bad | CC BY 4.0 | 80 kB |
| `glass` | 214 fragments of glass measured 9 ways, 6 kinds | CC BY 4.0 | 26 kB |
| `abalone` | 4,177 abalone measured 8 ways and counted for rings, 3 sexes | CC BY 4.0 | 83 kB |
| `banknote` | 1,372 photographed banknotes measured 4 ways, 2 classes | CC BY 4.0 | 42 kB |
| `magic` | 19,020 air showers seen by a Cherenkov telescope, gamma or hadron | CC BY 4.0 | 775 kB |
| `htru2` | 17,898 pulsar candidates from a radio survey, 2 classes | CC BY 4.0 | 977 kB |
| `heart_disease` | 920 patients from four hospitals, 5 degrees of narrowed arteries | CC BY 4.0 | 72 kB |
| `car_evaluation` | 1,728 cars described six ways and judged acceptable or not | CC BY 4.0 | 15 kB |
| `yeast` | 1,484 yeast proteins measured 8 ways, 10 places in the cell | CC BY 4.0 | 50 kB |
| `auto_mpg` | 398 cars of the 1970s and how far they went on a gallon | CC BY 4.0 | 14 kB |
| `bike_sharing` | 17,379 hours of a bicycle hire scheme, and how many were taken out | CC BY 4.0 | 222 kB |
| `energy_efficiency` | 768 simulated buildings and the heating and cooling they need | CC BY 4.0 | 12 kB |
| `real_estate` | 414 flats sold in Taipei and what a unit of floor cost | CC BY 4.0 | 16 kB |
| `student` | 1,044 pupils, 32 answers each, and the mark they finished on | CC BY 4.0 | 32 kB |
| `airfoil` | 1,503 wind tunnel runs and how loud the aerofoil was | CC BY 4.0 | 15 kB |
| `automobile` | 205 cars imported into America in 1985 and what they cost | CC BY 4.0 | 14 kB |
| `balance_scale` | 625 balances and which way each one tips, 3 classes | CC BY 4.0 | 10 kB |
| `bank_marketing` | 45,211 sales calls and whether the customer took the deposit | CC BY 4.0 | 476 kB |
| `blood_transfusion` | 748 blood donors and whether each gave again, 2 classes | CC BY 4.0 | 11 kB |
| `climate_crashes` | 540 runs of an ocean model and whether each one finished | CC BY 4.0 | 83 kB |
| `computer_hardware` | 209 mainframes of the 1980s and how fast each one was | CC BY 4.0 | 10 kB |
| `concrete_slump` | 103 concrete mixes and how each one flowed and held | CC BY 4.0 | 9 kB |
| `contraceptive` | 1,473 Indonesian couples and what they used, 3 classes | CC BY 4.0 | 20 kB |
| `dermatology` | 366 patients with one of 6 red scaly skin diseases | CC BY 4.0 | 49 kB |
| `diabetes_risk` | 520 patients asked about 14 symptoms, 2 classes | CC BY 4.0 | 14 kB |
| `ecoli` | 336 E. coli proteins measured 7 ways, 8 places in the cell | CC BY 4.0 | 28 kB |
| `fertility` | 100 men, 9 questions each, and their semen analysis | CC BY 4.0 | 9 kB |
| `forest_fires` | 517 fires in a Portuguese park and how far each one spread | CC BY 4.0 | 16 kB |
| `garment_productivity` | 1,197 team-days in a clothing factory and what each got done | CC BY 4.0 | 26 kB |
| `german_credit` | 1,000 loan applications judged good or bad, 2 classes | CC BY 4.0 | 25 kB |
| `haberman` | 306 breast cancer operations and who was alive 5 years on | CC BY 4.0 | 8 kB |
| `heart_failure` | 299 heart failure patients and who survived the follow-up | CC BY 4.0 | 14 kB |
| `hepatitis` | 155 hepatitis patients, 19 findings each, 2 classes | CC BY 4.0 | 14 kB |
| `image_segmentation` | 2,310 patches of outdoor photographs, 7 things they are of | CC BY 4.0 | 211 kB |
| `indian_liver` | 583 patients from Andhra Pradesh, 2 classes | CC BY 4.0 | 19 kB |
| `liver_disorders` | 345 blood tests and the drinking to predict from them | CC BY 4.0 | 9 kB |
| `lymphography` | 148 lymph node X-rays read 18 ways, 4 classes | CC BY 4.0 | 19 kB |
| `mammographic_mass` | 961 lumps seen on a mammogram, benign or malignant | CC BY 4.0 | 11 kB |
| `maternal_health` | 1,014 pregnancies seen in rural clinics, 3 degrees of risk | CC BY 4.0 | 14 kB |
| `nursery` | 12,960 nursery applications ranked 5 ways | CC BY 4.0 | 45 kB |
| `occupancy` | 20,560 minutes in an office and whether anyone was in it | CC BY 4.0 | 327 kB |
| `online_shoppers` | 12,330 shopping sessions and which of them ended in a sale | CC BY 4.0 | 295 kB |
| `parkinsons` | 195 voice recordings, 22 measurements each, 2 classes | CC BY 4.0 | 42 kB |
| `parkinsons_telemonitoring` | 5,875 recordings made at home and the two scores to predict | CC BY 4.0 | 395 kB |
| `pendigits` | 10,992 digits written on a tablet, 8 points each, 10 classes | CC BY 4.0 | 277 kB |
| `phishing` | 11,055 websites scored 30 ways, phishing or legitimate | CC BY 4.0 | 101 kB |
| `power_plant` | 9,568 hours of a gas turbine and the power it put out | CC BY 4.0 | 139 kB |
| `qsar_aquatic` | 546 chemicals and the dose that kills half the water fleas | CC BY 4.0 | 18 kB |
| `qsar_fish` | 908 chemicals and the dose that kills half the minnows | CC BY 4.0 | 21 kB |
| `raisin` | 900 raisins measured off a photograph, 2 varieties | CC BY 4.0 | 46 kB |
| `rice` | 3,810 grains of rice measured off a photograph, 2 varieties | CC BY 4.0 | 103 kB |
| `satellite` | 6,435 Landsat neighbourhoods of 9 pixels, 6 kinds of ground | CC BY 4.0 | 290 kB |
| `seismic` | 2,584 shifts in a Polish coal mine, 2 classes | CC BY 4.0 | 48 kB |
| `servo` | 167 servomechanisms and how long each took to settle | CC BY 4.0 | 7 kB |
| `solar_flare` | 1,066 active regions on the sun, 7 modified Zurich classes | CC BY 4.0 | 25 kB |
| `sonar` | 208 sonar returns off a rock or a mine, 60 bands each | CC BY 4.0 | 105 kB |
| `soybean` | 683 diseased soybean plants, 19 diseases | CC BY 4.0 | 254 kB |
| `statlog_heart` | 270 patients from the Cleveland heart study, 2 classes | CC BY 4.0 | 14 kB |
| `steel_industry` | 35,040 quarter hours in a steel plant, 3 kinds of load | CC BY 4.0 | 479 kB |
| `tic_tac_toe` | 958 finished games and whether x had won, 2 classes | CC BY 4.0 | 12 kB |
| `vertebral_column` | 310 lower spines measured 6 ways, 3 classes | CC BY 4.0 | 18 kB |
| `wifi_localisation` | 2,000 readings of 7 wifi points, taken in 4 rooms | CC BY 4.0 | 26 kB |
| `yacht` | 308 towing tank runs and the resistance each hull made | CC BY 4.0 | 8 kB |
| `zoo` | 101 animals described 16 ways, 7 kinds of animal | CC BY 4.0 | 26 kB |
| `absenteeism_at_work` | 740 absences at a Brazilian courier firm and the hours each one lost | CC BY 4.0 | 18 kB |
| `acute_inflammations` | 120 patients with urinary symptoms, judged for inflammation of the bladder | CC BY 4.0 | 8 kB |
| `aids_clinical_trials` | 2,139 people on HIV treatment and whose illness went on progressing | CC BY 4.0 | 61 kB |
| `android_permissions` | 29,332 Android apps described by the permissions they ask for, benign or malware | CC BY 4.0 | 265 kB |
| `annealing` | 898 steel coils, their chemistry, and the annealing class each took | CC BY 4.0 | 43 kB |
| `appliances_energy` | 19,735 ten-minute readings of a low-energy house and the watt-hours it drew | CC BY 4.0 | 1.1 MB |
| `auction_verification` | 2,043 runs of a model checker over simulated spectrum auctions, verified or not | CC BY 4.0 | 21 kB |
| `audiology` | 200 hearing tests and the diagnosis each led to, 24 classes | CC BY 4.0 | 262 kB |
| `autism_screening_adult` | 704 adults put through the autism screening questionnaire, 2 classes | CC BY 4.0 | 19 kB |
| `autism_screening_child` | 292 children put through the autism screening questionnaire, 2 classes | CC BY 4.0 | 15 kB |
| `beijing_pm25` | 43,824 hours of Beijing weather and the fine particles in the air | CC BY 4.0 | 412 kB |
| `bone_marrow_transplant` | 187 children given a bone marrow transplant, and who survived it | CC BY 4.0 | 25 kB |
| `breast_cancer_coimbra` | 116 blood samples from Coimbra, healthy women and cancer patients | CC BY 4.0 | 15 kB |
| `breast_cancer_original` | 699 breast tissue samples scored nine ways, benign or malignant | CC BY 4.0 | 16 kB |
| `breast_cancer_prognostic` | 198 breast cancer operations and whether the disease came back | CC BY 4.0 | 54 kB |
| `breast_cancer_recurrence` | 286 breast cancer cases from Ljubljana and which of them came back | CC BY 4.0 | 10 kB |
| `cardiotocography` | 2,126 foetal heart traces read by three obstetricians, 3 classes | CC BY 4.0 | 56 kB |
| `cdc_diabetes` | 253,680 answers to a CDC health survey, and who among them had diabetes | CC BY 4.0 | 2.7 MB |
| `census_income_kdd` | 199,523 census records from the 1990s and who earned over $50,000 | CC BY 4.0 | 6.2 MB |
| `cervical_cancer_behaviour` | 72 women answering a behaviour questionnaire, with and without cervical cancer | CC BY 4.0 | 13 kB |
| `cervical_cancer_risk` | 858 cervical cancer screenings and what the biopsy found | CC BY 4.0 | 30 kB |
| `challenger_o_rings` | 23 shuttle launches, the temperature at each, and the O-rings that failed | CC BY 4.0 | 6 kB |
| `chess_endgame` | 28,056 king-and-rook against king-and-king positions, by the moves white needs | CC BY 4.0 | 110 kB |
| `chronic_kidney_disease` | 400 patients tested for chronic kidney disease, 2 classes | CC BY 4.0 | 20 kB |
| `communities_crime` | 1,994 American communities described by the census, and the violent crime in each | CC BY 4.0 | 363 kB |
| `concrete_strength` | 1,030 concrete mixes, how long each cured, and the strength it reached | CC BY 4.0 | 19 kB |
| `congressional_voting` | 435 congressmen and the sixteen votes that give away the party | CC BY 4.0 | 15 kB |
| `connect_four` | 67,557 Connect Four positions eight moves in, won, lost or drawn | CC BY 4.0 | 494 kB |
| `credit_card_default` | 30,000 Taiwanese credit cards and which of them defaulted the next month | CC BY 4.0 | 1.1 MB |
| `credit_screening` | 690 credit card applications with every field anonymised, approved or not | CC BY 4.0 | 20 kB |
| `daily_demand` | 60 days at a Brazilian logistics firm and the orders each brought in | CC BY 4.0 | 10 kB |
| `darwin` | 174 people writing on a graphics tablet, with and without Alzheimer's | CC BY 4.0 | 588 kB |
| `diabetes_hospitals` | 101,766 hospital stays for diabetes, and who came back | CC BY 4.0 | 2.9 MB |
| `diabetic_retinopathy` | 1,151 eye images scored by a lesion detector, with and without retinopathy | CC BY 4.0 | 92 kB |
| `dota2_games` | 102,944 games of Dota 2, the heroes each side picked, and who won | CC BY 4.0 | 2.1 MB |
| `drug_consumption` | 1,885 personality profiles, and how recently each person last used cannabis | CC BY 4.0 | 95 kB |
| `eeg_eye_state` | 14,980 moments of EEG from fourteen electrodes, eyes open or shut | CC BY 4.0 | 370 kB |
| `el_nino` | 178,080 readings from the Pacific buoy array and the air temperature at each | CC BY 4.0 | 2.3 MB |
| `entrance_exam` | 666 students sitting an engineering entrance exam, and how well each did | CC BY 4.0 | 18 kB |
| `facebook_live_sellers` | 7,050 posts by Thai fashion sellers, by what kind of post each one was | CC BY 4.0 | 117 kB |
| `facebook_metrics` | 500 posts by a cosmetics brand and the reaction each of them drew | CC BY 4.0 | 23 kB |
| `flags` | 194 national flags, their colours and shapes, and the country's religion | CC BY 4.0 | 46 kB |
| `gas_turbine_emissions` | 36,733 hours of a gas turbine and the carbon monoxide and nitrogen oxides it made | CC BY 4.0 | 1.2 MB |
| `gender_by_name` | 147,269 first names and the sex of the babies given them | CC BY 4.0 | 1.2 MB |
| `glioma_grading` | 839 glioma patients, the genes mutated in each, and the grade of the tumour | CC BY 4.0 | 23 kB |
| `grid_stability` | 10,000 simulated four-node power grids, stable or not | CC BY 4.0 | 1.0 MB |
| `hcv_blood_donors` | 615 blood samples from donors and hepatitis C patients, 5 classes | CC BY 4.0 | 33 kB |
| `healthy_aging_poll` | 714 older Americans polled on their health, by how many doctors each sees | CC BY 4.0 | 19 kB |
| `hepatitis_c_egypt` | 1,385 Egyptian hepatitis C patients and how far the fibrosis had gone | CC BY 4.0 | 85 kB |
| `higher_education_students` | 145 students answering how they live and study, and the grade each got | CC BY 4.0 | 62 kB |
| `horse_colic` | 368 horses with colic and whether the lesion turned out to need surgery | CC BY 4.0 | 24 kB |
| `in_vehicle_coupon` | 12,684 drivers offered a coupon at the wheel, and who took one | CC BY 4.0 | 112 kB |
| `infrared_thermography` | 1,020 thermal images of a face and the oral temperature measured after each | CC BY 4.0 | 97 kB |
| `iot_intrusion` | 123,117 network flows from an IoT testbed, ordinary traffic and twelve attacks | CC BY 4.0 | 5.0 MB |
| `iranian_churn` | 3,150 customers of an Iranian telecom and which of them left | CC BY 4.0 | 45 kB |
| `isolet` | 7,797 spoken letters described 617 ways, 26 classes | CC BY 4.0 | 25.7 MB |
| `istanbul_exchange` | 536 trading days of the Istanbul exchange beside eight other indices | CC BY 4.0 | 45 kB |
| `kidney_risk_factors` | 200 villagers in India screened for chronic kidney disease | CC BY 4.0 | 15 kB |
| `land_mines` | 338 passes of a metal detector and the kind of mine underneath | CC BY 4.0 | 13 kB |
| `metro_traffic` | 48,204 hours on an interstate near Minneapolis and the cars that went by | CC BY 4.0 | 557 kB |
| `mice_protein` | 1,080 measurements of 77 proteins in the cortex of mice, 8 classes | CC BY 4.0 | 738 kB |
| `monks_problems` | 432 toy robots described six ways, and whether the rule holds of each | CC BY 4.0 | 9 kB |
| `multivariate_gait` | 181,800 joint angles measured as ten people walked at three speeds | CC BY 4.0 | 1.8 MB |
| `musk_version1` | 476 molecular conformations described 166 ways, musk or not | CC BY 4.0 | 174 kB |
| `musk_version2` | 6,598 conformations of 102 molecules, musk or not | CC BY 4.0 | 1.4 MB |
| `news_popularity` | 39,644 Mashable articles described 58 ways, and the times each was shared | CC BY 4.0 | 6.7 MB |
| `nhanes_age` | 2,278 people in an American health survey, adult or senior | CC BY 4.0 | 40 kB |
| `obesity_levels` | 2,111 people's eating and moving habits, and the weight class each fell in | CC BY 4.0 | 114 kB |
| `ozone_level` | 5,070 days of Houston weather, and which of them broke the ozone limit | CC BY 4.0 | 400 kB |
| `page_blocks` | 5,473 blocks of a scanned document page, 5 classes | CC BY 4.0 | 125 kB |
| `pittsburgh_bridges` | 108 bridges over the three rivers of Pittsburgh, by the era each was built in | CC BY 4.0 | 16 kB |
| `poker_hand` | 1,025,010 poker hands of five cards, by what each is worth | CC BY 4.0 | 8.3 MB |
| `polish_bankruptcy` | 43,405 years of Polish company accounts and which firms went bankrupt | CC BY 4.0 | 13.5 MB |
| `post_operative_patient` | 90 patients leaving the recovery room, and where each was sent next | CC BY 4.0 | 10 kB |
| `predictive_maintenance` | 10,000 simulated hours of a milling machine, and the tools that broke | CC BY 4.0 | 134 kB |
| `room_occupancy_count` | 10,129 minutes of light, sound, temperature and CO2, and the people in the room | CC BY 4.0 | 118 kB |
| `secondary_mushroom` | 61,069 mushrooms grown from a field guide's own descriptions, edible or poisonous | CC BY 4.0 | 591 kB |
| `seoul_bike_sharing` | 8,760 hours in Seoul, the weather at each, and the bikes rented | CC BY 4.0 | 118 kB |
| `sepsis_survival` | 110,341 hospital admissions for sepsis in Norway, and who lived | CC BY 4.0 | 411 kB |
| `skin_segmentation` | 245,057 pixels of face and background, as three colour channels | CC BY 4.0 | 1.1 MB |
| `soybean_cultivars` | 320 soybean plants of forty cultivars, and the grain each yielded | CC BY 4.0 | 14 kB |
| `soybean_small` | 47 soybean plants with one of four diseases, described 35 ways | CC BY 4.0 | 29 kB |
| `spect_heart` | 267 cardiac SPECT images reduced to 22 binary patterns, normal or not | CC BY 4.0 | 13 kB |
| `spectf_heart` | 267 cardiac SPECT images described by 44 counts, normal or not | CC BY 4.0 | 31 kB |
| `splice_junctions` | 3,190 stretches of DNA and the splice junction, if any, in the middle | CC BY 4.0 | 140 kB |
| `steel_plates` | 1,941 steel plates and the seven kinds of surface fault found on them | CC BY 4.0 | 123 kB |
| `student_academics` | 131 students in Kalyani and the band each ended the semester in | CC BY 4.0 | 19 kB |
| `student_dropout` | 4,424 university students, and whether each dropped out, stayed on or graduated | CC BY 4.0 | 133 kB |
| `superconductivity` | 21,263 superconductors described by their chemistry, and the critical temperature | CC BY 4.0 | 5.8 MB |
| `support2` | 9,105 seriously ill hospital patients, and who died before going home | CC BY 4.0 | 562 kB |
| `taiwanese_bankruptcy` | 6,819 Taiwanese companies described by 95 financial ratios, solvent or bankrupt | CC BY 4.0 | 3.7 MB |
| `tennis_majors` | 943 matches at the 2013 grand slams, point by point, and who won | CC BY 4.0 | 65 kB |
| `tetouan_power` | 52,416 ten-minute readings of Tetouan's weather and the power its three zones drew | CC BY 4.0 | 1.3 MB |
| `thoracic_surgery` | 470 lung cancer operations and who was still alive a year later | CC BY 4.0 | 15 kB |
| `thyroid_recurrence` | 383 thyroid cancer patients followed fifteen years, and whose cancer came back | CC BY 4.0 | 13 kB |
| `user_knowledge` | 403 students studying DC machines, and how well each knew the subject | CC BY 4.0 | 16 kB |
| `waveform` | 5,000 generated waveforms of three kinds, described by 21 noisy attributes | CC BY 4.0 | 255 kB |
| `website_phishing` | 1,353 web pages judged phishing, suspicious or legitimate on nine signs | CC BY 4.0 | 17 kB |
| `wholesale_customers` | 440 wholesale customers and what each spent on six kinds of goods | CC BY 4.0 | 15 kB |
| `youtube_spam` | 1,956 comments under five pop videos, spam or not | CC BY 4.0 | 242 kB |
| `acute_myeloid_leukaemia` | 646 patients in a trial of two treatments for acute myeloid leukaemia | LGPL-2 or later | 16 kB |
| `alcohol_by_country` | 193 countries and how much pure alcohol each drank a head | MIT | 9 kB |
| `animal_scat` | 110 droppings found on a trail and which animal left each | MIT | 18 kB |
| `anorexia_treatment` | 72 young women treated for anorexia and what each weighed after | GPL-2 or GPL-3 | 9 kB |
| `bad_drivers` | 51 American states and what car insurance cost a driver in each | MIT | 8 kB |
| `baseball_batting` | 128,598 player-seasons of major league batting and the home runs each brought | GPL | 2.5 MB |
| `baseball_fielding` | 174,332 player-seasons of major league fielding and the errors each brought | GPL | 2.7 MB |
| `baseball_hall_of_fame` | 6,426 Hall of Fame ballots and how many votes each player drew | GPL | 88 kB |
| `baseball_hitters` | 322 baseball players in the 1986 season and what each was paid | GPL-2 | 21 kB |
| `baseball_managers` | 4,410 manager-seasons and how many games each won | GPL | 59 kB |
| `baseball_pitching` | 57,630 pitcher-seasons and the earned run average each finished with | GPL | 1.8 MB |
| `baseball_players` | 24,270 major league players and how tall each stood | GPL | 1.5 MB |
| `baseball_salaries` | 26,428 player-seasons and what the player was paid | GPL | 236 kB |
| `bechdel_test` | 1,794 films and whether each passes the Bechdel test | MIT | 92 kB |
| `biliary_cholangitis` | 418 patients with primary biliary cholangitis and how long each lived | LGPL-2 or later | 21 kB |
| `black_cherry_trees` | 31 felled black cherry trees and how much timber each held | GPL-2 or GPL-3 | 6 kB |
| `bladder_tumours` | 340 follow-up records from a trial of thiotepa against bladder tumours | LGPL-2 or later | 8 kB |
| `boating_trips` | 659 households and how many boating trips each took in a season | GPL-2 or GPL-3 | 16 kB |
| `boston_housing` | 506 census tracts around Boston and what a home in each was worth | GPL-2 or GPL-3 | 23 kB |
| `breast_cancer_gbsg` | 686 women with node-positive breast cancer and how long each went clear | LGPL-2 or later | 17 kB |
| `brushtail_possums` | 104 brushtail possums trapped and measured, and where each was caught | GPL-3 | 9 kB |
| `california_schools` | 420 Californian school districts and how their fifth-graders scored | GPL-2 or GPL-3 | 29 kB |
| `canadian_interlocks` | 248 Canadian firms and how many boards each was tied to | GPL-2 or later | 7 kB |
| `canadian_womens_work` | 263 Canadian women and whether each worked full time, part time or not at all | GPL-2 or later | 10 kB |
| `candy_rankings` | 85 sweets and how often each won a head-to-head vote | MIT | 10 kB |
| `car_seat_sales` | 400 stores and how many child car seats each sold | GPL-2 | 13 kB |
| `card_default` | 10,000 cardholders and whether each defaulted | GPL-2 | 197 kB |
| `cat_hearts` | 144 adult cats weighed body and heart, and the sex of each | GPL-2 or GPL-3 | 7 kB |
| `chicago_taxi` | 10,000 Chicago taxi rides and whether the driver was tipped | MIT | 97 kB |
| `chick_weights` | 578 weighings of chicks fed four different diets | GPL-2 or GPL-3 | 9 kB |
| `chile_plebiscite` | 2,700 Chilean voters and how each said they would vote | GPL-2 or later | 49 kB |
| `chocolate_cakes` | 270 chocolate cakes baked to three recipes and the angle each broke at | GPL-2 or later | 7 kB |
| `college_distance` | 4,739 American schoolchildren and how far each lived from a college | GPL-2 or GPL-3 | 53 kB |
| `college_majors` | 173 college majors and what their graduates earned | MIT | 21 kB |
| `colon_cancer_trial` | 1,858 records from a trial of levamisole and fluorouracil after surgery | LGPL-2 or later | 31 kB |
| `commercial_oils` | 96 samples of commercial oil and which plant each was pressed from | MIT | 19 kB |
| `congress_age` | 18,635 terms served in the US Congress and how old the member was | MIT | 305 kB |
| `cow_milk_protein` | 1,337 weekly milk samples from cows fed three diets | GPL-2 or later | 13 kB |
| `cps_wages` | 534 American workers surveyed in 1985 and what each earned an hour | GPL-2 or GPL-3 | 12 kB |
| `credit_card_applications` | 1,319 credit card applications and whether each was accepted | GPL-2 or GPL-3 | 40 kB |
| `credit_card_balance` | 400 cardholders and the balance each carried | GPL-2 | 15 kB |
| `developer_survey` | 5,594 software developers and what each was paid | MIT | 71 kB |
| `diamonds` | 53,940 round-cut diamonds and what each sold for | MIT | 744 kB |
| `dnase_assay` | 176 wells of a DNase assay and the optical density each read | GPL-2 or GPL-3 | 7 kB |
| `doctor_visits` | 5,190 Australians in the 1977 health survey and how often each saw a doctor | GPL-2 or GPL-3 | 43 kB |
| `doctoral_publications` | 915 biochemistry doctoral students and how many papers each published | GPL-2 or GPL-3 | 12 kB |
| `earthquake_intensity` | 182 seismometer readings and the ground acceleration each recorded | GPL-2 or later | 7 kB |
| `economic_growth` | 121 countries in the Mankiw, Romer and Weil growth study and how fast each grew | GPL-2 or GPL-3 | 9 kB |
| `economics_journals` | 180 economics journals and how many libraries subscribed to each | GPL-2 or GPL-3 | 13 kB |
| `email_spam` | 3,921 emails to one account and whether each was spam | GPL-3 | 73 kB |
| `epilepsy_seizures` | 236 clinic visits by epileptic patients and the seizures each brought | GPL-2 or GPL-3 | 8 kB |
| `exercise_histories` | 945 exercise reports from girls in treatment and their controls | GPL-2 or later | 14 kB |
| `extramarital_affairs` | 601 people answering a 1969 magazine survey and how often each strayed | GPL-2 or GPL-3 | 11 kB |
| `fandango_ratings` | 146 films and how four websites rated each | MIT | 16 kB |
| `fast_food_nutrition` | 515 fast food items and how many calories each holds | GPL-3 | 22 kB |
| `fatty_liver_disease` | 17,549 residents of Olmsted County and how heavy each was | LGPL-2 or later | 376 kB |
| `fertility_labour` | 254,654 American mothers in the 1980 census and whether each had a third child | GPL-2 or GPL-3 | 1.8 MB |
| `fiji_earthquakes` | 1,000 earthquakes near Fiji and how strong each was | GPL-2 or GPL-3 | 18 kB |
| `florida_2000_vote` | 67 Florida counties and how each voted in the 2000 presidential election | GPL-2 or later | 9 kB |
| `flying_etiquette` | 1,040 air travellers and what each thought of reclining a seat | MIT | 47 kB |
| `free_light_chain` | 7,874 residents of Olmsted County assayed for serum free light chains | LGPL-2 or later | 116 kB |
| `fuel_economy` | 234 car models and what each did to the gallon on the highway | MIT | 9 kB |
| `galton_heights` | 898 adult children measured with their parents | GPL-2 or later | 12 kB |
| `gapminder` | 1,704 country-years of life expectancy, population and income | CC0 | 38 kB |
| `gestation_births` | 1,236 births in the Child Health and Development Studies | GPL-2 or later | 34 kB |
| `granulomatous_disease` | 203 infection records from a trial of gamma interferon | LGPL-2 or later | 14 kB |
| `greenhouse_gases` | 300 ice-core readings of three greenhouse gases over two thousand years | Artistic-2.0 | 10 kB |
| `grouse_ticks` | 403 grouse chicks and how many ticks each carried | GPL-2 or later | 9 kB |
| `guns_and_crime` | 1,173 American state-years of crime rates and whether a carry law was in force | GPL-2 or GPL-3 | 74 kB |
| `hate_crimes` | 51 American states and how many hate crimes each reported | MIT | 11 kB |
| `help_study` | 453 adults leaving detoxification and which substance each used | GPL-2 or later | 36 kB |
| `high_school_and_beyond` | 200 American schoolchildren and which programme each was in | GPL-3 | 14 kB |
| `historic_co2` | 694 readings of atmospheric carbon dioxide over eight hundred thousand years | Artistic-2.0 | 13 kB |
| `hpc_jobs` | 4,331 jobs run on a compute cluster and how long each took | MIT | 62 kB |
| `infant_mortality` | 105 nations around 1970 and how many infants each lost per thousand born | GPL-2 or later | 7 kB |
| `infertility` | 248 women in a matched study of infertility, cases and their controls | GPL-2 or GPL-3 | 10 kB |
| `insect_sprays` | 72 plots treated with six insecticides and how many insects survived | GPL-2 or GPL-3 | 10 kB |
| `italian_olive_oils` | 572 Italian olive oils and which part of the country each came from | Artistic-2.0 | 23 kB |
| `lecture_ratings` | 73,421 ratings students at ETH Zurich gave their lecturers | GPL-2 or later | 532 kB |
| `lending_club` | 9,857 personal loans and whether each went bad | MIT | 259 kB |
| `leptograpsus_crabs` | 200 rock crabs measured five ways, in two colour forms | GPL-2 or GPL-3 | 11 kB |
| `life_cycle_savings` | 50 countries in the 1960s and how much of their income each saved | GPL-2 or GPL-3 | 7 kB |
| `liver_transplant_list` | 815 people put on a liver transplant list and what became of each | LGPL-2 or later | 17 kB |
| `loblolly_pines` | 84 measurements of loblolly pine seedlings and how tall each stood | GPL-2 or GPL-3 | 6 kB |
| `low_birth_weight` | 189 births at a Massachusetts hospital and how much each baby weighed | GPL-2 or GPL-3 | 8 kB |
| `lung_cancer_survival` | 228 patients with advanced lung cancer and how long each lived | LGPL-2 or later | 9 kB |
| `mammal_sleep` | 83 mammals and how long each sleeps in a day | MIT | 10 kB |
| `marijuana_arrests` | 5,226 people arrested in Toronto and whether each was released with a summons | GPL-2 or later | 44 kB |
| `marriage_licences` | 98 people named on marriage licences in Mobile County | GPL-2 or later | 13 kB |
| `math_achievement` | 7,185 American schoolchildren and how each scored in mathematics | GPL-2 or later | 75 kB |
| `medical_care_demand` | 4,406 elderly Americans in a 1987 survey and how often each saw a doctor | GPL-2 or GPL-3 | 68 kB |
| `mid_atlantic_wages` | 3,000 men in the mid-Atlantic states and what each earned | GPL-2 | 47 kB |
| `midwest_counties` | 437 counties of the American midwest and how many in each were poor | MIT | 69 kB |
| `monoclonal_gammopathy` | 1,384 patients with monoclonal gammopathy followed to death | LGPL-2 or later | 28 kB |
| `mortgage_denial` | 2,380 Boston mortgage applications and whether each was turned down | GPL-2 or GPL-3 | 47 kB |
| `motor_trend_cars` | 32 cars road-tested by Motor Trend in 1974 and what each did to the gallon | GPL-2 or GPL-3 | 8 kB |
| `movielens` | 100,004 ratings people gave to films | Artistic-2.0 | 2.9 MB |
| `new_york_air` | 153 days in New York in 1973 and the ozone measured on each | GPL-2 or GPL-3 | 7 kB |
| `nyc_flights` | 336,776 flights out of New York in 2013 and how late each arrived | CC0 | 8.0 MB |
| `nyc_weather` | 26,115 hours of weather at the three New York airports | CC0 | 404 kB |
| `occupational_prestige` | 102 Canadian occupations and how each was rated for prestige | GPL-2 or later | 9 kB |
| `oesophageal_cancer` | 88 groups of French men and how many in each had oesophageal cancer | GPL-2 or GPL-3 | 6 kB |
| `old_faithful` | 272 eruptions of Old Faithful and how long the wait before each | GPL-2 or GPL-3 | 7 kB |
| `orange_juice` | 1,070 shoppers and which of two orange juices each bought | GPL-2 | 32 kB |
| `orange_trees` | 35 measurements of orange trees and how far around each had grown | GPL-2 or GPL-3 | 5 kB |
| `orchard_sprays` | 64 cells of a Latin square and how far each spray put the bees off | GPL-2 or GPL-3 | 14 kB |
| `orthodontic_growth` | 108 skull measurements of children followed through adolescence | GPL-2 or later | 6 kB |
| `oxford_boys` | 234 height measurements of twenty-six boys in Oxford | GPL-2 or later | 7 kB |
| `penicillin_testing` | 144 plates of a penicillin assay and how wide the clear zone grew | GPL-2 or later | 6 kB |
| `petroleum_rock` | 48 slices of reservoir rock and how well each let fluid through | GPL-2 or GPL-3 | 6 kB |
| `phenobarbital` | 744 doses given and blood samples drawn from newborn infants | GPL-2 or later | 11 kB |
| `pima_diabetes` | 200 Pima women and whether each tested diabetic | GPL-2 or GPL-3 | 11 kB |
| `professor_salaries` | 397 American professors and what each was paid over 2008 and 2009 | GPL-2 or later | 10 kB |
| `psid_labour` | 753 married women in the 1976 panel study and whether each worked for pay | GPL-2 or GPL-3 | 34 kB |
| `reported_weight` | 200 people who gave both their measured and their reported weight | GPL-2 or later | 9 kB |
| `resume_callbacks` | 4,870 fictitious resumes sent to employers and which drew a call back | GPL-3 | 77 kB |
| `retinopathy_laser` | 394 eyes treated with laser coagulation and how long each kept its sight | LGPL-2 or later | 12 kB |
| `sat_and_gpa` | 1,000 students and the grade average each finished the first year with | GPL-3 | 16 kB |
| `school_absences` | 146 Australian schoolchildren and how many days each missed | GPL-2 or GPL-3 | 6 kB |
| `seat_belt_laws` | 765 American state-years of road deaths and how belt wearing was enforced | GPL-2 or GPL-3 | 28 kB |
| `seattle_pets` | 52,519 licensed pets in Seattle and what kind of animal each is | GPL-3 | 1.3 MB |
| `sleep_deprivation` | 180 reaction times from eighteen drivers kept short of sleep | GPL-2 or later | 7 kB |
| `slid_wages` | 7,425 Ontario workers and what each earned an hour | GPL-2 or later | 69 kB |
| `snail_mortality` | 96 groups of snails held in a laboratory and how many died | GPL-2 or GPL-3 | 6 kB |
| `soybean_growth` | 412 weighings of soybean plants through a growing season | GPL-2 or later | 10 kB |
| `sp500_daily` | 1,250 trading days on the S&P 500 and whether the index rose | GPL-2 | 46 kB |
| `sp500_weekly` | 1,089 weeks on the S&P 500 and whether the index rose | GPL-2 | 45 kB |
| `spruce_growth` | 1,027 measurements of spruce trees grown in ozone chambers | GPL-2 or later | 11 kB |
| `stanford_heart` | 172 follow-up records from the Stanford heart transplant programme | LGPL-2 or later | 9 kB |
| `star_properties` | 96 stars and how hot the surface of each burns | Artistic-2.0 | 7 kB |
| `state_sat_scores` | 50 American states and what each spent on a pupil | GPL-2 or later | 8 kB |
| `steak_preferences` | 550 Americans and how each likes a steak cooked | MIT | 29 kB |
| `student_survey` | 237 Australian statistics students and which hand each wrote with | GPL-2 or GPL-3 | 14 kB |
| `swiss_fertility` | 47 French-speaking Swiss provinces in 1888 and how fertile each was | GPL-2 or GPL-3 | 7 kB |
| `swiss_labour` | 872 Swiss women and whether each was in the labour force | GPL-2 or GPL-3 | 20 kB |
| `tarantino_scripts` | 1,894 curses and deaths counted through seven Tarantino films | MIT | 32 kB |
| `teaching_evaluations` | 463 university courses and how the students rated the teacher | GPL-3 | 17 kB |
| `telecom_churn` | 5,000 phone customers and whether each left | MIT | 163 kB |
| `telecom_contracts` | 7,043 telecom customers and whether each left | MIT | 124 kB |
| `temperature_and_carbon` | 268 years of global temperature anomalies and the carbon burnt in each | Artistic-2.0 | 9 kB |
| `ten_mile_race` | 8,636 runners of the Cherry Blossom race and how long each took | GPL-2 or later | 85 kB |
| `texas_housing` | 8,602 city-months of Texas house sales and the median price in each | MIT | 140 kB |
| `theophylline` | 132 blood samples from twelve subjects given theophylline | GPL-2 or GPL-3 | 7 kB |
| `titanic` | 1,309 people aboard the Titanic and which of them lived | GPL-2 or later | 27 kB |
| `tooth_growth` | 60 guinea pigs given vitamin C two ways and how far their teeth grew | GPL-2 or GPL-3 | 7 kB |
| `travel_mode` | 840 rows of an Australian trip survey, one a way the traveller could have gone | GPL-2 or GPL-3 | 22 kB |
| `uk_smoking` | 1,691 British adults and whether each smoked | GPL-3 | 23 kB |
| `un_national_statistics` | 213 countries and how long a woman born in each could expect to live | GPL-2 or later | 12 kB |
| `us_aircraft` | 3,322 aircraft that flew out of New York and how many seats each had | CC0 | 42 kB |
| `us_airports` | 1,458 American airports and how high above the sea each stands | CC0 | 56 kB |
| `us_arrests` | 50 American states and how many were murdered in each per hundred thousand | GPL-2 or GPL-3 | 7 kB |
| `us_births_1978` | 365 days of 1978 and how many Americans were born on each | GPL-2 or later | 10 kB |
| `us_births_2014` | 1,000 American births in 2014 and how much each baby weighed | GPL-3 | 19 kB |
| `us_cereals` | 65 American breakfast cereals and which company made each | GPL-2 or GPL-3 | 22 kB |
| `us_colleges` | 777 American colleges and whether each was private | GPL-2 | 44 kB |
| `us_economics` | 574 months of American spending, saving and unemployment | MIT | 16 kB |
| `us_gun_murders` | 51 American states and how many gun murders each saw in 2010 | Artistic-2.0 | 7 kB |
| `us_state_education` | 51 American states and how their students scored on the SAT | GPL-2 or later | 7 kB |
| `used_car_prices` | 804 used cars from the 2005 model year and what each was worth | MIT | 17 kB |
| `utility_bills` | 117 monthly gas and electricity bills for one house | GPL-2 or later | 10 kB |
| `verbal_aggression` | 7,584 answers to a questionnaire about wanting to curse, scold or shout | GPL-2 or later | 59 kB |
| `vocabulary_test` | 30,351 Americans given a ten-word vocabulary test and how many each knew | GPL-2 or later | 186 kB |
| `volunteering` | 1,421 people scored on two personality scales and whether each volunteered | GPL-2 or later | 15 kB |
| `warp_breaks` | 54 looms of wool and how often the yarn broke on each | GPL-2 or GPL-3 | 6 kB |
| `wheat_yield_trials` | 224 plots of a wheat variety trial and what each yielded | GPL-2 or later | 8 kB |
| `whickham_smoking` | 1,314 women followed for twenty years and whether each was still alive | GPL-2 or later | 13 kB |
| `windsor_house_prices` | 546 houses sold in Windsor, Ontario and what each fetched | GPL-2 or GPL-3 | 12 kB |
| `womens_labour_1975` | 753 married women in 1975 and whether each worked for pay | GPL-2 or later | 21 kB |
| `workplace_smoking_ban` | 10,000 American workers and whether each smoked | GPL-2 or GPL-3 | 76 kB |
| `world_values_survey` | 5,381 people in four countries and what each thought the state owed the poor | GPL-2 or later | 43 kB |
| `youth_risk_behaviour` | 13,583 American schoolchildren and how much each weighed | GPL-3 | 169 kB |
| `gdp_per_capita_worldbank` | 7,445 country-years of output per person, in 2021 international dollars | CC BY 4.0 | 68 kB |
| `electricity_access` | 7,140 country-years and the share of people with electricity at home | CC BY 4.0 | 42 kB |
| `internet_use` | 6,476 country-years and the share of people using the internet | CC BY 4.0 | 57 kB |
| `consumer_price_inflation` | 9,795 country-years and how fast consumer prices rose in each | CC BY 4.0 | 106 kB |
| `unemployment_rate` | 6,986 country-years and the share of the workforce out of work | CC BY 4.0 | 50 kB |
| `freshwater_withdrawals` | 6,401 country-years and how much fresh water each drew, in cubic kilometres | CC BY 4.0 | 51 kB |
| `air_passengers` | 8,593 country-years and how many air passengers each carried | CC BY 4.0 | 70 kB |
| `mobile_subscriptions` | 9,521 country-years and how many mobile subscriptions each had per hundred people | CC BY 4.0 | 84 kB |
| `internet_users` | 6,006 country-years and how many people used the internet in each | CC BY 4.0 | 53 kB |
| `co2_emissions` | 29,384 country-years of carbon dioxide emitted, in tonnes | CC BY 4.0 | 196 kB |
| `co2_emissions_per_person` | 26,509 country-years of carbon dioxide emitted for every person living there | CC BY 4.0 | 270 kB |
| `cumulative_co2_emissions` | 27,563 country-years of carbon dioxide emitted since 1750, in tonnes | CC BY 4.0 | 203 kB |
| `co2_per_dollar` | 17,528 country-years of carbon dioxide emitted for every dollar of output | CC BY 4.0 | 181 kB |
| `renewable_electricity` | 7,872 country-years and the share of electricity generated from renewables | CC BY 4.0 | 72 kB |
| `electricity_generation` | 7,913 country-years of electricity generated, in terawatt-hours | CC BY 4.0 | 56 kB |
| `renewable_energy` | 6,379 country-years and the share of primary energy that came from renewables | CC BY 4.0 | 66 kB |
| `energy_use_per_person` | 11,225 country-years of primary energy used per person, in kilowatt-hours | CC BY 4.0 | 93 kB |
| `primary_energy` | 13,414 country-years of primary energy used, in terawatt-hours | CC BY 4.0 | 126 kB |
| `cereal_yields` | 13,488 country-years of cereal harvested, in tonnes a hectare | CC BY 4.0 | 105 kB |
| `calorie_supply` | 13,454 country-years and how many calories a day each had for every person | CC BY 4.0 | 102 kB |
| `gdp_per_capita_maddison` | 21,586 country-years of output per person back to the year one, in 2011 dollars | CC BY 4.0 | 155 kB |
| `population_density` | 76,576 country-years and how many people lived on each square kilometre | CC BY 4.0 | 687 kB |
| `urban_population` | 21,052 country-years and the share of people living in towns and cities | CC BY 4.0 | 191 kB |
| `life_expectancy_at_birth` | 18,722 country-years and how long a child born then could expect to live | CC BY 4.0 | 119 kB |
| `child_mortality` | 17,066 country-years and how many children in a hundred died before turning five | CC BY 4.0 | 88 kB |
| `child_mortality_igme` | 13,980 country-years of child deaths per hundred live births, as the UN counts them | CC BY 4.0 | 145 kB |
| `adult_literacy` | 1,833 country-years and the share of adults who could read and write | CC BY 4.0 | 20 kB |
| `human_development_index` | 6,604 country-years scored on health, schooling and income together | CC BY 4.0 | 42 kB |
| `sea_level` | 563 quarterly readings of how far the sea has risen since 1880, in millimetres | CC BY 4.0 | 15 kB |
| `abortion_and_crime` | 19,584 state-years used to ask whether legal abortion cut crime | MIT | 320 kB |
| `adult_services` | 1,787 sessions sold by escorts and what each one was paid for | MIT | 39 kB |
| `affair_counts` | 601 married people and how many affairs each admitted to | GPL-2 | 12 kB |
| `alone_episodes` | 172 episodes of the survival show and how viewers rated each | CC0 | 29 kB |
| `alone_loadouts` | 1,240 items survivalists chose to carry into the wild | CC0 | 23 kB |
| `alone_seasons` | 21 seasons of the survival show and where each was filmed | CC0 | 6 kB |
| `alone_survivalists` | 160 survivalists dropped in the wild and how many days each lasted | CC0 | 20 kB |
| `ancient_shipwrecks` | 1,784 ancient wrecks and how deep in the Mediterranean each lies | GPL-3 or later | 57 kB |
| `animal_attributes` | 20 animals and which of six attributes each of them has | GPL-2 or later | 6 kB |
| `anscombe_quartet` | 44 points of the four sets that share a mean and a fit and nothing else | MIT | 9 kB |
| `ansett_passengers` | 7,407 weeks of passengers flown between Australian cities | GPL-3 | 54 kB |
| `arbuthnot_christenings` | 82 years of London christenings and how many of each sex were baptised | GPL | 8 kB |
| `arctic_pit_houses` | 45 pit houses in arctic Norway and how large each was built | GPL-2 or later | 9 kB |
| `arizona_cardiac_stays` | 3,589 Arizona cardiac patients and how many days each stayed | GPL-2 | 28 kB |
| `arthritis_treatment` | 84 patients given a new treatment for arthritis and how each fared | GPL-2 | 9 kB |
| `ashkenazi_breast_cancer` | 3,920 Ashkenazi women and whether each carried the mutation | CC0 | 30 kB |
| `atmospheric_radiocarbon` | 620 air samples measured for the radiocarbon the bomb tests left | GPL-3 or later | 20 kB |
| `australian_car_policies` | 67,856 Australian car policies and what each claimed in a year | GPL-2 | 737 kB |
| `australian_livestock` | 29,364 months of animals sent to slaughter across Australia | GPL-3 | 194 kB |
| `australian_production` | 218 quarters of Australian beer, bricks, cement and electricity | GPL-3 | 10 kB |
| `australian_retail` | 64,532 months of retail takings by state and by trade | GPL-3 | 577 kB |
| `automobile_claims` | 6,773 car insurance claims and what each one paid out | GPL-2 | 67 kB |
| `bad_health_visits` | 1,127 Germans and how often each went to a doctor in a year | GPL-2 | 11 kB |
| `bakeoff_bakers` | 120 bakers who competed and how far into the series each got | MIT | 18 kB |
| `bakeoff_challenges` | 1,136 bakes and how the baker who made each one fared that week | MIT | 62 kB |
| `bakeoff_episodes` | 94 episodes and how many bakers were left in each of them | MIT | 8 kB |
| `bakeoff_ratings` | 94 episodes and how many people watched each one | MIT | 9 kB |
| `barley_yields` | 120 plots of barley grown in the 1930s and what each yielded | GPL-2 or later | 6 kB |
| `benthic_oxygen_stack` | 2,115 points of the deep-sea record of the last five million years | GPL-3 or later | 23 kB |
| `big_tech_shares` | 5,032 trading days of four big technology shares | GPL-3 | 136 kB |
| `blood_storage` | 316 men transfused during surgery and whether the cancer came back | MIT | 18 kB |
| `bodily_injury_claims` | 1,340 bodily injury claims and what each cost, in thousands | GPL-2 | 22 kB |
| `bone_marrow_leukaemia` | 137 leukaemia patients given a bone marrow transplant | GPL-3 or later | 11 kB |
| `bornholm_brooches` | 77 Danish iron age graves and the brooch types found in each | GPL-2 or later | 45 kB |
| `bowley_wages` | 45 years of the average British wage, in pounds a year | GPL | 5 kB |
| `breast_feeding` | 927 American mothers and how many weeks each breast fed | GPL-3 or later | 14 kB |
| `breslau_life_table` | 100 ages and how many people of each died in Breslau in the 1680s | GPL | 7 kB |
| `bronze_age_cups` | 60 Italian bronze age cups measured and dated to a phase | GPL-2 or later | 8 kB |
| `bundesliga_matches` | 14,018 German league matches and how many goals each side scored | GPL-2 | 145 kB |
| `bundestag_2005` | 80 seat counts by party and state in the 2005 German election | GPL-2 | 6 kB |
| `burn_wound_infection` | 154 burn patients and how long each went before the wound was infected | GPL-3 or later | 9 kB |
| `care_home_incidents` | 1,216 care home inspections and whether the home was found to be failing | MIT | 17 kB |
| `cavendish_density` | 29 weighings of the earth against water | GPL | 6 kB |
| `chinese_bronzes` | 369 Chinese bronzes assayed and dated to a dynasty | GPL-3 or later | 21 kB |
| `cholera_deaths_1849` | 730 days of 1849 and how many Londoners died of cholera or of diarrhoea | GPL | 12 kB |
| `choral_singers` | 235 singers in the New York Choral Society and what each sings | GPL-2 or later | 13 kB |
| `coal_miners_breathing` | 36 groupings of British coal miners by age and by what ailed them | GPL-2 | 6 kB |
| `college_proximity` | 3,010 American men and what each earned, near a college or far from one | MIT | 32 kB |
| `college_scorecard` | 11,300 American colleges and who runs each of them | CC0 | 639 kB |
| `corporal_punishment` | 36 groupings of Germans asked whether children should be smacked | GPL-2 | 6 kB |
| `covid_testing` | 15,524 COVID tests run in 2020 and what each came back as | MIT | 336 kB |
| `csgo_matches` | 1,133 Counter-Strike matches and how each one ended | MIT | 36 kB |
| `cytomegalovirus` | 64 stem cell transplants and whether the virus woke up afterwards | MIT | 17 kB |
| `danish_welfare` | 180 groupings of Danes by drink, income, marriage and where they lived | GPL-2 | 6 kB |
| `dart_points` | 91 dart points from Texas and which of five types each is | GPL-2 or later | 26 kB |
| `datasaurus_dozen` | 1,846 points of the thirteen sets that share every summary there is | MIT | 52 kB |
| `deep_sea_fish` | 147 trawls of the deep Atlantic and how many fish each brought up | GPL-2 | 9 kB |
| `drag_race_appearances` | 2,320 appearances by a queen in an episode and how each placed | GPL-2 | 22 kB |
| `drag_race_contestants` | 184 queens who competed and how old each was on entering | GPL-2 | 11 kB |
| `drag_race_episodes` | 191 episodes and how many queens were still in the running | GPL-2 | 26 kB |
| `drinks_and_wages` | 70 trades in 1910 Britain and what a man in each earned a week | GPL | 7 kB |
| `end_scrapers` | 48 groupings of end scraper shape and how many were found in each | GPL-2 or later | 6 kB |
| `epica_carbon_dioxide` | 1,096 readings of the carbon dioxide trapped in Antarctic ice | GPL-3 or later | 15 kB |
| `ernest_witte_burials` | 49 burials in a Texas cemetery and whether each was given grave goods | GPL-2 or later | 9 kB |
| `esophageal_cancer` | 88 groupings by age, drink and tobacco and how many in each had cancer | MIT | 6 kB |
| `ethanol_engine` | 88 runs of an ethanol engine and how much nitric oxide came out | GPL-2 or later | 7 kB |
| `familial_polyposis` | 22 patients treated for bowel polyps and how many each had a year on | MIT | 6 kB |
| `fingerprint_patterns` | 36 pairings of whorls and loops and how many hands showed each | GPL | 5 kB |
| `fish_adult_growth` | 16,795 readings taken down the ear stones of adult fish | GPL-3 | 178 kB |
| `fish_juvenile_catches` | 496 juvenile fish caught off New Zealand and where each was taken | GPL-3 | 13 kB |
| `fish_juvenile_growth` | 496 juvenile fish and how fast each grew in its first year | GPL-3 | 23 kB |
| `funnel_beaker_pottery` | 118 Neolithic pots outlined by hand and sorted by shape | GPL-2 or later | 20 kB |
| `furze_platt_handaxes` | 600 Acheulian handaxes measured at Furze Platt | GPL-2 or later | 15 kB |
| `galton_families` | 934 children of 205 Victorian families and how tall each grew | GPL | 13 kB |
| `galton_parent_child` | 928 children of Victorian parents and how tall each grew | GPL | 9 kB |
| `geologic_time_scale` | 176 named divisions of geologic time and when each began | GPL-3 or later | 18 kB |
| `german_health_1984` | 3,874 Germans in 1984 and how often each went to a doctor | GPL-2 | 46 kB |
| `german_health_reform` | 2,227 Germans seen before and after the 1997 health reform | GPL-2 | 29 kB |
| `german_suicides` | 306 groupings of German suicides by age, by sex and by method | GPL-2 | 8 kB |
| `global_economy` | 15,150 country-years of output, trade and population | GPL-3 | 539 kB |
| `gosset_yeast_cells` | 36 counts of yeast cells under a haemacytometer | GPL | 5 kB |
| `government_transfers` | 1,948 households either side of the cut-off for a cash transfer | MIT | 32 kB |
| `guerry_moral_statistics` | 86 French departments and the moral statistics Guerry gathered in 1833 | GPL | 14 kB |
| `hare_and_lynx_pelts` | 91 years of pelts traded by the Hudson's Bay Company | GPL-3 | 6 kB |
| `hepatocellular_carcinoma` | 227 liver cancer patients and how long each survived surgery | CC0 | 37 kB |
| `hiv_test_results` | 4,820 Malawians offered a little money to come back for their result | MIT | 55 kB |
| `household_budgets` | 88 country-years of what households owed, saved and spent | GPL-3 | 10 kB |
| `indomethacin_trial` | 602 patients given indomethacin or a placebo after an endoscopy | MIT | 23 kB |
| `infant_pneumonia` | 3,470 infants and how old each was when pneumonia struck | GPL-3 or later | 39 kB |
| `intcal20_curve` | 9,501 points of the curve radiocarbon dates are calibrated against | GPL-3 or later | 114 kB |
| `interaction_triptych` | 2,700 points of the three sets that show an interaction three ways | MIT | 41 kB |
| `iron_age_fibulae` | 30 brooches from an iron age cemetery, measured every way | GPL-2 or later | 8 kB |
| `iron_age_graves` | 52 graves in a Yorkshire cemetery and the goods found in each | GPL-2 or later | 8 kB |
| `jevons_guesses` | 50 guesses at how many beans had been thrown and how far each was out | GPL | 6 kB |
| `kidney_transplant` | 863 kidney transplant patients and how long each lived after surgery | GPL-3 or later | 13 kB |
| `kommos_pottery` | 88 pots from Bronze Age Crete assayed and sorted by ware | GPL-3 or later | 26 kB |
| `laryngoscope_trial` | 99 intubations and how long each took from start to finish | MIT | 10 kB |
| `larynx_cancer` | 90 men with cancer of the larynx and how long each lived | GPL-3 or later | 6 kB |
| `law_dome_gases` | 2,004 years of greenhouse gas read out of Antarctic ice | GPL-3 or later | 31 kB |
| `letters_to_politicians` | 5,593 American legislators written to and which of them wrote back | MIT | 168 kB |
| `licorice_gargle` | 235 patients gargling before surgery and how sore each throat was after | MIT | 11 kB |
| `london_cholera_districts` | 38 London districts and how many in each died of cholera in 1849 | GPL | 9 kB |
| `long_stay_patients` | 768 hospital admissions and whether the patient ended up stranded | MIT | 16 kB |
| `macdonell_criminals` | 924 pairings of height and finger length among three thousand criminals | GPL | 9 kB |
| `medicare_stays` | 1,495 Medicare patients in Arizona and how many days each stayed | GPL-2 | 15 kB |
| `medieval_glass` | 398 pieces of medieval glass assayed for what it was made of | GPL-3 or later | 21 kB |
| `mesolithic_tools` | 33 Mesolithic assemblages and the tools counted in each | GPL-2 or later | 6 kB |
| `michelsberg_pottery` | 109 Neolithic assemblages and which Michelsberg phase each belongs to | GPL-2 or later | 73 kB |
| `minard_troops` | 51 points along Napoleon's march and how many men were still alive | GPL | 6 kB |
| `mississippi_pottery` | 20 sites on the Mississippi and the pottery types found at each | GPL-3 or later | 7 kB |
| `ngrip_ice_core` | 6,114 readings of oxygen isotopes down a Greenland ice core | GPL-3 or later | 77 kB |
| `nightingale_mortality` | 24 months of the Crimean war and what the British army died of | GPL | 7 kB |
| `olympic_running` | 312 Olympic running finals and how fast each was won | GPL-3 | 8 kB |
| `organ_donations` | 162 state-quarters of organ donor registration in America | MIT | 8 kB |
| `oxford_pottery` | 30 Romano-British sites and the share of pottery from the Oxford kilns | GPL-2 or later | 7 kB |
| `ozone_and_weather` | 111 summer days in New York and how much ozone hung in the air | GPL-2 or later | 7 kB |
| `paris_registrations` | 516 months of nineteenth-century Paris and how many women registered | GPL | 10 kB |
| `pearson_lee_heights` | 746 pairings of parent and child height in Edwardian families | GPL | 10 kB |
| `plant_carbon_isotopes` | 155 plants and whether each fixes carbon the C3 way or the C4 way | GPL-3 or later | 10 kB |
| `plant_traits` | 136 plants of north-west France and the traits each one carries | GPL-2 or later | 12 kB |
| `playfair_wheat` | 53 years of the price of wheat set against a labourer's wage | GPL | 6 kB |
| `portal_rodents` | 35,549 animals trapped in the Arizona desert and what each turned out to be | CC0 | 472 kB |
| `portal_species` | 54 species trapped in the Arizona desert and what kind of animal each is | CC0 | 12 kB |
| `prediabetes` | 3,059 patients with prediabetes and how long each took to develop diabetes | MIT | 53 kB |
| `prostate_survival` | 14,294 men with prostate cancer and how long each lived after diagnosis | CC0 | 103 kB |
| `prussian_horse_kicks` | 280 corps-years of the Prussian army and how many horses kicked dead | GPL-2 | 7 kB |
| `rashomon_quartet` | 2,000 points of the set four different models fit equally well | MIT | 74 kB |
| `repeat_victimisation` | 64 pairings of a first crime and a second against the same person | GPL-2 | 6 kB |
| `republican_vote_share` | 50 American states and the Republican share at every election since 1856 | GPL-2 or later | 16 kB |
| `restaurant_inspections` | 27,178 restaurant inspections and what each one scored | MIT | 445 kB |
| `rice_farmer_insurance` | 1,410 Chinese rice farmers and whether each took the insurance offered | MIT | 29 kB |
| `rochdale_women` | 256 groupings of Rochdale women by whether each held a job | GPL-2 | 7 kB |
| `roman_street_networks` | 125 Roman cities and how much of each was given over to streets | GPL-3 or later | 9 kB |
| `romano_british_glass` | 105 pieces of Roman glass assayed and traced to the town that made it | GPL-2 or later | 12 kB |
| `romano_british_pottery` | 48 Roman pots assayed and traced to the kiln that fired them | GPL-2 or later | 19 kB |
| `ruspini_points` | 75 points in the plane that fall into four clusters | GPL-2 or later | 6 kB |
| `sea_level_reconstruction` | 799 points of sea level reconstructed across the last ice ages | GPL-3 or later | 26 kB |
| `ship_damage` | 40 groupings of cargo ships and how many came to harm | GPL-2 | 6 kB |
| `singapore_car_claims` | 7,483 Singaporean car policies and how many claims each one made | GPL-2 | 77 kB |
| `smartpill_motility` | 95 readings from a pill swallowed to time the gut | MIT | 16 kB |
| `smoking_cessation` | 125 smokers given a patch or a combination and how long each held out | CC0 | 9 kB |
| `snodgrass_houses` | 91 house pits at Snodgrass and whether each stood inside the wall | GPL-2 or later | 13 kB |
| `snow_cholera_deaths` | 578 deaths in the Broad Street outbreak, each placed on Snow's map | GPL | 16 kB |
| `std_reinfection` | 877 patients and how long each went before a second infection | GPL-3 or later | 20 kB |
| `stone_age_sites` | 43 Danish stone age sites and the tools found at each | GPL-2 or later | 7 kB |
| `streptomycin_tuberculosis` | 107 patients in the first randomised trial and whether each improved | MIT | 11 kB |
| `stroke_classification` | 5,110 patients and whether each went on to have a stroke | MIT | 76 kB |
| `supported_work_programme` | 445 people in a job training trial and what each earned afterwards | MIT | 12 kB |
| `supraclavicular_block` | 103 nerve blocks and how long each took to numb the arm | MIT | 10 kB |
| `swedish_motorcycles` | 64,548 Swedish motorcycle policies and what each claimed | GPL-2 | 512 kB |
| `texas_prisons` | 816 state-years of prison building and who was locked up in them | MIT | 32 kB |
| `tongue_cancer` | 80 tongue cancer patients and how long each lived after diagnosis | GPL-3 or later | 6 kB |
| `trial_of_the_pyx` | 72 weighings of coin from the royal mint and how far each strayed | GPL | 6 kB |
| `us_regional_mortality` | 400 death rates by region, cause, sex and town or country | GPL-2 or later | 9 kB |
| `victorian_electricity` | 52,608 half-hours of electricity demand in Victoria, and how warm it was | GPL-3 | 841 kB |
| `virgil_dactyls` | 60 counts of how often each foot of a hexameter line was a dactyl | GPL | 6 kB |
| `woodland_birds` | 35 bird species and how many of each were counted in three woods | GPL-3 or later | 6 kB |
| `workers_compensation` | 847 workers compensation losses, by class of work and by year | GPL-2 | 16 kB |
| `xclara_clusters` | 3,000 points in the plane that fall into three clusters | GPL-2 or later | 56 kB |
| `yule_pauperism` | 32 English districts and how poor relief moved with pauperism | MIT | 6 kB |
| `zuni_pottery` | 420 rooms of a Zuni pueblo and the wares found in each | GPL-3 or later | 12 kB |
| `fossil_electricity_share` | 7,182 country-years and the share of electricity burnt out of fossil fuels | CC BY 4.0 | 63 kB |
| `nuclear_electricity_share` | 7,718 country-years and the share of electricity split out of atoms | CC BY 4.0 | 45 kB |
| `wind_electricity_share` | 7,661 country-years and the share of electricity taken from the wind | CC BY 4.0 | 54 kB |
| `solar_electricity_share` | 7,869 country-years and the share of electricity taken from the sun | CC BY 4.0 | 54 kB |
| `hydro_electricity_share` | 7,777 country-years and the share of electricity taken from falling water | CC BY 4.0 | 70 kB |
| `electricity_per_person` | 7,071 country-years of electricity generated for every person living there | CC BY 4.0 | 64 kB |
| `electricity_demand` | 6,378 country-years of electricity asked for, in terawatt-hours | CC BY 4.0 | 42 kB |
| `fossil_fuel_energy` | 11,989 country-years of energy taken from fossil fuels, in terawatt-hours | CC BY 4.0 | 116 kB |
| `electricity_carbon_intensity` | 6,332 country-years and the carbon a kilowatt-hour of electricity cost | CC BY 4.0 | 46 kB |
| `coal_production` | 17,032 country-years of coal dug up, counted in terawatt-hours | CC BY 4.0 | 118 kB |
| `oil_production` | 17,992 country-years of oil pumped, counted in terawatt-hours | CC BY 4.0 | 116 kB |
| `gas_production` | 17,251 country-years of gas drawn, counted in terawatt-hours | CC BY 4.0 | 106 kB |
| `consumption_co2_emissions` | 5,053 country-years of carbon dioxide emitted for what each country used | CC BY 4.0 | 43 kB |
| `methane_emissions` | 38,150 country-years of methane let go, weighed as carbon dioxide | CC BY 4.0 | 282 kB |
| `nitrous_oxide_emissions` | 38,500 country-years of nitrous oxide let go, weighed as carbon dioxide | CC BY 4.0 | 289 kB |
| `greenhouse_gas_emissions` | 38,150 country-years of every greenhouse gas together, weighed as carbon dioxide | CC BY 4.0 | 286 kB |
| `temperature_anomaly` | 531 yearly readings of how far the air has warmed since the 1860s | CC BY 4.0 | 20 kB |
| `sea_surface_temperature` | 531 yearly readings of how far the sea surface has warmed since the 1860s | CC BY 4.0 | 20 kB |
| `ice_sheet_mass` | 384 monthly weighings of the ice lost from Greenland and Antarctica | CC BY 4.0 | 8 kB |
| `annual_precipitation` | 16,770 country-years and how much rain and snow fell on each | CC BY 4.0 | 140 kB |
| `forest_cover` | 8,078 country-years and the share of the land each still keeps under trees | CC BY 4.0 | 75 kB |
| `agricultural_land` | 12,940 country-years and the share of the land each gives over to farming | CC BY 4.0 | 107 kB |
| `fertilizer_use` | 12,606 country-years of fertiliser spread on every hectare of cropland | CC BY 4.0 | 80 kB |
| `pesticide_use` | 8,225 country-years of pesticide sprayed on the fields, in tonnes | CC BY 4.0 | 50 kB |
| `wheat_yields` | 9,799 country-years of wheat harvested, in tonnes a hectare | CC BY 4.0 | 79 kB |
| `maize_yields` | 12,478 country-years of maize harvested, in tonnes a hectare | CC BY 4.0 | 99 kB |
| `rice_yields` | 9,934 country-years of rice harvested, in tonnes a hectare | CC BY 4.0 | 79 kB |
| `cereal_production` | 13,538 country-years of cereal brought in, in tonnes | CC BY 4.0 | 100 kB |
| `cattle_numbers` | 14,468 country-years and how many head of cattle stood in each | CC BY 4.0 | 99 kB |
| `fish_consumption` | 13,220 country-years of fish and seafood eaten for every person living there | CC BY 4.0 | 127 kB |
| `gdp_per_capita_growth` | 12,246 country-years and how fast output per person grew or shrank | CC BY 4.0 | 134 kB |
| `trade_share_of_gdp` | 9,739 country-years and how much of what each made was traded | CC BY 4.0 | 95 kB |
| `foreign_direct_investment` | 10,031 country-years and how much foreign money came in, against output | CC BY 4.0 | 111 kB |
| `labour_force_participation` | 7,186 country-years and the share of grown-ups working or looking for work | CC BY 4.0 | 53 kB |
| `world_population` | 58,824 country-years and how many people lived in each, back to 10,000 BC | CC BY 4.0 | 382 kB |
| `birth_rate` | 18,722 country-years and how many were born for every thousand living | CC BY 4.0 | 118 kB |
| `maternal_mortality` | 9,264 country-years and how many mothers died for every hundred thousand born | CC BY 4.0 | 98 kB |
| `international_migrants` | 2,176 country-years and how many people living in each were born elsewhere | CC BY 4.0 | 23 kB |
| `broadband_subscriptions` | 4,590 country-years and how many broadband lines each had per hundred people | CC BY 4.0 | 55 kB |

Three hundred and fifty of this hand-curated shelf have a number to predict
rather than a class; the rest sort rows into classes. The last column is one file holding
every split, written with the default `zlib`, as measured on a conversion of
the original 627 public sets — every split of a set that has them, a
tree a class, and the `about` key beside them.

The last four hundred come from two places that publish whole shelves at once.
The R teaching tables are read from the CSV Rdatasets serves for each: a
header row naming the columns the way the R package documents them, a first
column of row names, then a row per example. A row-name column that counts
from one is kept as `row`, and one that names the thing measured — a car, a
canton, a state — is kept as `name`; what each may be passed on under is what
its package says, which is why the GPL, the LGPL, the Artistic licence, MIT
and CC0 all appear. The country-year tables are the CSV behind an Our World in
Data chart: a place, its three-letter code where it has one — a continent or
an income group has none — the year, and the one thing measured, which is the
number to predict.

The second two hundred are the same two shelves read further along. From R
come the tables the history of statistics was written from — Arbuthnot's
christenings, Cavendish weighing the earth, Snow's cholera deaths, Galton on
parents and children, Nightingale's mortality of the army, the Prussian horse
kicks — the trials medicine is taught with, what archaeologists dig up and
measure, the quartets built to show that a summary hides the shape of a thing,
and the series forecasting is practised on. A survey question nobody answered
is kept as a class of its own rather than dropped, so `bakeoff_challenges`
sorts a baker into `not_recorded` and `geologic_time_scale` into `unranked`.
A column named after something Python already uses is renamed out of the way:
the German health survey's `self` is written as `self_`. From Our World in
Data come what the world burns and generates, what it lets into the air, how
warm the air and the sea have grown, what the land is put to and what it
yields, and how many people live off it; where a chart measures a band as well
as a value — the temperature anomaly and its low and high — the value is the
number to predict and the band is carried beside it.

Another 499 entries are generated from explicitly licensed, public Hugging
Face dataset repositories with complete scalar Parquet conversions. They add
805 bounded output splits from 1,235 shards and 12.13 GB of source data; 24 expose a
published `ClassLabel`, while 475 expose a named or explicitly designated
numeric teaching target. Every scalar source field is retained. Text becomes
fixed-width UTF-8 bytes with a companion length, missing floats become NaN,
`ClassLabel` metadata becomes an integer, and one `rows` TTree is written per
official split. The crowd-code corpus is additionally partitioned by power-of-two
UTF-8 text length so outlier source rows cannot force a multi-gigabyte fixed-width
ROOT file. The generated [selection manifest](https://github.com/rob-c/PyXRootDClient/blob/main/catalogues/hub-open.json)
records every repository, revision, canonical licence URL, source size and
split. Ambiguous `public` metadata, gated repositories, incomplete conversions
and nested schemas are rejected rather than guessed.

Seventy default large archives use disk-backed readers rather than putting
their sources wholesale into memory. This behavior is independent of origin:
EMNIST and both CIFAR archives use the same retained source cache as UCI, JetNet,
OmniFold Big, TinySOL, WikiText-103, ReefSet, BioDCASE and BirdSet, alongside
37 members of the Hub shelf. A dataset's
complete unique source
payload must be at least 100 MB and strictly below 2,000,000,000 bytes for
`--large` by default; `convert` enforces the same ceiling when it fetches the
declared source. `--allow-oversize` (alias `--no-size-limit`) removes the upper
source-selection bound for registered converters, as does
`convert(..., allow_oversize=True)` in Python. Exact published source-size
checks remain in force. Explicit local `parts=` remain available for converter
tests and private data without claiming them as catalogue downloads.

| name | retained conversion | compressed source |
|---|---|---:|
| `emnist` | selected IDX members are decoded per official split; pixels are unchanged | 535.7 MiB |
| `cifar10` | binary RGB planes and official train/test split are preserved | 162.2 MiB |
| `cifar100` | binary RGB planes, fine labels, coarse labels and official split are preserved | 160.7 MiB |

The UCI archives below the ceiling have purpose-built streaming readers:

| name | ROOT rows | compressed source |
|---|---|---:|
| `susy` | 5,000,000 collision events, official last-500,000 test split | 879.6 MiB |
| `multimodal_damage` | 640×640 RGB images paired with their captions | 1.05 GiB |
| `pems_sf` | one 963×144 occupancy matrix per day, official train/test split | 104.4 MiB |
| `physical_unclonable_functions` | 64- and 128-bit challenges in four official partitions | 152.4 MiB |
| `daily_sports_activities` | one 125×45 motion segment per entry | 162.9 MiB |
| `gas_sensor_temperature` | environmental controls and 14 resistance channels | 174.8 MiB |
| `twin_gas_sensor_arrays` | padded ten-minute, eight-channel exposure records | 194.6 MiB |
| `electricity_load_diagrams` | quarter-hour loads for all 370 clients | 249.2 MiB |
| `opportunity_activity` | raw sensor timestamps and locomotion labels | 292.4 MiB |
| `gas_sensor_dynamic_mixtures` | concentrations and 16 raw sensor channels | 351.9 MiB |
| `p53_mutants` | 5,408 molecular features and activity label | 527.0 MiB |
| `pamap2` | timestamps, 52 measurements and activity labels | 656.3 MiB |
| `hhar` | heterogeneous phone/watch motion rows | 784.0 MiB |
| `year_prediction_msd` | 90 timbre statistics and release year, official train/test split | 201.2 MiB |

Fifty-four more explicitly licensed archive families are admitted: JetNet (five
HDF5 shards, 436.5 MB total), OmniFold Big (817.9 MB), TinySOL (1.03 GB),
WikiText-103 (313.1 MB), ReefSet (1.63 GB), BioDCASE 2025 Task 3 (224.7 MB),
Speech Commands v0.01 (1.49 GB), AudioMNIST (1.91 GB through its fixed
Hugging Face mirror), CirCor heart sounds (471.3 MB), BirdSet BASEAL
(225.2 MB), UAV Maize Stress (1.18 GB), Wildlife MNIST (1.48 GB), SODv2
(14.5 MB), All-Sky Cloud Segmentation Almeria (16.8 MB), 17 CC BY MedMNIST
28-pixel classification/volume sources (686.0 MB together), Galaxy10 SDSS
(210.2 MB), Mars Surface Image v1 (60.6 MB), and SWEFil (151.5 MB).

The condensed-matter shelf contributes 20 individually buildable problems. Seven are
image-first collections, and every source remains below the strict two-gigabyte ceiling:

| name | ROOT representation | compressed source |
|---|---|---:|
| `jarvis_stm_bravais` | paired positive/negative-bias RGB STM images; five Bravais TTrees | 313.9 MB |
| `nffa_sem_compact` | grayscale SEM images; five nanomaterial-morphology TTrees | 1.97 GB |
| `moke_skyrmion_segmentation` | MOKE intensity image plus background/skyrmion/defect mask | 957.2 MB |
| `wse2_stm_defects` | float32 atomic STM patch plus three-class defect mask and author split | 1.79 GB |
| `tem_nanoparticle_morphology` | grayscale TEM images; three balanced assembly TTrees | 1.57 GB |
| `polymer_blend_afm` | five aligned, unnormalised float32 AFM channels | 483.8 MB |
| `perovskite_sem_segmentation` | RGB SEM image plus rasterized phase/defect mask, retaining image/annotation geometry | 77.1 MB |

The other 13 are the complete Matbench v0.1 task suite: `matbench_dielectric`,
`matbench_expt_gap`, `matbench_expt_is_metal`, `matbench_glass`,
`matbench_jdft2d`, `matbench_log_gvrh`, `matbench_log_kvrh`,
`matbench_mp_e_form`, `matbench_mp_gap`, `matbench_mp_is_metal`,
`matbench_perovskites`, `matbench_phonons`, and `matbench_steels`. Composition
tasks retain the formula and add a normalized 8×16 elemental image. Structure tasks
also retain the 3×3 lattice and every site/species occupancy, plus three 32×32
fractional-coordinate projections. The Matbench gzip JSON is streamed row by row.

The JARVIS visual-physics shelf adds another 100 independently buildable
regression problems: 50 from the 93,902-crystal JARVIS-DFT 3D snapshot and the
same 50 concepts from the 1,103-crystal JARVIS-DFT 2D snapshot. They are the
Cartesian product of these source prefixes and target groups:

| name prefix | logical tasks | pinned compressed source | canonical record |
|---|---:|---:|---|
| `jarvis_dft3d_` | 50 | 48.45 MB | [JARVIS-DFT 3D](https://doi.org/10.6084/m9.figshare.6815699.v11) |
| `jarvis_dft2d_` | 50 | 8.39 MB | [JARVIS-DFT 2D](https://doi.org/10.6084/m9.figshare.6815705.v8) |

Thirty-two targets preserve published calculated observables: formation and
total energies, OptB88vdW/MBJ gaps, density, energy above hull, cutoff and
k-point parameters, atom count, two magnetic moments, six dielectric-axis
responses, electron/hole effective mass, two band-difference measures, eight
n/p thermoelectric quantities, SLME, spin-orbit spillage and exfoliation
energy. Eighteen transparent teaching targets are derived from the published
structure: lattice lengths and angles, volume, volume per atom, length/angle
statistics, anisotropy, element count and atomic-number statistics.

Every task retains the JARVIS id, formula, lattice, padded atomic numbers and
fractional positions. It also supplies a normalized 8×16 elemental image and
three 32×32 orthogonal crystal projections. Numeric-id modulo ten provides a
stable 80/10/10 train/validation/test tree partition; this project-created
partition is stated on every generated detail page. The two input ZIPs have
shared cache names, so building all 100 tasks downloads 56.84 MB once rather
than downloading the same structures 100 times. The task files intentionally
repeat the input representation so each `.root` remains independently usable:

```console
$ xrd-datasets build /nfs/datasets --only 'jarvis_dft*' \
    --source-cache /nfs/dataset-sources --jobs 4
```

The Alex-MP-20 shelf adds another 100 visual materials-physics regressions over
675,204 inorganic structures and preserves OMatG's train/validation/test split.
Its 197.51 MB of CC BY 4.0 Parquet shards are shared in the source cache by all
tasks. Six targets are published observables, 32 are documented cell and
aggregate-composition derivations, and 62 are elemental stoichiometric fractions.
Every row retains the cell, padded atoms and source identifiers together with a
normalized 8×16 elemental image, an occupancy-only three-plane image and three
atomic-number 32×32 crystal projections:

```console
$ xrd-datasets build /nfs/datasets --only 'alex_mp20_*' \
    --source-cache /nfs/dataset-sources --jobs 2
```

For an elemental-fraction exercise, choose `occupancy_projection` as the model
input instead of `atomic_numbers`, `element_image` or `projection`; the latter
branches deliberately retain exact composition for provenance and would leak
that target. The canonical MatterGen paper, the OMatG repository and the
Alexandria source collection are linked from every generated detail page.

The `well_*` shelf adds 100 visual field-learning tasks from sixteen distinct
CC BY 4.0 repositories in [The Well](https://polymathic-ai.org/the_well/).
Its domains include acoustic and Helmholtz waves, active matter,
reaction-diffusion, planetary atmospheres, convection, hydrodynamic
instabilities, non-Newtonian flow, plasma mixing and stellar/relativistic
astrophysics. Six common tasks per source predict the next state, temporal
change, gradient magnitude, Laplacian, an above-mean mask and next-state mean;
four sources add RMS regression.

Each entry is a traceable 64×64 central plane containing raw and min-max
normalized input values, validity masks, a transformed target image, scalar
target, source field name and original limits. Sixteen pinned HDF5 objects are
shared across the 100 outputs. Eight are admitted by the default ceiling; the
complete 86.52 GB physical source shelf needs the explicit production opt-in:

```console
$ xrd-datasets build /nfs/datasets --only 'well_*' --allow-oversize \
    --source-cache /nfs/dataset-sources --jobs 1
```

This is deliberately an educational slice, not a claim to mirror The Well's
full 15 TB. Every detail page links the domain-specific documentation and
paper, identifies the selected upstream test shard, and states the derived
contiguous temporal split.

[`xrd.ml.load_image_2d`](ml.md#raw-and-normalized-2d-crystal-images) reads one
entry and reconstructs any `xy`, `xz` or `yz` plane as both its raw
atomic-number image and a normalized floating-point image. Its optional plot
puts the two side by side; `examples/jarvis_2d_visualize.py` can display or
save the result directly from a local path, hosted URL or catalogue name.

Wildlife MNIST's float32 RGB arrays remain channel-first and at their
published scale; its official non-mixed training and mixed test labels become
digit, background and foreground branches under split-and-digit TTrees. The maize converter
joins four ZIP shards, writes the water and rust source orthomosaics as padded
224×224 six-band tiles, casts the published float16 derived patches to float32,
and keeps their five-class masks and coordinates in separate source/patch TTrees.
All 229 default large registrations total 93.70 GB when logical task sources are
summed; the public build withholds the two unlicensed CIFAR sources and reports
93.36 GB. Shared cache names reduce the physical source fetch to about
41.15 GB. Sources are cached on disk and read a member or record at a time; allow 500 GiB for the
source cache, expanding output, temporary nested members and writer baskets
on a first complete pass. JetNet's HDF5 and the multimodal archive's JPEG
formats use optional readers, as does WikiText-103's Parquet source:

The explicit oversized selection adds eight UCI archives, all with
purpose-built, bounded-source readers:

| name | retained conversion | compressed source |
|---|---|---:|
| `higgs` | float32 collision features; official train/test boundary; signal/background TTrees | 2.82 GB |
| `realdisp` | timestamps, subject/placement metadata and 117 sensor readings; activity TTrees | 2.67 GB |
| `cuffless_blood_pressure` | HDF5 PPG, pressure and ECG signals in length-preserving 1,000-sample windows | 3.36 GB |
| `ppg_dalia` | synchronized wrist signals in overlapping eight-second windows with subject and heart-rate target | 2.87 GB |
| `medical_deepfakes` | original signed 512×512 DICOM pixels, calibration and tamper labels | 6.40 GB |
| `gas_sensor_arrays_open_sampling` | controls and 72 sensor series padded with their true lengths; chemical TTrees | 8.37 GB |
| `hepmass` | all six gzip CSV files; three hypotheses and their official train/test splits | 7.89 GB |
| `chipseq` | run-length bedGraph coverage split at published weak-label boundaries; label TTrees | 37.28 GB |

Together the default and oversized selections are 287 registered converters
and 681.39 GB of declared logical source payload (285 and 681.05 GB in the public build,
which still withholds CIFAR-10 and CIFAR-100). Start an oversized production
pass with one conversion job and at least 1 TiB of working space. Shared-cache
deduplication reduces the actual source fetch to about 193.10 GB; ROOT output
and temporary space must still be measured on the production filesystem.

```console
$ pip install 'pyxrootdclient[datasets]'
```

Nothing is redistributed here. Each set is fetched from whoever publishes it,
on the machine doing the converting, and the licences above are what those
publishers say — read them before passing the converted file on. The CIFAR
sets have no formal licence at all; Krizhevsky asks that the tech report be
cited. So that a file outlives the program that made it, `convert` writes an
`about` key beside the trees holding the same statement:

```python
with xrd.root.open_root("cifar10.root") as f:
    print(f["train_about"])
# CIFAR-10: 60,000 photographs, 32x32 colour, 10 classes
# split: train
# licence: no formal licence; Krizhevsky asks that the tech report be cited
# source: https://www.cs.toronto.edu/~kriz/cifar.html
# transformation: extracted the publisher's binary records; preserved 32x32
# RGB-plane unsigned-byte pixels, labels and official train/test split without
# normalization or augmentation; wrote one ROOT TTree per class
# layout: one tree per class
```

The CIFAR sets are taken in their **binary** distribution rather than the
Python one, on purpose: that Python distribution is a pickle. The ordinary
archive readers — IDX, tar, zip, a zip inside a zip, gzip, WAV, ARFF, CSV and
the XML a spreadsheet keeps inside its own zip — use the standard library.
PPG-DaLiA itself publishes NumPy arrays in a pickle; its large reader uses a
restricted unpickler that permits only NumPy's inert array constructors and
built-in containers, never an arbitrary class or callable from the download.

A table that gives its names in a header takes them from the first line that is
neither blank nor a comment, because a file is as likely to name its columns
after a preamble as before one. A column of dates holds the days since 1970; a
column of timestamps holds the seconds, so a set recorded every quarter of an
hour — `occupancy`, `steel_industry` — keeps its clock rather than collapsing
onto the day it was taken. Either way a missing value is `-1`.

Images come out exactly as the archive laid them: CIFAR is 3072 bytes an
entry, 1024 red then 1024 green then 1024 blue, which is what PyTorch wants,
so the loop is the MNIST one with a wider `view`:

```python
loaders = [
    torch.utils.data.DataLoader(
        xrd.root.ml.dataset(f[f"train_{cls}"], ["image", "label"], step=64),
        batch_size=None,
    )
    for cls in (
        "airplane",
        "automobile",
        "bird",
        "cat",
        "deer",
        "dog",
        "frog",
        "horse",
        "ship",
        "truck",
    )
]
for parts in zip(*loaders):
    x = torch.cat([part["image"] for part in parts]).float().div_(255)
    loss = criterion(model(x.view(-1, 3, 32, 32)), ...)
```

EMNIST is MNIST's shape — 28×28 IDX, the same reader — but NIST wrote its
images with the rows and the columns swapped, and they are kept that way here
rather than quietly turned round. Measure a `1` and you can see it: in MNIST
the ink runs down rows 4–24 and across columns 9–19, in EMNIST down rows 9–19
and across columns 3–25. Each tree says so in its title, so a `view(28, 28)`
that comes out sideways has an answer in the file itself:

```python
x = torch.from_numpy(...).view(-1, 28, 28).transpose(1, 2)  # if you want it upright
```

The set converted is the **balanced** split, 47 classes: the ten digits, the
26 capitals, and the eleven lower-case letters whose shape differs from the
capital — `a b d e f g h n q r t`. The other fifteen were merged into their
capitals by NIST because nothing in a 28×28 bitmap tells `c` from `C`.

### Detection and pixel masks

Twenty-two open sources now provide visual problems tied directly to astronomy,
planetary and atmospheric physics, microscopy and medical imaging. The latest
20 are the 17 redistributable MedMNIST subsets plus Galaxy10 SDSS, the Curiosity
rover collection and SWEFil; DermaMNIST is deliberately absent because its
CC BY-NC terms do not permit the public mirror.

| name | task retained in ROOT | complete source |
|---|---|---:|
| `pathmnist`, `chestmnist`, `octmnist`, `pneumoniamnist`, `retinamnist`, `breastmnist`, `bloodmnist`, `tissuemnist` | compact histology, radiography, OCT, ultrasound and microscopy image tasks with official splits | 511,802,078 B total |
| `organamnist`, `organcmnist`, `organsmnist` | axial, coronal and sagittal CT organ classification | 70,302,478 B total |
| `organmnist3d`, `nodulemnist3d`, `adrenalmnist3d`, `fracturemnist3d`, `vesselmnist3d`, `synapsemnist3d` | 9,996 compact 28³ CT, vessel, shape-mask and electron-microscopy volumes | 103,944,897 B total |
| `galaxy10_sdss` | 21,785 69×69 SDSS/Galaxy Zoo morphology examples in ten classes | 210,234,548 B |
| `mars_surface_images` | 6,691 roughly 256-pixel Curiosity images in publisher train/validation/test splits | 60,635,475 B |
| `swefil` | 554 raw/processed H-alpha images, 4,144 COCO boxes and four overlap-preserving masks | 151,465,835 B |
| `sodv2` | 600 simulated orbital scenes, 1,339 normalized YOLO boxes, official train/validation split and near/mid/far distance TTrees | 14,516,386 B |
| `allsky_cloud_segmentation` | 818 all-sky images paired with unchanged five-class masks, official train/validation/test split and four-camera test set | 16,828,503 B |

Every MedMNIST file keeps its published 28×28 or 28×28×28 uint8 tensor with no
new scaling and preserves `train`, `validation` and `test`. Single-label tasks
write one TTree per class; ChestMNIST writes one `samples` tree with its
fourteen-wide binary `targets` vector. The NPZ members are safely extracted and
memory-mapped, so TissueMNIST's 236,386 cells do not become one Python-memory
copy. The generated pages credit the eight MedMNIST creators, link the versioned
Zenodo origin and licence, and cite the dataset paper.

Galaxy10 keeps the publisher's complete HDF5 array as 69×69×3 uint8 pixels and
the ten Galaxy Zoo morphology labels; no unofficial split is invented. Mars
converts its small grayscale subset to RGB and letterboxes variable source
geometry within 256×256 while retaining source dimensions, padding, sol,
instrument and product id. Its 25 published category ids become TTrees, with
the unused `sun` id correctly left empty because the official manifests contain
24 populated categories.

SWEFil is the requested real solar-physics detection and masking problem. Each
2048×2048 GONG JPEG is resized to 512×512 RGB; every COCO polygon is rasterized
into separate QRF, IRF, ARF and sunspot masks, so overlaps are not erased.
Forty normalized source boxes, category ids, source areas and annotation ids
are padded beside a true `objects` count. Separate `processed` and `raw` TTrees
retain the official train/test division and observatory/timestamp identity.

SODv2's `image` is a channel-last `794×706×3` uint8 row. `boxes` is a
16×4 float32 tensor in the source's normalized `(centre_x, centre_y, width,
height)` convention; `objects` says how many rows are real, and the unused
tail is zero-padded. `object_labels` is zero for each satellite and `-1` in
the padding. Trees retain the publisher's three observation-distance strata,
for example `train_mid_0_5_to_2_km`.

The all-sky `image` is channel-last `512×512×3` uint8 and `mask` is a
`512×512` uint8 semantic map. Values remain exactly the publisher's `0`
camera mask, `1` sky, `2` low cloud, `3` mid cloud and `4` high cloud.
`class_fractions`, camera id, acquisition timestamp and camera coordinates sit
beside each pair. The source's validation CSV defines the 616/154 training and
validation partition; the 48-row test tree keeps 12 observations from each of
four independent imagers.

```console
$ python -m pip install 'pyxrootdclient[datasets]'
$ xrd-datasets build /nfs/datasets \
    --only swefil --only galaxy10_sdss --only mars_surface_images \
    --only pathmnist --only organmnist3d \
    --source-cache /nfs/dataset-sources --jobs 1
$ xrd-datasets verify /nfs/datasets
```

No synthetic mask or box is inferred during conversion: JPEG/PNG pixels are
decoded to model-ready unsigned bytes, annotations are joined by filename and
their coordinate or class convention is kept. The ROOT file's `about` key and
the generated site credit the creators and publisher, link the canonical
record or repository and state this transformation.

Sound is the same idea with a longer row. Every raw-audio converter decodes
signed 16-bit PCM to float32 in `[-1, 1)` before writing it; models therefore
receive waveform values directly rather than compressed audio bytes or integer
amplitudes. `fsdd` is 3,000 WAV recordings read
with the standard library's `wave`, and because a tree column is a fixed size,
every clip is written into a 20,000-sample column — 2.5 seconds at 8 kHz — with
a `length` beside it saying how much of that is recording and how much is the
zeroes after it. A clip longer than the column is refused by name rather than
truncated, and so is a file that is not 8 kHz mono 16-bit, which is what
`speaker` being a column can be trusted against. Its publisher's repetition
rule becomes `train_0` … `train_9` and `test_0` … `test_9` trees:

```python
with xrd.root.open_root("fsdd.root") as f:
    for batch in xrd.root.ml.iter_tensors(f["train_7"], ["audio", "length", "speaker"], step=32):
        wave = batch["audio"]  # float32, already normalized to [-1, 1)
        mask = torch.arange(20000) < batch["length"][:, None]
        loss = criterion(model(wave * mask), ...)
```

The other waveform sets exercise different teaching problems while remaining
below the default two-gigabyte source ceiling:

| name | waveform layout in ROOT | source payload |
|---|---|---:|
| `speech_commands_v001` | 16,000 float32 values, official train/validation/test lists, 30 words plus one-second noise windows | 1.489 GB |
| `audiomnist` | 48,000 float32 values with speaker, repetition, age, sex, accent and origin metadata | 1.907 GB |
| `circor_heart_sound` | non-overlapping five-second 4 kHz windows with patient, auscultation, murmur and outcome labels | 471.3 MB |
| `tinysol` | lossless ten-second chunks per orchestral note, one tree per instrument | 1.027 GB |
| `reefset` | 30,720 values plus the source's acoustic and recorder provenance | 1.629 GB |
| `biodcase_2025_task3` | up to 32,000 values with the official training/validation split | 224.7 MB |

TinySOL's publisher describes clips as two to ten seconds, but the distributed
archive contains longer valid PCM recordings. The converter never truncates
them: `recording` identifies the original WAV, `recording_length` preserves its
total frame count, and `chunk`, `chunks`, and `length` describe the contiguous
ten-second ROOT rows needed to reconstruct it exactly.

Build only the waveform shelf with one conversion job so archive inflation and
ROOT compression do not compete for the same disk:

```console
$ xrd-datasets build /nfs/datasets \
    --only fsdd --only speech_commands_v001 --only audiomnist \
    --only circor_heart_sound --only tinysol --only reefset \
    --only biodcase_2025_task3 \
    --source-cache /nfs/dataset-sources --jobs 1
$ xrd-datasets verify /nfs/datasets
```

AudioMNIST is only 92,968,703 bytes below the strict two-gigabyte source
ceiling and its decoded 48 kHz float rows can occupy several gigabytes before
ROOT compression. Keep both the source cache and output on the production NFS
mount for this pass.

AudioMNIST is the useful mirror example: `origin` names the authors' GitHub
project, while `source`, `repository`, and `mirrors` identify the fixed
Hugging Face adaptation that supplies the three registered archives. Those
roles are separate in `index.json`, the generated HTML, JSON-LD, and each
ROOT file's `about` key.

Some sets are neither pictures nor a table of named fields, but one long row
of numbers an example — and those become one fixed-size column called
`features`, which is what a training loop wants of 561 of them and what
`iter_tensors` hands straight to a tensor. The awkward part of those sets is
never the numbers, it is where the publisher put the labels, so all three
places are read:

```python
datasets.convert("miniboone", "miniboone.root")
# {'electron_neutrino': 36499, 'muon_neutrino': 93565}
```

`miniboone` is the one to point at when somebody says HEP formats are not for
machine learning: it is a particle-identification set from a neutrino
experiment, published for exactly this teaching purpose, and it goes back into
a ROOT file where it started. Its file has no labels in it at all — the first
line says `36499 93565`, meaning the first 36,499 rows are signal and the rest
background — so the label comes from where a row lies, and a file with more
rows than the counts promised is refused rather than labelled by guesswork.

`har` keeps its labels in a file beside the numbers, inside a zip inside the
zip, with the volunteer's number in a third file; all three are read together
and a mismatch between their lengths is an error, not a silent truncation.
`semeion` writes its label as ten columns at the end of each row, one of them
set — anything other than exactly one set is refused. Its 256 pixels are 16×16
black and white, so it reads like a smaller MNIST:

```python
with xrd.root.open_root("semeion.root") as f:
    row = f["3"]["image"].array(0, 1)
    for line in range(16):
        print("".join("#" if pixel else "." for pixel in row[line * 16 : line * 16 + 16]))
```

Text works the way sound does. `sms_spam` is 5,574 messages, and since a tree
column is a fixed size each one is written into 1,024 bytes of UTF-8 with a
`message_length` beside it saying where the real text stops:

```python
with xrd.root.open_root("sms_spam.root") as f:
    tree = f["spam"]
    held, upto = bytes(tree["message"].array(0, 1)), tree["message_length"].array(0, 1)[0]
    print(held[:upto].decode())
# Free entry in 2 a wkly comp to win FA Cup final tkts 21st May 2005. ...
```

That file is tab-separated, and reading it as an ordinary CSV would quietly
change 536 of its messages and lose two rows entirely, because a message that
opens with a quotation mark swallows everything up to the next one. English
means its quotes literally, so `Table` sets `quoted=False` and the text
arrives as written.

The tabular sets become one column per field instead of one wide array:

```python
with xrd.root.open_root("penguins.root") as f:
    print(xrd.root.ml.numeric(f["gentoo"]))
# ['island', 'bill_length_mm', 'bill_depth_mm', 'flipper_length_mm',
#  'body_mass_g', 'sex', 'year', 'label', 'index']
```

Trees hold numbers, so the categories are numbered: `datasets.DATASETS[
"penguins"].codes` says which is which, and a category nobody declared is
refused rather than guessed at. A measurement nobody took becomes a NaN, which
is what every reader downstream already means by it; a category nobody
recorded becomes `-1`, because there is no such code.

Not every set has classes. Half of what regression is taught with is a table
with a number at the end of the row — the miles a car did on a gallon, the
bicycles hired in an hour, the mark a pupil finished on — and forcing that into
classes would be inventing bins nobody published. A set with no `classes` has a
`"target"` field instead of a `"label"` one, and it comes out as a single tree
called `rows`, with the number to predict a `double` column beside the features:

```python
datasets.convert("auto_mpg", "auto_mpg.root")
# {'rows': 398}

with xrd.root.open_root("auto_mpg.root") as f:
    print(xrd.root.ml.numeric(f["rows"]))
# ['mpg', 'cylinders', 'displacement', 'horsepower', 'weight',
#  'acceleration', 'model_year', 'origin', 'car_name_length', 'index']
```

There is no `label` column in those files, because there is nothing for it to
say, and `describe()` says `no classes, a number to predict` where it would
otherwise count them. `energy_efficiency` has two targets rather than one — a
building's heating load and its cooling load — which is nothing special here:
they are two columns.

`auto_mpg` also shows the older way of ending a row. Its eight numbers are
separated by runs of spaces and then, after a tab, the car is named in quotes:
`18.0 8 307.0 130.0 3504. 12.0 70 1  "chevrolet chevelle malibu"`. Splitting
that on whitespace turns one name into three fields, so `tail=` says how many
fields the line really has; the line is split that many times and no further,
and the name arrives in a `car_name` column with a `car_name_length` beside it
the way any text does.

Dates are a column too. `bike_sharing` is a time series — a count of hires an
hour for two years — and a `"date"` field becomes the days since 1970 as an
`int`, which sorts, subtracts and plots without a parser at the other end:

```python
import datetime

with xrd.root.open_root("bike_sharing.root") as f:
    day = f["rows"]["date"].array(0, 1)[0]
    print(datetime.date(1970, 1, 1) + datetime.timedelta(days=day))
# 2011-01-01
```

`dates=` says how the file writes them, in `strptime` terms, and a date written
some other way is refused with that format quoted back rather than guessed at.

Two of these sets are published only as spreadsheets, so `read_xlsx` reads one.
A modern spreadsheet is a zip of XML with the text kept once in a shared table
and referred to by number, which is why a sheet on its own reads as nonsense
and this does not — and it needs nothing that is not already in the standard
library. Rows of nothing at all, and the empty cells trailing a row, are not
data and are dropped; a gap in the middle of a row is kept, so the fields after
it still line up:

```python
datasets.convert("energy_efficiency", "energy.root")
# {'rows': 768}
```

Tables do not all arrive as comma-separated files, so `Table` says which shape
it is and the reader does the rest: `delimiter=None` splits on whitespace
(`seeds`), `delimiter=";"` with `header=True` reads the shape a spreadsheet
exports (`wine_quality`), `comment=` drops the lines a publisher wrote above
the data (`adult`), `arff=True` skips everything down to the `@data` line of a
Weka file (`dry_bean`), `xlsx=True` reads the spreadsheet itself
(`energy_efficiency`), `inner=` opens the zip inside the zip (`student`), and
`files=` picks a different member of the one archive for each split — which
need not be train and test: `wine_quality`'s two are `red` and `white`, and
`heart_disease`'s four are the hospitals that gathered it, Cleveland, Hungary,
Switzerland and Long Beach. `codes=` may name its categories as a mapping or
just list them in order — `mushroom`'s 22 columns are the letters UCI
documents, in UCI's order, and `car_evaluation`'s six are listed worst to best,
so the numbers in the file mean what the published description says they mean.

`magic` and `htru2` are the two to point at beside `miniboone` when somebody
says HEP formats are not for machine learning: air showers in a Cherenkov
telescope, sorted into gamma rays and hadrons, and pulsar candidates from a
radio survey, sorted into pulsars and everything else. Both are ordinary
teaching sets on every benchmark list, and both go back into a ROOT file
without anybody having to adapt to us.

Converting is one pass, and `parts=` takes archives already on disk when you
would rather not download them twice:

```python
datasets.convert(
    "cifar100", "cifar100.root", split="test", parts={"archive": "cifar-100-binary.tar.gz"}
)
```

`base=` points the downloads at a mirror of your own, and `target` may be a
`WritableFile` already open, which is how several splits end up in one file:

```python
from xrd.root import create, datasets

with create("mnist.root") as out:
    for split in ("train", "test"):
        datasets.convert("mnist", out, split=split)
```

Adding a dataset of your own is an `Images`, `CIFAR`, `Audio`, `Matrix` or
`Table` in the registry — each is a frozen dataclass saying where the data is
and what its fields mean, with no code to write for the common shapes.

### Training off a `root://` URL

Converted, a dataset is a ROOT file like any other, so a training loop reads it
the way it would read events. If there is no storage element to hand, share the
directory over `root://` yourself — an unprivileged port, no login, no daemon:

```console
$ python -m xrd.testing datasets --port 21094 --pattern '*.root'
serving 5 files on root://127.0.0.1:21094/ with no login
```

Then the ten class trees are one stream, mixed a batch at a time. Nothing is
downloaded first; each pull is one basket off the wire:

```python
import torch
from xrd.root import open_root
from xrd.root.ml import mixed

URL = "root://127.0.0.1:21094//home/you/datasets/mnist.root"

with open_root(URL) as handle:
    trees = [handle[f"train_{cls}"] for cls in range(10)]
    loader = torch.utils.data.DataLoader(
        mixed(trees, ["image", "label"], step=512, batch=256, device="cuda"), batch_size=None
    )
    for batch in loader:  # 60,000 rows an epoch
        train(batch["image"].float().view(-1, 1, 28, 28) / 255, batch["label"].long())
```

Three programs that run as they stand — an MLP, an autoencoder and a small
convolutional net — are in [Training playbooks](playbooks.md).

A regression set is simpler still, being one tree of every row, with the number
to predict a column beside the rest:

```python
with open_root("root://127.0.0.1:21094//victorian_electricity.root") as handle:
    loader = torch.utils.data.DataLoader(
        dataset(handle["rows"], ["temperature", "holiday", "time", "demand"], step=4096),
        batch_size=None,
    )
```

## Compression

Every algorithm ROOT writes with is read here — and written: zlib, lzma, LZ4,
zstd, and the bare-deflate blocks ROOT wrote before 2005 (read only). The LZ4
coder is Python both ways, checksum and all, because a physics file should not
need a wheel; zstd uses Python 3.14's own `compression.zstd` where there is
one and the `zstandard` package otherwise, and is the one case where a file
may need something installed.

## Old files

A tree written by ROOT 4 opens like any other. Those files count entries in
doubles and keep their seek points in 32-bit integers, and one small enough
never to have been flushed holds its baskets inside the branch record rather
than out in the file — all of which is read here, so a decade-old Geant4 run
needs no copying forward first. ROOT 3 and older are refused by name.

## What it refuses, and why by name

A plausible misreading of physics data is worse than a refusal, so anything
this reader does not understand is named rather than guessed at. A column that
cannot be decoded is listed in `tree.unreadable` with the reason, and raises
with the same sentence if it is asked for:

```python
>>> tree.unreadable
{'vtx': 'Vertex, which is a C++ type this reader does not decode: this '
        "file's streamer information does not describe its layout, and a "
        'file written split would have its members as branches of their own'}
```

What is named that way:

- a class whose layout the file's streamer information does not describe, so
  that reading it whole would be a guess;
- a member of an unsplit object of a kind this reader has no reader for, which
  stops the entry it is in, because an unsplit entry is read from first byte to
  last and there is no length in front of a member to step over it by;
- a `multimap`, whose duplicate keys a `dict` would silently drop, and a map
  keyed by a container or nested inside one;
- a `pair` anywhere but directly under its own container, and a container of
  pairs written pair by pair, neither of which any file this reader has met
  writes;
- a graph of layered y errors asked for `yerr`, because summing the layers
  would be an answer this reader made up;
- trees written by ROOT 3 or older.

A class the file describes as having no members at all — `TLimit` is one —
reads as the empty `dict` it honestly is, rather than being refused.

## Errors

`xrd.root.ROOTError` is an `XRootDError`, so it is an `OSError` like
everything else this library raises.

| Exception | Means |
| --- | --- |
| `FormatError` | the bytes are not the ROOT format they claim — a truncated download and an HTML error page both look like this |
| `UnsupportedFeatureError` | a valid file using something this reader does not do, named rather than guessed at |
