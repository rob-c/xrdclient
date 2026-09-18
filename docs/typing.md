# Typing

The package ships a `py.typed` marker and is checked with `mypy --strict` in
CI, so annotations reach your code without a stub package and without
`ignore_missing_imports`.

```console
$ mypy --strict your_analysis.py     # xrdclient is checked, not skipped
```

## What the annotations promise

`xrdclient.open` is overloaded the way typeshed overloads the builtin: a *literal*
mode string decides whether you get bytes or text, and `buffering=0` decides
whether you get the raw layer.

| Call | Type |
| --- | --- |
| `xrdclient.open(url, "rb")` | `BinaryIO` |
| `xrdclient.open(url, "r")` | `TextIO` |
| `xrdclient.open(url, "rb", buffering=0)` | `XRootDRawIO` - so `.file` is visible |
| `xrdclient.open(url, mode)` where `mode: str` | `IO[Any]` |

That last row is the escape hatch: a mode computed at runtime still
type-checks, it just cannot say which of the four you asked for.
`tests/test_typing.py` runs `mypy` over exactly these calls and compares
`reveal_type` against the table, so the promise cannot rot silently.

`FileSystem.open` deliberately keeps one signature returning `IO[Any]` rather
than repeating the overloads. It is the by-path spelling; if you want the
element type statically, `xrdclient.open` is the one that knows. The whole-file
helpers are precise either way:

```python
fs.read_bytes(path)   # bytes
fs.read_text(path)    # str
fs.stat(path)         # StatInfo
```

## Flags are enums, not integers

Everything that is a bitmask on the wire is an `IntFlag` in Python, so it
still compares and combines as an integer and prints as something you can
read:

```python
>>> fs.statx(["/store/a", "/store"])[1].flags
<StatInfoFlags.IS_DIR|IS_READABLE: 18>
```

## What is `Any`, and why

Three places are honestly `Any`, and are annotated as such rather than being
papered over:

- **`gssapi`** and **`google_crc32c`** ship no stubs, and both are optional
  extras reached through a guarded import.
- **`fsspec`** ships no `py.typed`, so `AbstractFileSystem` is `Any` and
  `xrdclient.fsspec_impl` is the one module allowed to subclass an untyped base.
- **`copy(source, target)`** takes a URL, a `str`, an `os.PathLike`, an
  `XRootDPath` or an already-open binary file, and dispatches on what it got.

## Raw I/O and `IO[bytes]`

`XRootDRawIO` and `HTTPRawIO` are `io.RawIOBase` subclasses, which typeshed
does not consider `IO[bytes]` even though they read and write bytes. The copy
engine casts at that one boundary, with a comment saying so; nothing else in
the package needs to know.
