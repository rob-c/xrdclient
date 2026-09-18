# Easy mode

You have a URL and a question. This page is the whole answer: fifteen verbs,
each of which takes the URL and does the obvious thing. No objects to build,
no connections to open, no flags to add up.

```python
import xrdclient

for path in xrdclient.ls("root://eos.example.org//store/user/me"):
    print(path.name, xrdclient.human_bytes(xrdclient.size(path)))
```

Everything here is also available the long way round - see
[Files and paths](files.md) and [Namespaces](filesystem.md) - and the long way
is what you want once a script grows. Nothing is lost by starting here.

## What is there?

```python
xrdclient.ls("root://host//store/run7")            # the directory, sorted
xrdclient.glob("root://host//store/run7/**/*.root")  # ** crosses directories, * does not
xrdclient.exists("root://host//store/f.root")      # True or False
xrdclient.size("root://host//store/f.root")        # bytes
xrdclient.stat("root://host//store/f.root")        # size, times, permissions
xrdclient.checksum("root://host//store/f.root")    # the digest the server already has
```

`ls` and `glob` hand back paths, not strings, so the answer to one question
is the input to the next. A stat prints as the line `ls -l` would have
printed:

```pycon
>>> print(xrdclient.stat("root://host//store/f.root"))
-rw-rw----    1.4 GiB  2026-03-11 09:42  /store/f.root
```

and `xrdclient.human_bytes(1536)` turns any byte count into `1.5 KiB` on its own.

## Reading and writing

```python
text = xrdclient.read_text("root://host//store/notes.txt")
data = xrdclient.read_bytes("root://host//store/small.root")

xrdclient.write_text("root://host//store/notes.txt", "ran fine\n")
xrdclient.write_bytes("root://host//store/small.root", payload)
```

These read or write the whole file in one go, which is right for a note and
wrong for a hundred gigabytes. For a big file use
[`xrdclient.open`](quickstart.md#read-a-file), which streams and behaves exactly
like the builtin `open`:

```python
with xrdclient.open("root://host//store/big.root", "rb") as fh:
    header = fh.read(1024)
```

Directories above a file are created for you when you write.

## Moving things around

```python
xrdclient.mkdir("root://host//store/user/me/run7")     # parents and all
xrdclient.move("root://host//store/a.root", "root://host//store/b.root")
xrdclient.remove("root://host//store/b.root")
xrdclient.remove("root://host//store/run7", recursive=True)
```

`move` is a rename when both ends are the same server - instant, and no data
crosses the network - and a verified copy followed by a delete when they are
not. `remove` refuses a directory with anything in it until you say
`recursive=True`, because that is the one call here that cannot be undone.

## Tape

Big datasets live on tape, and a file on tape has to be brought to disk
before anything can read it. That takes minutes to hours, so you ask, and
then you wait:

```python
request = xrdclient.stage(["root://tape.example.org//store/f.root",
                     "root://tape.example.org//store/g.root"])

while not xrdclient.is_online("root://tape.example.org//store/f.root"):
    time.sleep(60)
```

`xrdclient.stage` takes one URL or a list of them and returns the request id the
site gave it. If you want the site's own view of the request rather than a
file-by-file check, that is
[`FileSystem.query_prepare`](filesystem.md#asking-the-server-about-itself).

## Words instead of flags

Nothing in this library needs you to add bits together. Everywhere a flag is
accepted, the flag's own name in a string is accepted too, and the common
choices have ordinary keyword arguments:

| Instead of | Write |
| --- | --- |
| `fh.open(OpenFlags.READ)` | `fh.open("r")` |
| `fh.open(OpenFlags.NEW \| OpenFlags.MAKEPATH)` | `fh.open("new makepath")` |
| `fs.mkdir(path, 0o750)` | `fs.mkdir(path, "rwxr-x---")` |
| `fs.scandir(path, flags=DirListFlags.NONE)` | `fs.scandir(path, stat=False)` |
| `fs.locate(path, flags=LocateFlags.REFRESH)` | `fs.locate(path, refresh=True)` |
| `fs.prepare(paths, flags=PrepareFlags.EVICT)` | `fs.prepare(paths, evict=True)` |
| `fs.query(QueryCode.CHECKSUM, path)` | `fs.query("checksum", path)` |

Case, hyphens and separators are yours to choose: `"no-wait"`, `"NO_WAIT"`
and `"no wait"` are one flag, and `"stage notify"`, `"stage,notify"` and
`"stage|notify"` are two. A word that is not a flag says so, and says what
you probably meant:

```pycon
>>> xrdclient.PrepareFlags("stag")
Traceback (most recent call last):
ValueError: PrepareFlags has no 'stag'; did you mean 'stage'?
```

Permissions read three ways - `0o750`, `"750"`, `"rwxr-x---"` - and flags
print as their names, so a stat you print says `is_dir|is_readable` rather
than `18`.

## When to stop using this page

Each verb here opens a connection, asks its question and closes it again.
That is the right trade for one question and the wrong one for a thousand:

```python
# Fine.
size = xrdclient.size("root://host//store/f.root")

# Not fine: one connection per file, a thousand times over.
sizes = [xrdclient.size(url) for url in thousands_of_urls]
```

Hold a path instead, and everything derived from it shares the one
connection:

```python
with xrdclient.Path("root://host//store/run7") as run:
    sizes = {child.name: child.stat().st_size for child in run.iterdir()}
```

That is the same library, one level down, and it reads almost the same.

## Configuration

Every verb takes `config=` for a site that needs a longer timeout, a
particular credential, or a proxy:

```python
slow = xrdclient.Config(request_timeout=600)
xrdclient.stage("root://tape.example.org//store/f.root", config=slow)
```

or set it once for the whole program with
[`xrdclient.configure`](config.md).
