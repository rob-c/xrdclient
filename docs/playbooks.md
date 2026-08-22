# Training playbooks

Five programs live in `examples/`. Four learn from a `root://` URL without
holding the file: MNIST twice — once in the short words of [`xrd.ml`](ml.md)
and once written out longhand — an autoencoder that squeezes CIFAR-10
photographs through sixty-four numbers, and a small convolutional net on
Fashion-MNIST. The fifth inspects one JARVIS crystal entry as raw and
normalized 2D images. They are short on purpose, and each keeps remote access
visible rather than hiding it behind setup code.

The point they make is the one worth checking before trusting a client with a
dataset that does not fit anywhere: every minibatch is a read of one basket
out of the file on the server, so the memory a run costs is a pool of rows and
not a copy of the file. Each program prints what it held, measured rather than
claimed.

## Making the files

Once, and then they sit there:

```python
from xrd.root import create, datasets

for name in ("mnist", "fashion_mnist", "cifar10"):
    with create(f"datasets/{name}.root") as out:
        for split in ("train", "test"):
            datasets.convert(name, out, split=split)
```

Each becomes one file of twenty trees: `train_0` … `train_9` and `test_0` …
`test_9` for MNIST, and the class names themselves — `train_airplane`,
`train_cat` — for the other two. See
[the datasets everyone teaches with](root.md#the-datasets-everyone-teaches-with)
for the 1,422 sets a public mirror can write, and what each of
them is licensed under.

## Serving them

Any endpoint this library reads will do — a real storage element, an HTTP
server, an S3 bucket, a path on the machine. With none of those to hand, share
the directory yourself on a port you can bind, with no login and no daemon:

```console
$ python -m xrd.testing datasets --port 21094 --pattern 'cifar10.root'
serving 1 files on root://127.0.0.1:21094/ with no login
  root://127.0.0.1:21094//home/you/datasets/cifar10.root  169,068,241 bytes
  root://127.0.0.1:21094//cifar10.root  169,068,241 bytes
```

That server authorises everyone and reads its files into memory, which is fine
for a demonstration on loopback and wrong for anything else; see
[Testing](testing.md#sharing-a-directory-over-root) before pointing it at a
network.

## Inspecting a JARVIS crystal projection

`examples/jarvis_2d_visualize.py` reads one selected entry and one `xy`, `xz`
or `yz` projection from a JARVIS-DFT ROOT file. It does not require PyTorch or
download the rest of a hosted dataset:

```console
$ python3 examples/jarvis_2d_visualize.py \
    root://127.0.0.1:21094//jarvis_dft2d_formation_energy.root \
    --tree train --entry 12 --plane xz --normalization minmax \
    --output crystal.png
<Image2D 32x32 from 'train'/'projection' entry=12, plane='xz', normalization='minmax'>
id=JVASP-... formula=... target=...
saved crystal.png
```

Without `--output` it opens the side-by-side Matplotlib figure. A local path,
HTTPS URL or catalogue name works in place of the `root://` URL. See
[Raw and normalized 2D crystal images](ml.md#raw-and-normalized-2d-crystal-images)
for rectangular branches, custom layers and direct NumPy/PyTorch conversion.

## MNIST, in the words a beginner has

`examples/mnist_easy.py` — the same classifier as the next section, written
against [`xrd.ml`](ml.md). There are no baskets, offsets or dtypes in it: a
URL goes in and minibatches of `(images, labels)` come out, already scaled and
already typed for the loss function.

```console
$ python examples/mnist_easy.py root://127.0.0.1:21094//mnist.root
root://127.0.0.1:21094//mnist.root: 70,000 rows, 10 classes
  inputs   image: 784 x uint8, scaled to 0-1
  answer   label: int32
  splits   train 60,000 rows, test 10,000 rows
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
       *%*          @%*
      :%%-          %%+
      -%%          =%*
      -%#         =%#:
      -%+       .+%*
      -%#      =%%+
      -%%+..-*#%#+.
      -%%%%#%%%*=
       #%%%%%%+
        =%%%=.
rows per class: {'0': 5923, '1': 6742, '2': 5958, ...}

epoch 1: 60,000 rows, loss 0.5150
epoch 2: 60,000 rows, loss 0.2358
epoch 3: 60,000 rows, loss 0.1782
epoch 4: 60,000 rows, loss 0.1425
epoch 5: 60,000 rows, loss 0.1188
accuracy 0.9646 on 10,000 test rows, none of which were downloaded
```

The whole of the reading is one line, and the summary above it is printed
rather than known:

```python
for images, labels in data.train.batches(256, device=device):
```

The three programs below do the same work a layer down, with the pooling,
the scaling and the shapes written out; that layer is where to go when the
reading itself needs changing, and [Machine learning](ml.md) says what each
gives you.

## An MLP on MNIST

`examples/mnist_mlp.py` — 784 pixels into 128 hidden units into ten digits,
Adam, five epochs:

```console
$ python examples/mnist_mlp.py root://127.0.0.1:21094//mnist.root
root://127.0.0.1:21094//mnist.root: 12 MB, 237 minibatches an epoch, on cuda
  epoch 1: 60,000 rows in  1.9s (31,998 rows/s), loss 0.5209
  epoch 2: 60,000 rows in  3.2s (18,568 rows/s), loss 0.2387
  epoch 3: 60,000 rows in  2.1s (28,839 rows/s), loss 0.1817
  epoch 4: 60,000 rows in  4.1s (14,600 rows/s), loss 0.1469
  epoch 5: 60,000 rows in  2.3s (26,571 rows/s), loss 0.1230
  accuracy 0.9522 on 10,000 test rows, streamed the same way
  never more than 14.6 MB in hand at once, of a 12 MB file, after 5 passes over its training rows and one over the rest
  peak resident 1,127 MB, which is mostly PyTorch and CUDA
```

The whole of the reading is these four lines:

```python
trees = [handle[name] for name in handle.trees() if name.startswith("train_")]
train = DataLoader(
    mixed(trees, ["image", "label"], step=512, batch=256, device=device), batch_size=None
)
```

`mixed` is there because the file keeps one tree per class: read them in turn
and the model sees six thousand zeroes before it sees a one. It takes 512 rows
from each of the ten trees, shuffles that pool of 5,120 together and hands out
256 at a time, so every minibatch holds every digit while the file is still
read a basket at a time.

## An autoencoder on CIFAR-10

`examples/cifar10_autoencoder.py` — 3,072 numbers a photograph down to 64 and
back out again, scored on the held-out split:

```console
$ python examples/cifar10_autoencoder.py root://127.0.0.1:21094//cifar10.root
root://127.0.0.1:21094//cifar10.root: 169 MB, 10 classes, 196 minibatches an epoch, on cuda
  epoch 1: 50,000 pictures in  5.5s (9,025 pictures/s), mse 0.03186
  epoch 2: 50,000 pictures in  4.8s (10,396 pictures/s), mse 0.01848
  ...
  epoch 8: 50,000 pictures in  9.5s (5,279 pictures/s), mse 0.01143
  27.0 levels out of 255 per pixel on 10,000 held-out photographs
  never more than 48.4 MB in hand at once, of a 169 MB file, after 8 passes over its training rows and one over the rest
  peak resident 1,238 MB, which is mostly PyTorch and CUDA
```

This is the interesting one for memory: the file is 169 MB, every epoch reads
all of it, and the most that was ever in hand is 48 MB — ten trees times a
pool of 1,024 pictures of 3,072 bytes, plus the one basket each branch keeps.
Halve `step` and that halves too. Nothing about the run changes if the file is
a terabyte on a storage element in another country, except how long the
baskets take to arrive.

No labels are read at all — an autoencoder has no use for them — so the column
never leaves the server. Reading is by column, and a column not named is a
basket not fetched.

## A CNN on Fashion-MNIST

`examples/fashion_mnist_cnn.py` — two convolutions and a pool apiece, then ten
outputs:

```console
$ python examples/fashion_mnist_cnn.py root://127.0.0.1:21094//fashion_mnist.root
root://127.0.0.1:21094//fashion_mnist.root: 31 MB, 10 classes t_shirt_top, trouser, pullover ..., on cuda
  epoch 1: 60,000 rows in  2.0s (30,727 rows/s), loss 0.7398
  ...
  epoch 5: 60,000 rows in  3.2s (18,487 rows/s), loss 0.3095
  accuracy 0.8819 on 10,000 test rows, streamed the same way
  never more than 17.5 MB in hand at once, of a 31 MB file, after 5 passes over its training rows and one over the rest
  peak resident 1,317 MB, which is mostly PyTorch and CUDA
```

A row of 784 numbers is a picture again in one call, because that is how the
converter wrote it:

```python
batch["image"].float().view(-1, 1, 28, 28) / 255
```

CIFAR-10 is the same one line with `(-1, 3, 32, 32)`: its 3,072 bytes are 1,024
red, then green, then blue, which is the order PyTorch reads planes in.

## What the numbers mean

The memory line is measured, not asserted. Each program starts `tracemalloc`,
builds its model and loaders, calls `tracemalloc.reset_peak()`, and reports how
much further the Python heap went while the data went through it:

```python
tracemalloc.reset_peak()
settled = tracemalloc.get_traced_memory()[0]
...
held = (tracemalloc.get_traced_memory()[1] - settled) / 1e6
```

That counts what this library allocates — sockets, baskets, decompressed
buffers, the arrays the tensors are made from — and not PyTorch's own arenas,
which are C++ and invisible to `tracemalloc`. The resident figure beside it is
the honest total for the process, and it is over a gigabyte in all three runs
because importing PyTorch and starting CUDA costs that before a single row
arrives.

What is held while reading, then, is:

* one basket per branch being read, kept so that consecutive entries do not
  re-read and re-decompress the same one;
* the pool `mixed` is working through: `step` rows from each open tree;
* whatever the loop itself is still holding a reference to.

Nothing accumulates across epochs — the second epoch peaks where the first
did, which is what makes the same program fine on a file the machine could not
hold. Streaming from a real storage element is the same code with a different
URL:

```console
$ python examples/mnist_mlp.py root://eos.example.org//eos/user/y/you/mnist.root
$ python examples/mnist_mlp.py https://data.example.org/sets/mnist.root
$ python examples/mnist_mlp.py datasets/mnist.root
```

The timings above are a laptop's: an RTX 4060 Laptop GPU, PyTorch 2.7.1, the
server on loopback, and other work going on at the same time — the same MNIST
epoch takes under a second on a quiet machine and four seconds on a busy one,
so read the rows-per-second as a floor rather than a benchmark. The losses and
the memory do not move. On a machine with no GPU the programs still run — they
ask `torch.cuda.is_available()` and take what they find — and read at the same
speed, since what is being measured over loopback is mostly decompression.
