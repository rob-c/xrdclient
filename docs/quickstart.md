# Quickstart

Everything below is a complete program once you add `import xrdclient`.

If all you have is a URL and one question, start at
[Easy mode](easy.md) instead - `xrdclient.ls`, `xrdclient.size`, `xrdclient.read_text` and a
dozen more, each of them one line.

## Read a file

```python
with xrdclient.open("root://eos.example.org//store/data.root", "rb") as fh:
    header = fh.read(1024)
    fh.seek(-4096, 2)          # relative to the end
    trailer = fh.read()
```

The object is an `io.BufferedReader`, so line iteration, `readinto`, `peek`
and `TextIOWrapper` all work. The default mode is `"rb"` - remote files are
data far more often than they are text - and `"r"` or `"rt"` gets you decoded
lines with the usual `encoding`, `errors` and `newline` arguments:

```python
with xrdclient.open("root://host//store/notes.txt", "r") as fh:
    for line in fh:
        print(line.rstrip())
```

## Write a file

```python
with xrdclient.open("root://host//store/out.root", "wb") as fh:
    fh.write(payload)
```

`"w"` truncates, `"x"` refuses to overwrite, `"a"` appends and creates when
the file is not there. Parent directories are created for you; pass
`posc=True` if a half-written file should be discarded when the connection
dies.

## Walk a namespace

```python
fs = xrdclient.FileSystem("root://eos.example.org")

for entry in fs.scandir("/store/user/me"):
    print(entry.name, entry.stat.st_size, entry.is_dir())

for root, dirs, files in fs.walk("/store/user/me"):
    ...

for path in fs.glob("/store/user/me/**/*.root"):
    ...
```

`scandir` fetches a stat per entry; `scandir(path, stat=False)` asks for names
only, which is the cheaper call on a directory with a hundred thousand files
in it.

## Use it as a path

```python
p = xrdclient.Path("root://host//store") / "user" / "me"
p.mkdir(parents=True, exist_ok=True)
(p / "note.txt").write_text("hello")
sizes = {child.name: child.stat().st_size for child in p.iterdir()}
```

## Copy something

```python
xrdclient.copy("root://a//store/f.root", "/scratch/f.root")            # download
xrdclient.copy("/scratch/f.root", "root://b//store/f.root")            # upload
xrdclient.copy_tree("root://a//store/run7", "/scratch/run7")           # recursive
xrdclient.third_party("root://a//store/f.root", "root://b//store/f.root")
```

Checksums are verified by default. `progress=` takes a `(done, total)`
callable.

## Handle an error

```python
try:
    fs.stat("/store/missing.root")
except FileNotFoundError as exc:
    print(exc.errno, exc.filename)
```

There is no status object to inspect and no return value to forget to check.
[Errors](errors.md) has the full hierarchy.

## Do it asynchronously

```python
import asyncio, xrdclient.aio

async def main():
    async with xrdclient.aio.FileSystem("root://eos.example.org") as fs:
        names = await fs.listdir("/store")
        sizes = await asyncio.gather(*(fs.getsize(f"/store/{n}") for n in names))

asyncio.run(main())
```

## Configure it

```python
cfg = xrdclient.Config(request_timeout=60.0, verify_checksums=False)
fs = xrdclient.FileSystem("root://host", cfg)

with xrdclient.override(chunk_size=1 << 20):   # for this block only
    ...
```

Every setting reads its default from the same environment variable the C
client uses, and settings you always want can live in
`~/.config/xrd/config.ini`:

```python
cfg = xrdclient.Config.from_file(alias="eos")     # [defaults], then [alias eos]
```

See [Configuration](config.md).

## From the shell

```console
$ xrd-fs ls -l root://eos.example.org//store/user/me
$ xrd-fs stat --json davs://dav.example.org/store/f.root
$ xrd-cp -r /tmp/results davs://dav.example.org/store/results
$ xrd-cp -r --sync size /tmp/results root://host//store/results/
$ xrd-fs du --alias eos root://eos.example.org//store/user/me
```

## Without a storage element

```python
from xrdclient.testing import FakeServer

with FakeServer(files={"/data/a.root": b"hello"}) as server:
    fs = xrdclient.FileSystem(server.url)
    assert fs.read_bytes("/data/a.root") == b"hello"
```

See [Testing and fault injection](testing.md) for the failure modes you can
ask for on purpose.
