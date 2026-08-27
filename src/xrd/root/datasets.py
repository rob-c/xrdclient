"""The datasets machine learning is taught with, written as ROOT files.

    >>> from xrd.root import datasets                        # doctest: +SKIP
    >>> datasets.convert("cifar10", "cifar10.root")          # doctest: +SKIP
    {'train_airplane': 5000, 'train_automobile': 5000, ...}

Physics keeps its data in ROOT and reads it over XRootD; everyone else keeps
theirs in whatever the framework of the year unpacks. That is a shame in one
direction only - the format is perfectly good at images and tables - so this
module converts the sets people actually teach and benchmark with into ROOT
files, laid out the way a training loop wants them: one tree per class, a
fixed-size array per entry, the label beside it, and the row's place in the
original file so any number can be traced back to where it came from.

Nothing is redistributed here. Each dataset is fetched from the people who
publish it, on the machine doing the converting, and :attr:`DATASETS` records
what each one is and what its licence says. The converted file carries that
same statement in an ``about`` key, so a file that outlives this program still
says where it came from.

Almost every archive is read with the standard library: IDX, tar, zip, a zip
inside a zip, gzip, WAV, ARFF, CSV and the XML a spreadsheet keeps inside its
own zip. The few large scientific formats use narrowly scoped optional readers
for HDF5 and JPEG. What comes out is pictures, sound, sentences, tables and
plain blocks of numbers, because a training loop should not have to care which
of those it is reading. Some sets have a class to sort a row into and some have
a number to predict from it; both are here. The CIFAR sets are taken in their
binary distribution rather than the Python one on purpose: that one is a
pickle, and unpickling a download is a way to run somebody else's code.
"""

from __future__ import annotations

import csv
import gzip
import io
import os
import struct
import tarfile
import tempfile
import threading
import time
import wave
import xml.etree.ElementTree as ET
import zipfile
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import date, datetime
from math import nan, prod
from pathlib import Path
from typing import Any, ClassVar
from urllib.parse import urlsplit

from .._compat import SLOTS, zip_strict
from .._log import get_logger
from ..config import Config
from ..errors import TransientError
from ..url import parse
from ._hub_tables import HUB_OPEN
from ._open_large import OPEN_LARGE
from ._uci_attribution import UCI_ATTRIBUTION
from ._uci_large import UCI_LARGE
from ._uci_tables import UCI_TABLES
from .writer import WritableFile, create

_log = get_logger(__name__)

__all__ = [
    "CIFAR",
    "DATASETS",
    "IDX_FILES",
    "MNIST_MIRROR",
    "IDX_TYPES",
    "IMAGE_BASKET",
    "MISSING",
    "REQUIRED",
    "TABLE_BASKET",
    "Audio",
    "Dataset",
    "Images",
    "Large",
    "Matrix",
    "Table",
    "convert",
    "describe",
    "fetch",
    "licence_url",
    "read_arff",
    "read_idx",
    "read_table",
    "read_xlsx",
    "redistributable",
]

#: How many bytes of a column gather before a basket goes out. Images get a
#: large one on purpose: a training loop reads thousands of rows at a time,
#: and at this size that is one read rather than twenty.
IMAGE_BASKET = 512 * 1024
TABLE_BASKET = 64 * 1024

#: What the type byte of an IDX file says its values are. These datasets hold
#: unsigned bytes; the others are named so a wrong file is refused by what it
#: actually is rather than read as pixels.
IDX_TYPES = {
    0x08: "unsigned bytes",
    0x09: "signed bytes",
    0x0B: "16-bit integers",
    0x0C: "32-bit integers",
    0x0D: "floats",
    0x0E: "doubles",
}

#: Where MNIST is served from. The original site stopped offering it; this
#: is the mirror PyTorch's own downloader uses.
MNIST_MIRROR = "https://ossci-datasets.s3.amazonaws.com/mnist/"

#: The four files an IDX-format image set comes in. MNIST chose these names
#: and the two datasets built to drop into its place kept them.
IDX_FILES = {
    "train": ("train-images-idx3-ubyte.gz", "train-labels-idx1-ubyte.gz"),
    "test": ("t10k-images-idx3-ubyte.gz", "t10k-labels-idx1-ubyte.gz"),
}

#: How the tabular sets spell a value nobody measured. A missing number is
#: written as a NaN, which is what every reader downstream already means by
#: it; a missing category is written as -1, because there is no such code.
MISSING = frozenset({"", "*", "?", "NA", "na", "N/A", "nan", "NaN", "NaNN", "null"})

#: The namespace everything in a spreadsheet's XML is in.
XLSX_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"

#: The day a ``"date"`` column counts from, as an ordinal: 1970-01-01.
EPOCH = date(1970, 1, 1).toordinal()

#: The moment a ``"time"`` column counts from: midnight starting that day.
MIDNIGHT = datetime(1970, 1, 1)

#: One row on its way to a tree: which class it belongs to, and its columns.
Rows = Iterator[tuple[int, dict[str, Any]]]

#: What a reader hands back: the class names, the columns, and the rows.
Loaded = tuple[tuple[str, ...], dict[str, Any], Rows]


def read_idx(raw: bytes) -> tuple[tuple[int, ...], bytes]:
    """The shape and the values of one IDX file, unzipped if it arrived zipped.

        >>> read_idx(open("train-labels-idx1-ubyte.gz", "rb").read())  # doctest: +SKIP
        ((60000,), b'\\x05\\x00\\x04...')

    IDX is four magic bytes - two zeros, a type, and how many dimensions -
    then one big-endian length per dimension, then the values.
    """
    raw = gzip.decompress(raw) if raw[:2] == b"\x1f\x8b" else raw
    if len(raw) < 4 or raw[:2] != b"\x00\x00":
        raise ValueError(
            "this is not an IDX file: one starts with two zero bytes, a type and a rank, "
            "and this starts with something else"
        )
    kind, rank = raw[2], raw[3]
    if kind != 0x08:
        what = IDX_TYPES.get(kind, f"values of type {kind:#04x}")
        raise ValueError(f"this IDX file holds {what}, and these image sets hold unsigned bytes")
    if len(raw) < 4 + 4 * rank:
        raise ValueError(
            f"this IDX file says it has {rank} dimensions and stops before saying them"
        )
    shape = struct.unpack_from(f">{rank}i", raw, 4)
    wanted = prod(shape)
    values = raw[4 + 4 * rank :]
    if len(values) != wanted:
        raise ValueError(
            f"this IDX file says {'x'.join(str(size) for size in shape)}, which is {wanted} "
            f"values, and holds {len(values)}"
        )
    return shape, values


def read_table(
    raw: bytes,
    *,
    delimiter: str | None = ",",
    header: bool = False,
    comment: str = "",
    quoted: bool = True,
    tail: int = 0,
) -> Iterator[list[str]]:
    """Every row of a delimited text file, as stripped strings, blanks dropped.

    ``delimiter`` is what separates the fields, or ``None`` for any run of
    whitespace, which is how the older sets are written. ``comment`` drops
    every line that starts with it, and ``header`` drops the first line that
    survives that - the names are as likely to come after a preamble as before
    one. ``quoted`` is whether a double quote groups a field, as it does in a CSV;
    a file of English sentences means its quotes literally, and setting this
    ``False`` keeps them rather than reading them as punctuation.

    ``tail`` is how many fields a whitespace-separated line has when the last
    of them is free text with spaces in it - a car's name at the end of a row
    of numbers. The line is split that many times and no further, and the last
    field is unwrapped if it arrived in quotes, which is what a CSV reader
    would have done with it.
    """
    rows = _table_rows(raw, delimiter, quoted, tail)
    naming = header
    for row in rows:
        if _ignored_row(row, comment):
            continue
        if naming:
            naming = False
            continue
        yield _clean_row(row, tail=tail, quoted=quoted)


def _table_rows(raw: bytes, delimiter: str | None, quoted: bool, tail: int) -> Iterator[list[str]]:
    """Physical rows parsed using this table's delimiter convention."""
    text = io.StringIO(raw.decode("utf-8-sig"), newline="")
    if delimiter is None:
        return ((line.split(None, tail - 1) if tail else line.split()) for line in text)
    return csv.reader(
        text,
        delimiter=delimiter,
        quoting=csv.QUOTE_MINIMAL if quoted else csv.QUOTE_NONE,
    )


def _ignored_row(row: Sequence[str], comment: str) -> bool:
    """Whether a physical row is blank or begins with the comment marker."""
    return not any(cell.strip() for cell in row) or bool(comment and row[0].startswith(comment))


def _clean_row(row: Sequence[str], *, tail: int, quoted: bool) -> list[str]:
    """Strip a row and unwrap its optional free-text tail."""
    cells = [cell.strip() for cell in row]
    if tail and quoted:
        cells[-1] = _unquoted(cells[-1])
    return cells


def _continued(rows: Iterator[list[str]], width: int, label: str) -> Iterator[list[str]]:
    """Join a publisher's physical lines until they make one declared row."""
    pending: list[str] = []
    for physical, row in enumerate(rows):
        if len(row) < width and row[-1:] == [""]:
            row = row[:-1]
        pending.extend(row)
        if len(pending) == width:
            yield pending
            pending = []
        elif len(pending) > width:
            raise ValueError(
                f"physical row {physical} of {label} takes a continued row past its {width} fields"
            )
    if pending:
        raise ValueError(
            f"the last continued row of {label} ends after {len(pending)} of its {width} fields"
        )


def _unquoted(cell: str) -> str:
    """A field with the quotes around it taken off, if that is what they are."""
    return cell[1:-1] if len(cell) > 1 and cell[0] == cell[-1] == '"' else cell


def read_xlsx(raw: bytes, *, sheet: int = 1, header: bool = False) -> Iterator[list[str]]:
    """Every row of one sheet of a spreadsheet, as the strings it shows.

        >>> next(read_xlsx(open("ENB2012_data.xlsx", "rb").read()))  # doctest: +SKIP
        ['X1', 'X2', 'X3', 'X4', 'X5', 'X6', 'X7', 'X8', 'Y1', 'Y2']

    A modern spreadsheet is a zip of XML, so this needs nothing the standard
    library does not already have. Text is usually kept once in a shared table
    and referred to by number, which is why a sheet on its own reads as
    nonsense and this does not.

    A row of nothing at all is not data - spreadsheets are full of them, below
    and beside what was typed - and neither are the empty cells trailing a
    row, so both are dropped. A gap in the middle of a row is kept, as an empty
    string, because the fields after it still have to line up.
    """
    book = zipfile.ZipFile(io.BytesIO(raw))
    inside = f"xl/worksheets/sheet{sheet}.xml"
    if inside not in book.namelist():
        sheets = sum(name.startswith("xl/worksheets/sheet") for name in book.namelist())
        raise ValueError(f"this spreadsheet has {sheets} sheets in it, and no sheet {sheet}")
    shared: list[str] = []
    if "xl/sharedStrings.xml" in book.namelist():
        shared = [
            "".join(part.text or "" for part in entry.iter(XLSX_NS + "t"))
            for entry in ET.fromstring(book.read("xl/sharedStrings.xml"))
        ]
    rows = _sheet(book.open(inside), shared)
    if header:
        next(rows, None)
    yield from rows


def _sheet(stream: Any, shared: Sequence[str]) -> Iterator[list[str]]:
    """The rows of one sheet's XML, each cell put back where its reference says."""
    with stream:
        for _, element in ET.iterparse(stream):
            if element.tag != XLSX_NS + "row":
                continue
            cells: list[str] = []
            for cell in element:
                while len(cells) < _at(cell.get("r", "")):
                    cells.append("")
                value = cell.find(XLSX_NS + "v")
                if value is None:
                    cells.append("".join(part.text or "" for part in cell.iter(XLSX_NS + "t")))
                elif cell.get("t") == "s":
                    cells.append(shared[int(value.text or "0")])
                else:
                    cells.append(value.text or "")
            while cells and not cells[-1]:
                cells.pop()
            element.clear()
            if cells:
                yield cells


def _at(ref: str) -> int:
    """Which column a cell reference like ``A1`` or ``BC7`` names, from zero."""
    place = 0
    for letter in ref.rstrip("0123456789"):
        place = place * 26 + ord(letter.upper()) - 64
    return place - 1


def read_arff(raw: bytes) -> Iterator[list[str]]:
    """The rows of an ARFF file's ``@data`` section, its header and comments gone.

    ARFF is what Weka wrote and half of UCI still publishes: a header of
    ``@attribute`` lines saying what the columns are, then ``@data`` and a
    comma-separated table. The header is documentation - what the columns
    actually hold is declared in :attr:`Table.fields` - so only the rows come
    back.
    """
    lines = raw.decode("utf-8-sig").splitlines()
    for at, line in enumerate(lines):
        if line.strip().lower().startswith("@data"):
            return read_table("\n".join(lines[at + 1 :]).encode(), comment="%")
    raise ValueError(
        "this is not an ARFF file: one has a @data line and its rows after it, "
        "and this has no @data line at all"
    )


def fetch(source: Any, *, config: Any = None) -> bytes:
    """Everything at ``source``: bytes as they are, or a path or URL read whole.

    Read through this library, so ``root://``, ``https://`` and the rest all
    work, and so does an already-open file.
    """
    if isinstance(source, (bytes, bytearray, memoryview)):
        return bytes(source)
    if hasattr(source, "read"):
        return bytes(source.read())
    url = parse(source)
    if url.is_local:
        with open(url.path, "rb") as handle:
            return handle.read()
    from ..io import open_url

    settings = config or Config()
    for attempt in range(settings.connect_retries + 1):
        try:
            with open_url(url, "rb", config=settings) as handle:
                return bytes(handle.read())
        except TransientError:
            if attempt >= settings.connect_retries:
                raise
            number = attempt + 1
            _log.info(
                "source fetch from %s failed transiently; retrying %d/%d",
                url,
                number,
                settings.connect_retries,
            )
            delay = min(settings.retry_backoff * 2**attempt, settings.wait_cap)
            if delay:
                time.sleep(delay)
    raise AssertionError("unreachable")  # pragma: no cover


_SOURCE_LOCKS: dict[Path, threading.Lock] = {}
_SOURCE_LOCKS_GUARD = threading.Lock()


def _source_lock(target: Path) -> threading.Lock:
    """The in-process lock which serializes writers of one shared cache entry."""
    with _SOURCE_LOCKS_GUARD:
        return _SOURCE_LOCKS.setdefault(target, threading.Lock())


def _fetch_file(
    source: Any,
    *,
    cache: Path | None,
    name: str,
    expected: int,
    config: Any,
) -> tuple[Path, bool]:
    """Put one large source on disk; return its path and whether it is temporary.

    The ordinary teaching archives are small enough to read whole.  The large
    UCI shelf is not: its biggest ZIP is tens of gigabytes, and ``zipfile``
    needs a seekable object for its central directory.  A local path is used
    where it lies, a cache entry is retained, and an uncached remote source is
    spooled to a temporary file and removed after conversion.
    """
    if isinstance(source, (str, os.PathLike)):
        url = parse(source)
        if url.is_local:
            return Path(url.path), False
    else:
        url = None

    target = cache / f"{name}.source" if cache is not None else None
    if target is not None:
        with _source_lock(target):
            return _spool_file(source, url, target, name, expected, config)
    return _spool_file(source, url, None, name, expected, config)


def _spool_file(
    source: Any,
    url: Any,
    target: Path | None,
    name: str,
    expected: int,
    config: Any,
) -> tuple[Path, bool]:
    """Copy one source to a retained target or a disposable temporary file."""
    cached = _cached_source(target, name, expected)
    if cached is not None:
        return cached, False
    part, handle, temporary = _source_handle(target, name, resume=url is not None)
    try:
        offset = handle.tell()
        if expected and offset > expected:
            raise ValueError(
                f"the partial source for {name} is {offset} bytes, and the published "
                f"archive is {expected}"
            )
        if not expected or offset < expected:
            _copy_source(source, url, handle, config)
        handle.close()
        _check_source_size(part, source, name, expected)
        return _retain_source(part, target, temporary)
    except TransientError:
        handle.close()
        if temporary:
            part.unlink(missing_ok=True)
        raise
    except BaseException:
        handle.close()
        part.unlink(missing_ok=True)
        raise


def _cached_source(target: Path | None, name: str, expected: int) -> Path | None:
    """A complete retained source, after checking its published byte count."""
    if target is None or not target.exists():
        return None
    if expected and target.stat().st_size != expected:
        raise ValueError(
            f"the cached source for {name} is {target.stat().st_size} bytes, "
            f"and the published archive is {expected}; remove {target} to fetch it again"
        )
    return target


def _source_handle(
    target: Path | None, name: str, *, resume: bool
) -> tuple[Path, Any, bool]:
    """Open the temporary or cache-adjacent file which receives a source."""
    if target is not None:
        target.parent.mkdir(parents=True, exist_ok=True)
        part = target.with_name(target.name + ".part")
        handle: Any = part.open("ab" if resume else "wb")
        if handle.tell():
            _log.info("resuming %s source download at byte %d", name, handle.tell())
        return part, handle, False
    held = tempfile.NamedTemporaryFile(prefix=f"xrd-{name}-", suffix=".source", delete=False)
    return Path(held.name), held, True


def _copy_source(source: Any, url: Any, handle: Any, config: Any) -> None:
    """Stream an in-memory, open, or remote source to an open binary file."""
    if isinstance(source, (bytes, bytearray, memoryview)):
        handle.write(source)
        return
    if hasattr(source, "read"):
        while chunk := source.read(1 << 20):
            handle.write(chunk)
        return
    from ..io import open_url

    assert url is not None
    with open_url(url, "rb", buffering=0, config=config) as remote:
        if handle.tell():
            remote.seek(handle.tell())
        _copy_remote(remote, handle, config)


def _copy_remote(remote: Any, handle: Any, config: Any) -> None:
    """Copy a remote stream, resuming recoverable drops at the last committed byte."""
    retries = (config or Config()).connect_retries
    failures = 0
    while True:
        try:
            chunk = remote.read(1 << 20)
        except TransientError:
            failures += 1
            if failures > retries:
                raise
            _log.info(
                "source connection dropped at byte %d; retrying %d/%d",
                handle.tell(),
                failures,
                retries,
            )
            remote.seek(handle.tell())
            continue
        if not chunk:
            return
        handle.write(chunk)
        failures = 0


def _check_source_size(part: Path, source: Any, name: str, expected: int) -> None:
    """Reject a remote source whose retained byte count changed upstream."""
    if not expected or isinstance(source, (bytes, bytearray, memoryview)):
        return
    size = part.stat().st_size
    if size != expected:
        raise ValueError(
            f"the source fetched for {name} is {size} bytes, and the published "
            f"archive is {expected}"
        )


def _retain_source(part: Path, target: Path | None, temporary: bool) -> tuple[Path, bool]:
    """Move a finished cache part into place, or return its temporary path."""
    if target is None:
        return part, temporary
    os.replace(part, target)
    return target, False


def _mirror_source(base: str, source: str) -> str:
    """Put a source under a mirror prefix, retaining its published filename."""
    path = source.partition("?")[0].rstrip("/")
    filename = path.rsplit("/", 1)[-1]
    if filename == "content":
        filename = path.rsplit("/", 2)[-2]
    return base + filename


def _member(raw: bytes, member: str) -> bytes:
    """One file out of whatever arrived: a zip, a gzip, or the bytes themselves."""
    if raw[:2] == b"PK":
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            held = archive.namelist()
            if member not in held:
                raise ValueError(f"this zip holds {', '.join(held)}, and not {member!r}")
            raw = archive.read(member)
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    return raw


def _number(cell: str, cast: Any, name: str, index: int, label: str) -> Any:
    """One field as a number, saying where it was when it is not one."""
    try:
        return cast(cell)
    except ValueError:
        raise ValueError(
            f"row {index} of {label} has {cell!r} in {name}, which is not a number"
        ) from None


def _numbered(kinds: Mapping[str, int] | Sequence[str]) -> Mapping[str, int]:
    """A field's categories as name to code, however they were written down."""
    if isinstance(kinds, Mapping):
        return kinds
    return {name: at for at, name in enumerate(kinds)}


def _plain(names: Sequence[str], role: str = "i") -> tuple[tuple[str, str], ...]:
    """A run of fields that all hold the same sort of value, named in order."""
    return tuple((name, role) for name in names)


def _tidy(text: str) -> tuple[str, ...]:
    """The class names in a file of them, one per line, made safe to name a tree."""
    return tuple(
        "".join(letter if letter.isalnum() else "_" for letter in line.strip().lower())
        for line in text.splitlines()
        if line.strip()
    )


#: The default of a field that has to be given anyway - see
#: :attr:`Dataset.NEEDS`. It is not a value any of them could hold, so a
#: description that still has one has left that field out.
REQUIRED: Any = None

#: Canonical terms for every licence statement used by the registry.  The
#: statement stays dataset-specific (including dual-version R package terms),
#: while this gives a visitor the authoritative licence or policy text rather
#: than making them search for it from an abbreviation.
LICENCE_URLS = {
    "AFL-3.0": "https://spdx.org/licenses/AFL-3.0.html",
    "CC BY 4.0": "https://creativecommons.org/licenses/by/4.0/",
    "CC BY 3.0": "https://creativecommons.org/licenses/by/3.0/",
    ("CC BY 4.0 or CC0, per source recording"): "https://zenodo.org/records/19340660#licensing",
    "CC BY-SA 3.0": "https://creativecommons.org/licenses/by-sa/3.0/",
    "CC BY-SA 4.0": "https://creativecommons.org/licenses/by-sa/4.0/",
    "CC BY-NC 4.0": "https://creativecommons.org/licenses/by-nc/4.0/",
    "CC BY-ND 4.0": "https://creativecommons.org/licenses/by-nd/4.0/",
    "CC0": "https://creativecommons.org/publicdomain/zero/1.0/",
    "MIT": "https://opensource.org/license/mit/",
    "Apache-2.0": "https://www.apache.org/licenses/LICENSE-2.0",
    "BSD-2-Clause": "https://opensource.org/license/bsd-2-clause/",
    "BSD-3-Clause": "https://opensource.org/license/bsd-3-clause/",
    "BSL-1.0": "https://www.boost.org/LICENSE_1_0.txt",
    "CC BY 2.0": "https://creativecommons.org/licenses/by/2.0/",
    "CC BY 2.5": "https://creativecommons.org/licenses/by/2.5/",
    "CDLA-Permissive-1.0": "https://spdx.org/licenses/CDLA-Permissive-1.0.html",
    "CDLA-Permissive-2.0": "https://cdla.dev/permissive-2-0/",
    "ECL-2.0": "https://opensource.org/license/ecl-2-0/",
    "Etalab Open Licence 2.0": "https://www.etalab.gouv.fr/licence-ouverte-open-licence/",
    "ISC": "https://opensource.org/license/isc-license-txt/",
    "MPL-2.0": "https://www.mozilla.org/MPL/2.0/",
    "EPL-2.0": "https://www.eclipse.org/legal/epl-2.0/",
    "AGPL-3.0 or later": "https://www.gnu.org/licenses/agpl-3.0.html",
    "ODC-By-1.0": "https://opendatacommons.org/licenses/by/1-0/",
    "ODbL-1.0": "https://opendatacommons.org/licenses/odbl/1-0/",
    "PDDL-1.0": "https://opendatacommons.org/licenses/pddl/1-0/",
    "Unlicense": "https://unlicense.org/",
    "NCSA": "https://spdx.org/licenses/NCSA.html",
    "PostgreSQL": "https://www.postgresql.org/about/licence/",
    "WTFPL": "https://spdx.org/licenses/WTFPL.html",
    "Zlib": "https://www.zlib.net/zlib_license.html",
    "GPL": "https://www.gnu.org/licenses/gpl.html",
    "GPL-2": "https://www.gnu.org/licenses/old-licenses/gpl-2.0.html",
    "GPL-2 or later": "https://www.gnu.org/licenses/old-licenses/gpl-2.0.html",
    "GPL-2 or GPL-3": "https://www.r-project.org/Licenses/",
    "GPL-3": "https://www.gnu.org/licenses/gpl-3.0.html",
    "GPL-3 or later": "https://www.gnu.org/licenses/gpl-3.0.html",
    "LGPL-2 or later": "https://www.gnu.org/licenses/old-licenses/lgpl-2.0.html",
    "Artistic-2.0": "https://opensource.org/license/artistic-2-0/",
    (
        "NIST Special Database 19, a work of the US federal government; "
        "the EMNIST paper asks to be cited"
    ): (
        "https://www.nist.gov/open/copyright-fair-use-and-licensing-statements-"
        "srd-data-software-and-technical-series-publications"
    ),
}

#: A public mirror never admits a single dataset whose complete published
#: source payload reaches two decimal gigabytes.  It is deliberately a total
#: per dataset (not a per-shard loophole), and deliberately strict.
SOURCE_SIZE_CEILING = 2_000_000_000
LARGE_SOURCE_FLOOR = 100_000_000

_SOURCE_REPOSITORIES = {
    "archive.ics.uci.edu": "UCI Machine Learning Repository",
    "huggingface.co": "Hugging Face Hub",
    "zenodo.org": "Zenodo",
    "vincentarelbundock.github.io": "Rdatasets mirror",
    "ourworldindata.org": "Our World in Data",
    "github.com": "GitHub",
    "physionet.org": "PhysioNet",
    "www.physionet.org": "PhysioNet",
    "www.cs.toronto.edu": "University of Toronto",
    "www.nist.gov": "NIST",
    "biometrics.nist.gov": "NIST",
    "yann.lecun.com": "Yann LeCun's MNIST archive",
}


def licence_url(licence: str) -> str:
    """The canonical terms for a registry licence, or no link when none exist."""
    return LICENCE_URLS.get(licence, "")


def _about_line(label: str, value: str) -> str:
    return f"\n{label}: {value}" if value else ""


@dataclass(frozen=True, **SLOTS)
class Dataset:
    """Where a dataset comes from, what it holds, and what may be done with it.

    The subclasses below add how to read one: :class:`Images` for the IDX sets
    shaped like MNIST, :class:`CIFAR` for the two tiny-image archives,
    :class:`Audio` for the recordings, :class:`Matrix` for the plain blocks of
    numbers, and :class:`Table` for the delimited ones.
    """

    #: How this module is asked for it: ``convert("cifar10", ...)``.
    name: str
    #: What its authors call it, for messages.
    label: str
    #: One line saying what is in it.
    title: str
    #: What its licence is. Read it before you redistribute what comes out.
    licence: str
    #: Where it is published, for anyone who wants the paper or the terms.
    source: str
    #: The classes, in label order. One tree is written per class, named here.
    classes: tuple[str, ...]
    #: The splits it comes in. Single-split sets use ``("all",)``.
    splits: tuple[str, ...] = ("all",)
    #: How big a basket to fill before writing one out.
    basket_size: int = TABLE_BASKET
    #: Broad discovery labels used by the generated catalogue and search page.
    modality: str = "tabular"
    task: str = "machine learning"
    #: Named people or organisations credited by the canonical dataset record.
    creators: tuple[str, ...] = ()
    #: The dataset publisher, when it is distinct from a hosting repository.
    publisher: str = ""
    #: Canonical project, DOI, or first-party landing page. Defaults to ``source``.
    origin: str = ""
    #: The repository serving the registered source bytes, never presented as an author.
    repository: str = ""
    #: Explicit secondary copies, as ``(name, landing URL)`` pairs.
    mirrors: tuple[tuple[str, str], ...] = ()
    #: Preferred paper or version citation URL, when the authors publish one.
    citation: str = ""

    #: Which of a subclass's own fields it cannot do without. They carry
    #: :data:`REQUIRED` rather than no default at all, because a field without
    #: one may not follow a field with one and every subclass field follows
    #: :attr:`splits`; so what the language would have refused at the call is
    #: refused here instead, by name, the moment one is built.
    NEEDS: ClassVar[tuple[str, ...]] = ()
    FILE_BACKED: ClassVar[bool] = False

    def __post_init__(self) -> None:
        """Refuse a description that has left out something it is read by."""
        missing = [name for name in self.NEEDS if getattr(self, name) is REQUIRED]
        if missing:
            raise TypeError(f"{type(self).__name__} needs {', '.join(missing)}")

    def about(self, split: str) -> str:
        """What goes in the file's ``about`` key, so it can say what it is."""
        where = _about_line("split", split) if len(self.splits) > 1 else ""
        terms = licence_url(self.licence)
        canonical = _about_line("licence URL", terms)
        creators = _about_line("creators", "; ".join(self.creators))
        publisher = _about_line("publisher", self.publisher)
        citation = _about_line("citation", self.citation)
        mirrors = "".join(f"\nmirror ({name}): {url}" for name, url in self.mirrors)
        return (
            f"{self.label}: {self.title}{where}{creators}{publisher}\n"
            f"origin: {self.origin_url()}\norigin status: {self.origin_kind()}\n"
            f"source repository: {self.source_repository()}\n"
            f"source: {self.source}{mirrors}{citation}\nlicence: {self.licence}{canonical}\n"
            f"transformation: {self.transformation_summary()}\n"
            f"layout: {self.layout()}"
        )

    def origin_url(self) -> str:
        """The canonical origin when known, otherwise the registered dataset record."""
        return self.origin or self.source

    def origin_kind(self) -> str:
        """Whether :meth:`origin_url` is canonical or the best published record available."""
        return "canonical" if self.origin else "source record"

    def source_repository(self) -> str:
        """Who serves the registered bytes, without mistaking the host for an author."""
        if self.repository:
            return self.repository
        host = (urlsplit(self.source).hostname or "").lower()
        return _SOURCE_REPOSITORIES.get(host, host or "publisher website")

    def provenance(self) -> dict[str, Any]:
        """Structured lineage shared by list output, ROOT metadata, and the website."""
        return {
            "creators": list(self.creators),
            "publisher": self.publisher,
            "origin": self.origin_url(),
            "origin_kind": self.origin_kind(),
            "repository": self.source_repository(),
            "source": self.source,
            "mirrors": [{"name": name, "url": url} for name, url in self.mirrors],
            "citation": self.citation,
        }

    def file_backed(self) -> bool:
        """Whether conversion retains a seekable source instead of loading it whole."""
        return self.FILE_BACKED

    def expected_source_bytes(self, role: str) -> int:
        """The published size used to check a retained source, when one is fixed."""
        return 0

    def urls(self, split: str) -> dict[str, str]:
        """The named source files for a split, supplied by each concrete adapter."""
        raise NotImplementedError

    def source_payload_bytes(self) -> int:
        """The complete unique source payload, counting shared split files once."""
        seen: set[str] = set()
        total = 0
        for split in self.splits:
            for role, url in self.urls(split).items():
                if url not in seen:
                    total += self.expected_source_bytes(role)
                    seen.add(url)
        return total

    def within_source_ceiling(self) -> bool:
        """Whether this source is admitted by the public two-gigabyte policy."""
        size = self.source_payload_bytes()
        return not size or size < SOURCE_SIZE_CEILING

    def large_source(self, *, allow_oversize: bool = False) -> bool:
        """Whether this is a retained source archive selected by ``--large``.

        The normal catalogue keeps the source below two gigabytes.  An explicit
        production opt-in admits larger registered converters while preserving
        the 100 MB lower bound that distinguishes this selection from small sets.
        """
        size = self.source_payload_bytes()
        return (
            self.file_backed()
            and LARGE_SOURCE_FLOOR <= size
            and (allow_oversize or size < SOURCE_SIZE_CEILING)
        )

    def transformation_summary(self) -> str:
        """A concise provenance note describing what the ROOT conversion changes."""
        return f"preserved the published records and wrote {self.layout()}"

    def entry_title(self, split: str, cls: str) -> str:
        """The title of the tree holding one class."""
        where = f"{split} " if len(self.splits) > 1 else ""
        return f"{self.label} {where}rows labelled {cls}"

    def sorting(self) -> str:
        """How many classes it has, the way :func:`describe` puts it."""
        return f"{len(self.classes)} classes"

    def layout(self) -> str:
        """How the trees are laid out, the way :meth:`about` puts it."""
        return "one tree per class"


@dataclass(frozen=True, **SLOTS)
class Images(Dataset):
    """An image set in IDX format: MNIST, and the two sets built to replace it."""

    #: Where the four files are served from, when they are served separately.
    base: str = ""
    #: The one archive holding them, when they are not.
    archive: str = ""
    #: Its published size when it is large enough to retain between splits.
    archive_bytes: int = 0
    #: Which two of them each split is, images first.
    files: Mapping[str, tuple[str, str]] = field(default_factory=lambda: IDX_FILES)
    #: How wide and tall one picture is.
    side: int = 28
    #: What the pixels mean, for the tree title.
    shade: str = "greyscale"

    def urls(self, split: str) -> dict[str, str]:
        """The images and the labels of one split, or the archive holding both."""
        if self.archive:
            return {"archive": self.archive}
        images, labels = self.files[split]
        return {"images": self.base + images, "labels": self.base + labels}

    def file_backed(self) -> bool:
        """Retain a declared large all-splits archive, such as EMNIST."""
        return bool(self.archive and self.archive_bytes)

    def expected_source_bytes(self, role: str) -> int:
        return self.archive_bytes if role == "archive" else 0

    def transformation_summary(self) -> str:
        return (
            f"decompressed the published IDX images and labels; preserved {self.side}x"
            f"{self.side} unsigned-byte pixels and official splits without normalization or "
            "resampling; wrote one ROOT TTree per class"
        )

    def entry_title(self, split: str, cls: str) -> str:
        where = f"{split} " if len(self.splits) > 1 else ""
        return f"{self.label} {where}images of class {cls}, {self.side}x{self.side} {self.shade}"

    def rows(self, raw: Mapping[str, Any], split: str) -> Loaded:
        """The pixels and the labels, checked against each other."""
        if self.archive:
            pictures, names = self.files[split]
            held = raw["archive"]
            if isinstance(held, Path):
                with zipfile.ZipFile(held) as archive:
                    raw = {"images": archive.read(pictures), "labels": archive.read(names)}
            else:
                raw = {"images": _member(held, pictures), "labels": _member(held, names)}
        shape, pixels = read_idx(raw["images"])
        counts, labels = read_idx(raw["labels"])
        if len(shape) != 3 or shape[1:] != (self.side, self.side):
            raise ValueError(
                f"these images are {'x'.join(str(size) for size in shape)}, and {self.label} "
                f"images come as a count of {self.side}x{self.side} pictures"
            )
        if len(counts) != 1 or counts[0] != shape[0]:
            raise ValueError(
                f"there are {shape[0]} images and {counts[0] if len(counts) == 1 else counts} "
                f"labels, and every image needs exactly one"
            )
        width = self.side * self.side
        columns: dict[str, Any] = {"image": ("B", width), "label": "i", "index": "i"}
        return self.classes, columns, self._entries(pixels, labels, width)

    def _entries(self, pixels: bytes, labels: bytes, width: int) -> Rows:
        view = memoryview(pixels)
        for index, label in enumerate(labels):
            at = index * width
            yield label, {"image": view[at : at + width], "label": label, "index": index}


@dataclass(frozen=True, **SLOTS)
class CIFAR(Dataset):
    """One of the two tiny-image archives, in its binary rather than pickled form.

    A record is its labels then 3072 bytes: 1024 red, 1024 green, 1024 blue,
    each of them a 32 by 32 picture read along its rows. That is the layout
    PyTorch wants, so ``.view(-1, 3, 32, 32)`` is all the reshaping there is.
    """

    #: The one archive holding every split.
    archive: str = REQUIRED
    #: Its published size, checked after the retained download.
    source_bytes: int = 0
    #: Which members of it each split is.
    files: Mapping[str, tuple[str, ...]] = REQUIRED
    #: The member naming the classes, one per line, in label order.
    meta: str = REQUIRED
    #: The member naming the coarse classes, when there are two labels a row.
    coarse: str = ""
    #: How wide and tall one picture is.
    side: int = 32

    NEEDS: ClassVar[tuple[str, ...]] = ("archive", "files", "meta")
    FILE_BACKED: ClassVar[bool] = True

    def urls(self, split: str) -> dict[str, str]:
        """The single archive, whichever split was asked for."""
        return {"archive": self.archive}

    def expected_source_bytes(self, role: str) -> int:
        return self.source_bytes if role == "archive" else 0

    def transformation_summary(self) -> str:
        return (
            f"extracted the publisher's binary records; preserved {self.side}x{self.side} "
            "RGB-plane unsigned-byte pixels, labels and official train/test split without "
            "normalization or augmentation; wrote one ROOT TTree per class"
        )

    def sorting(self) -> str:
        """How many classes it has; CIFAR-100 keeps its hundred in the archive."""
        return f"{len(self.classes) or 100} classes"

    def entry_title(self, split: str, cls: str) -> str:
        return (
            f"{self.label} {split} images of class {cls}, {self.side}x{self.side} colour, "
            f"three planes"
        )

    def rows(self, raw: Mapping[str, Any], split: str) -> Loaded:
        """The class names out of the archive's own list, then the records."""
        source = raw["archive"]
        tar = (
            tarfile.open(source, mode="r:gz")
            if isinstance(source, Path)
            else tarfile.open(fileobj=io.BytesIO(source), mode="r:gz")
        )
        try:
            held = {member.name.rsplit("/", 1)[-1]: member for member in tar.getmembers()}
            classes = _tidy(_extract(tar, held, self.meta).decode())
            if self.classes and classes != self.classes:
                raise ValueError(
                    f"this archive says its classes are {', '.join(classes)}, and {self.label} "
                    f"has {', '.join(self.classes)}"
                )
            width = 3 * self.side * self.side
            columns: dict[str, Any] = {"image": ("B", width), "label": "i"}
            if self.coarse:
                columns["coarse"] = "i"
            columns["index"] = "i"
        except Exception:
            tar.close()
            raise
        return classes, columns, self._entries(tar, held, split, width)

    def _entries(
        self, tar: tarfile.TarFile, held: Mapping[str, Any], split: str, width: int
    ) -> Rows:
        stride = width + (2 if self.coarse else 1)
        index = 0
        try:
            for member in self.files[split]:
                data = _extract(tar, held, member)
                if len(data) % stride:
                    raise ValueError(
                        f"{member} is {len(data)} bytes, and {self.label} records are "
                        f"{stride} bytes each"
                    )
                view = memoryview(data)
                for at in range(0, len(data), stride):
                    row: dict[str, Any] = {}
                    if self.coarse:
                        row["coarse"], label = data[at], data[at + 1]
                    else:
                        label = data[at]
                    start = at + stride - width
                    row["image"] = view[start : start + width]
                    row["label"] = label
                    row["index"] = index
                    index += 1
                    yield label, row
        finally:
            tar.close()


def _extract(tar: tarfile.TarFile, held: Mapping[str, Any], member: str) -> bytes:
    """One member of a tar as bytes, refusing what is not there or not a file."""
    found = held.get(member)
    handle = tar.extractfile(found) if found is not None else None
    if handle is None:
        raise ValueError(f"this archive holds {', '.join(sorted(held))}, and not {member!r}")
    with handle:
        return handle.read()


@dataclass(frozen=True, **SLOTS)
class Audio(Dataset):
    """Recordings in an archive of WAV files, one clip an entry.

    A tree column holds a fixed number of values, so every clip is written
    into one :attr:`samples` long and the ``length`` column says how much of
    it is real - the rest is silence this module put there. Nothing is
    resampled, mixed down or trimmed: a clip that is not what :attr:`rate`
    says, or is longer than the column, is refused by name.
    """

    #: The archive holding the recordings.
    archive: str = REQUIRED
    #: The folder inside it they are in.
    folder: str = REQUIRED
    #: What the first part of a file name means, as an index into
    #: :attr:`~Dataset.classes`.
    labels: Mapping[str, int] = REQUIRED
    #: Who is speaking, in code order, when the file names say. A speaker
    #: nobody wrote down is refused rather than numbered on the spot.
    speakers: tuple[str, ...] = ()
    #: How many samples a second every recording must be.
    rate: int = 8000
    #: How long the column is. A clip is padded to it, never cut down to it.
    samples: int = 20000
    #: Exact compressed size when the publisher provides a versioned archive.
    archive_bytes: int = 0
    #: Repetitions below this number form the test split; zero means no split rule.
    test_repetitions: int = 0

    NEEDS: ClassVar[tuple[str, ...]] = ("archive", "folder", "labels")

    def transformation_summary(self) -> str:
        return (
            f"decoded the published WAV PCM without resampling; normalized signed 16-bit "
            f"samples to float32 in [-1, 1), zero-padded each clip to {self.samples} values "
            "while retaining its true length, label and speaker; preserved the publisher's "
            "split rule and wrote one ROOT TTree per class"
        )

    def urls(self, split: str) -> dict[str, str]:
        """The single archive, whichever split was asked for."""
        return {"archive": self.archive}

    def expected_source_bytes(self, role: str) -> int:
        return self.archive_bytes if role == "archive" else 0

    def entry_title(self, split: str, cls: str) -> str:
        return (
            f"{self.label} recordings of class {cls}, {self.rate} Hz mono, "
            f"{self.samples} samples an entry"
        )

    def rows(self, raw: Mapping[str, bytes], split: str) -> Loaded:
        """The recordings under the folder, in name order, and their columns."""
        archive = zipfile.ZipFile(io.BytesIO(raw["archive"]))
        try:
            held = sorted(
                name
                for name in archive.namelist()
                if name.endswith(".wav") and self.folder in name.split("/")[:-1]
            )
            if not held:
                raise ValueError(
                    f"this archive has no .wav files in a {self.folder!r} folder, and that "
                    f"is where {self.label} keeps its recordings"
                )
            columns: dict[str, Any] = {"audio": ("f", self.samples), "length": "i", "label": "i"}
            if self.speakers:
                columns["speaker"] = "i"
            columns["index"] = "i"
        except Exception:
            archive.close()
            raise
        return self.classes, columns, self._entries(archive, held, split)

    def _entries(self, archive: zipfile.ZipFile, held: Sequence[str], split: str) -> Rows:
        speakers = {name: at for at, name in enumerate(self.speakers)}
        try:
            for index, path in enumerate(held):
                stem = path.rsplit("/", 1)[-1][: -len(".wav")]
                parts = stem.split("_")
                if not self._in_split(parts, split):
                    continue
                if parts[0] not in self.labels:
                    raise ValueError(
                        f"{stem} of {self.label} is labelled {parts[0]!r}, and its classes "
                        f"are {', '.join(sorted(self.labels))}"
                    )
                which = self.labels[parts[0]]
                values, count = self._clip(archive.read(path), stem)
                row: dict[str, Any] = {
                    "audio": values,
                    "length": count,
                    "label": which,
                    "index": index,
                }
                if self.speakers:
                    who = parts[1] if len(parts) > 1 else ""
                    if who not in speakers:
                        raise ValueError(
                            f"{stem} of {self.label} is spoken by {who!r}, and the speakers "
                            f"it knows are {', '.join(self.speakers)}"
                        )
                    row["speaker"] = speakers[who]
                yield which, row
        finally:
            archive.close()

    def _in_split(self, parts: Sequence[str], split: str) -> bool:
        """Apply an archive's published repetition split without renumbering rows."""
        if not self.test_repetitions:
            return True
        repetition = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else -1
        expected = "test" if 0 <= repetition < self.test_repetitions else "train"
        return split == expected

    def _clip(self, raw: bytes, stem: str) -> tuple[tuple[float, ...], int]:
        """One recording's samples, padded out to the width of the column."""
        with wave.open(io.BytesIO(raw)) as clip:
            spoken = (clip.getnchannels(), clip.getsampwidth(), clip.getframerate())
            if spoken != (1, 2, self.rate):
                channels, width, rate = spoken
                raise ValueError(
                    f"{stem} of {self.label} is {channels}-channel {8 * width}-bit at "
                    f"{rate} Hz, and these recordings are mono 16-bit at {self.rate} Hz"
                )
            frames = clip.readframes(clip.getnframes())
        count = len(frames) // 2
        if count > self.samples:
            raise ValueError(
                f"{stem} of {self.label} is {count} samples long, and the column holds "
                f"{self.samples}"
            )
        pcm = struct.unpack(f"<{count}h", frames)
        values = tuple(sample / 32768.0 for sample in pcm)
        return values + (0.0,) * (self.samples - count), count


@dataclass(frozen=True, **SLOTS)
class Table(Dataset):
    """A delimited table: one row an example, one field a feature or its label."""

    #: Where it is served from.
    url: str = REQUIRED
    #: Which member of the archive it is, when it arrives in one.
    member: str = ""
    #: Which member of the outer archive holds the inner one, when it arrives
    #: in a zip inside a zip.
    inner: str = ""
    #: Which member each split is, when the archive holds more than one.
    files: Mapping[str, str] = field(default_factory=dict)
    #: What separates the fields, or ``None`` for any run of whitespace.
    delimiter: str | None = ","
    #: Whether the first line names them rather than holding one.
    header: bool = False
    #: Whether a publisher accidentally put a newline in the middle of a row;
    #: short physical rows are joined until the declared field count is met.
    continuations: bool = False
    #: What a line that is not data starts with, when the file has any.
    comment: str = ""
    #: Whether it is an ARFF file, whose rows come after its ``@data`` line.
    arff: bool = False
    #: Whether it is a spreadsheet rather than a text file, read with
    #: :func:`read_xlsx`.
    xlsx: bool = False
    #: How many fields a whitespace-separated row has when the last of them is
    #: free text with spaces in it. Zero splits on every run of whitespace.
    tail: int = 0
    #: How a ``"date"`` or ``"time"`` field is written, in
    #: :func:`~datetime.datetime.strptime` terms. A ``"date"`` column holds the
    #: days since 1970 and a ``"time"`` column the seconds, so a set that
    #: measures something every quarter of an hour keeps its clock.
    dates: str = "%Y-%m-%d"
    #: Whether a double quote groups a field. A table of sentences means its
    #: quotes literally and sets this ``False``.
    quoted: bool = True
    #: How many bytes a ``"text"`` field is given. A tree column is a fixed
    #: size, so the text is written into one this wide with a ``_length``
    #: column beside it; anything longer is refused rather than cut short.
    text_size: int = 0
    #: The fields in the order the file writes them: a name, and either a
    #: typecode, ``"label"``, ``"text"``, ``"date"``, ``"time"``, ``"target"``,
    #: or the name of an entry in :attr:`codes`. A set with no ``"label"`` and no
    #: :attr:`classes` is a regression set: it has a ``"target"`` to predict
    #: instead of a class to sort into, and its rows go to one tree.
    fields: tuple[tuple[str, str], ...] = REQUIRED
    #: What each label in the file means, as an index into :attr:`classes`.
    labels: Mapping[str, int] = field(default_factory=dict)
    #: How the categorical fields are numbered, either as a mapping or as the
    #: categories in code order - a string of them where each is one letter.
    #: A category nobody wrote down is refused rather than guessed at.
    codes: Mapping[str, Mapping[str, int] | Sequence[str]] = field(default_factory=dict)

    NEEDS: ClassVar[tuple[str, ...]] = ("url", "fields")

    def transformation_summary(self) -> str:
        return (
            "parsed the published table without feature scaling; preserved numeric values, "
            "encoded labels and categories as documented integer codes, represented missing "
            "numbers as NaN and retained the publisher's splits; wrote one ROOT TTree per "
            "class (or one rows tree for regression)"
        )

    def urls(self, split: str) -> dict[str, str]:
        """The one file it comes in."""
        return {"table": self.url}

    def entry_title(self, split: str, cls: str) -> str:
        """The title of the tree, which holds every row when there are no classes."""
        where = f"{split} " if len(self.splits) > 1 else ""
        return f"{self.label} {where}rows" + (f" labelled {cls}" if self.classes else "")

    def sorting(self) -> str:
        """How many classes it has, or that it has a number to predict instead."""
        return f"{len(self.classes)} classes" if self.classes else "no classes, a number to predict"

    def layout(self) -> str:
        """One tree a class, unless there are none and every row shares one."""
        return "one tree per class" if self.classes else "one tree of every row"

    def rows(self, raw: Mapping[str, bytes], split: str) -> Loaded:
        """The columns the fields become, then the rows themselves."""
        columns: dict[str, Any] = {}
        for name, role in self.fields:
            if role == "label":
                continue
            if role == "text":
                columns[name] = ("B", self.text_size)
                columns[f"{name}_length"] = "i"
            elif role in ("d", "target"):
                columns[name] = "d"
            elif role == "time":
                columns[name] = "q"
            else:
                columns[name] = "i"
        if self.classes:
            columns["label"] = "i"
        columns["index"] = "i"
        return self.classes or ("rows",), columns, self._entries(raw["table"], split)

    def _text(self, cell: str, name: str, index: int) -> bytes:
        """One field as the bytes its column holds, padded out to the width of it."""
        written = cell.encode()
        if len(written) > self.text_size:
            raise ValueError(
                f"row {index} of {self.label} has {len(written)} bytes in {name}, and the "
                f"column holds {self.text_size}"
            )
        return written + bytes(self.text_size - len(written))

    def _when(self, cell: str, name: str, index: int) -> datetime:
        """One field read as the moment it is written as."""
        try:
            return datetime.strptime(cell, self.dates)
        except ValueError:
            raise ValueError(
                f"row {index} of {self.label} has {cell!r} in {name}, and the dates in it are "
                f"written {self.dates}"
            ) from None

    def _day(self, cell: str, name: str, index: int) -> int:
        """One date as the days since 1970 its column holds."""
        return self._when(cell, name, index).date().toordinal() - EPOCH

    def _moment(self, cell: str, name: str, index: int) -> int:
        """One timestamp as the whole seconds since 1970 its column holds."""
        return int((self._when(cell, name, index) - MIDNIGHT).total_seconds())

    def _cell(
        self, cell: str, name: str, role: str, index: int, coded: Mapping[str, Mapping[str, int]]
    ) -> Any:
        """One field as the number its column holds, or what a gap means there."""
        if role in ("d", "target"):
            return nan if cell in MISSING else _number(cell, float, name, index, self.label)
        if role == "date":
            return -1 if cell in MISSING else self._day(cell, name, index)
        if role == "time":
            return -1 if cell in MISSING else self._moment(cell, name, index)
        if role == "i":
            return -1 if cell in MISSING else _number(cell, int, name, index, self.label)
        if cell in MISSING:
            return -1
        if cell not in coded[role]:
            raise ValueError(
                f"row {index} of {self.label} has {cell!r} in {name}, and the ones it knows "
                f"are {', '.join(sorted(coded[role]))}"
            )
        return coded[role][cell]

    def _read(self, held: bytes) -> Iterator[list[str]]:
        """The rows of the file, however this one happens to be written."""
        if self.arff:
            return read_arff(held)
        if self.xlsx:
            return read_xlsx(held, header=self.header)
        return read_table(
            held,
            delimiter=self.delimiter,
            header=self.header,
            comment=self.comment,
            quoted=self.quoted,
            tail=self.tail,
        )

    def _entries(self, raw: bytes, split: str) -> Rows:
        if self.inner:
            raw = _member(raw, self.inner)
        table = self._read(_member(raw, self.files.get(split, self.member)))
        if self.continuations:
            table = _continued(table, len(self.fields), self.label)
        coded = {role: _numbered(kinds) for role, kinds in self.codes.items()}
        for index, cells in enumerate(table):
            yield self._entry(cells, index, coded)

    def _entry(
        self, cells: Sequence[str], index: int, coded: Mapping[str, Mapping[str, int]]
    ) -> tuple[int, dict[str, Any]]:
        """Validate and convert one physical table row."""
        if len(cells) != len(self.fields):
            raise ValueError(
                f"row {index} of {self.label} has {len(cells)} fields, and its columns "
                f"are {len(self.fields)}"
            )
        which = -1 if self.classes else 0
        row: dict[str, Any] = {}
        for cell, (name, role) in zip_strict(cells, self.fields):
            if role == "text":
                row[name] = self._text(cell, name, index)
                row[f"{name}_length"] = len(cell.encode())
            elif role != "label":
                row[name] = self._cell(cell, name, role, index, coded)
            else:
                which = self._label(cell, index)
        if self.classes:
            row["label"] = which
        row["index"] = index
        return which, row

    def _label(self, cell: str, index: int) -> int:
        """Translate a declared class label, with a useful malformed-row error."""
        if cell in self.labels:
            return self.labels[cell]
        raise ValueError(
            f"row {index} of {self.label} is labelled {cell!r}, and its classes "
            f"are {', '.join(sorted(self.labels))}"
        )


@dataclass(frozen=True, **SLOTS)
class Matrix(Dataset):
    """A file that is one long row of numbers an example, and nothing else.

    The feature-vector sets are written this way: every row the same width, no
    header naming anything, and the label kept wherever the publisher found
    convenient - in a file of its own beside the numbers, as a run of columns
    at the end of the row, or nowhere at all except a count at the top saying
    how many rows each class has. All three are read here, because the
    alternative is asking everyone to reshape the file first.

    The numbers become one fixed-size column rather than one column a feature,
    which is what a training loop wants of 561 of them, and is what
    :func:`~.ml.iter_tensors` hands straight to a tensor.
    """

    #: Where it is served from.
    url: str = REQUIRED
    #: The archive inside the archive, when it arrives wrapped twice.
    inner: str = ""
    #: Which member of it each split's numbers are.
    files: Mapping[str, str] = REQUIRED
    #: Which member each split's labels are, when they are kept apart.
    label_files: Mapping[str, str] = field(default_factory=dict)
    #: What each label in such a file means, as an index into
    #: :attr:`~Dataset.classes`.
    labels: Mapping[str, int] = field(default_factory=dict)
    #: Any other column kept in a file of its own, as name to member a split.
    beside: Mapping[str, Mapping[str, str]] = field(default_factory=dict)
    #: How many numbers a row holds, checked against every one of them.
    width: int = REQUIRED
    #: What the column of them is called.
    column: str = "features"
    #: What one of them is stored as.
    kind: str = "d"
    #: How many columns at the end of a row are a one-hot label, if it is
    #: written there rather than in a file of its own.
    onehot: int = 0
    #: Whether the first line counts the rows of each class in turn, which is
    #: how a file with no labels in it at all still says what its rows are.
    counts: bool = False

    NEEDS: ClassVar[tuple[str, ...]] = ("url", "files", "width")

    def transformation_summary(self) -> str:
        return (
            f"parsed each published fixed-width numeric row into an unscaled {self.width}-value "
            f"ROOT array; preserved labels, companion columns and publisher splits; wrote one "
            "ROOT TTree per class"
        )

    def urls(self, split: str) -> dict[str, str]:
        """The one archive everything comes in."""
        return {"archive": self.url}

    def entry_title(self, split: str, cls: str) -> str:
        where = f"{split} " if len(self.splits) > 1 else ""
        return f"{self.label} {where}rows labelled {cls}, {self.width} numbers each"

    def rows(self, raw: Mapping[str, bytes], split: str) -> Loaded:
        """The fixed-size column of numbers, the label, and whatever is beside it."""
        columns: dict[str, Any] = {self.column: (self.kind, self.width), "label": "i"}
        for name in self.beside:
            columns[name] = "i"
        columns["index"] = "i"
        return self.classes, columns, self._entries(raw["archive"], split)

    def _held(self, raw: bytes, member: str) -> bytes:
        """One member of the archive, out of the archive inside it when there is one."""
        return _member(_member(raw, self.inner) if self.inner else raw, member)

    def _column(self, raw: bytes, member: str) -> list[str]:
        """A file holding one value a line, which is how a label or a subject arrives."""
        return [cells[0] for cells in read_table(self._held(raw, member), delimiter=None)]

    def _runs(self, counted: Sequence[str]) -> list[int]:
        """Where each class ends, from the line that says how many rows each has."""
        if len(counted) != len(self.classes):
            raise ValueError(
                f"{self.label} starts by counting {len(counted)} classes, and it has "
                f"{len(self.classes)}"
            )
        ends, total = [], 0
        for count, cls in zip_strict(counted, self.classes):
            total += _number(count, int, cls, 0, self.label)
            ends.append(total)
        return ends

    def _entries(self, raw: bytes, split: str) -> Rows:
        table = read_table(self._held(raw, self.files[split]), delimiter=None)
        ends = self._runs(next(table, [])) if self.counts else []
        told = self._column(raw, self.label_files[split]) if self.label_files else []
        beside = {name: self._column(raw, wheres[split]) for name, wheres in self.beside.items()}
        index = -1
        for index, cells in enumerate(table):
            yield self._entry_row(cells, told, ends, beside, index)
        self._check_side_lengths(split, index + 1, told, beside)

    def _entry_row(
        self,
        cells: Sequence[str],
        told: Sequence[str],
        ends: Sequence[int],
        beside: Mapping[str, Sequence[str]],
        index: int,
    ) -> tuple[int, dict[str, Any]]:
        """Validate and convert one matrix row and its side columns."""
        if len(cells) != self.width + self.onehot:
            raise ValueError(
                f"row {index} of {self.label} holds {len(cells)} numbers, and its rows "
                f"are {self.width + self.onehot} wide"
            )
        row: dict[str, Any] = {
            self.column: self._values(cells[: self.width], index),
            "index": index,
        }
        for name, values in beside.items():
            row[name] = _number(self._beside(values, name, index), int, name, index, self.label)
        which = self._which(cells, told, ends, index)
        row["label"] = which
        return which, row

    def _check_side_lengths(
        self,
        split: str,
        rows: int,
        told: Sequence[str],
        beside: Mapping[str, Sequence[str]],
    ) -> None:
        """Require each file kept beside the matrix to have the same row count."""
        for name, values in (("labels", told), *beside.items()):
            if values and len(values) != rows:
                raise ValueError(
                    f"{self.label} has {rows} rows in {split} and {len(values)} in its "
                    f"file of {name}"
                )

    def _values(self, cells: Sequence[str], index: int) -> tuple[Any, ...]:
        """One row's numbers, as whatever the column stores them as."""
        numbers = tuple(_number(cell, float, self.column, index, self.label) for cell in cells)
        return numbers if self.kind == "d" else tuple(int(number) for number in numbers)

    def _beside(self, values: Sequence[str], name: str, index: int) -> str:
        """One row's value out of a file kept beside the numbers."""
        if index >= len(values):
            raise ValueError(
                f"{self.label} has more rows than the {len(values)} in its file of {name}"
            )
        return values[index]

    def _which(
        self, cells: Sequence[str], told: Sequence[str], ends: Sequence[int], index: int
    ) -> int:
        """Which class a row belongs to, however this dataset says so."""
        if self.onehot:
            hot = [at for at, cell in enumerate(cells[self.width :]) if float(cell)]
            if len(hot) != 1:
                raise ValueError(
                    f"row {index} of {self.label} has {len(hot)} of its {self.onehot} label "
                    f"columns set, and exactly one of them is a label"
                )
            return hot[0]
        if ends:
            for at, end in enumerate(ends):
                if index < end:
                    return at
            raise ValueError(
                f"{self.label} counts {ends[-1]} rows at the top of its file and holds more "
                f"than that"
            )
        cell = self._beside(told, "labels", index)
        if cell not in self.labels:
            raise ValueError(
                f"row {index} of {self.label} is labelled {cell!r}, and its classes "
                f"are {', '.join(sorted(self.labels))}"
            )
        return self.labels[cell]


@dataclass(frozen=True, **SLOTS)
class Large(Dataset):
    """A disk-backed archive whose rows need a purpose-built reader.

    These are the archives for which the generic table and image adapters are
    deliberately the wrong abstraction: ZIP64 collections of thousands of
    time series, nested archives of DICOM slices, HDF5 signal matrices, or
    compressed CSVs with millions of events.  Their source stays on disk and
    :mod:`xrd.root._uci_large` yields one bounded piece at a time.
    """

    #: The complete source archive, or an empty string for a sharded source.
    url: str = ""
    #: Its snapshot size, checked after a remote fetch before conversion.
    source_bytes: int = REQUIRED
    #: The reader registered in :mod:`xrd.root._uci_large`.
    converter: str = REQUIRED
    #: Optional Python packages needed by this particular source format.
    requires: tuple[str, ...] = ()
    #: Exactly what its purpose-built converter changes or preserves.
    transformation_note: str = REQUIRED
    #: Optional structural tree layout when trees represent stages or domains, not classes.
    layout_note: str = ""
    #: Named shards which together constitute one dataset.
    sources: Mapping[str, str] = field(default_factory=dict)
    #: Published size of each shard, used to validate retained downloads.
    source_sizes: Mapping[str, int] = field(default_factory=dict)
    #: Enforce those sizes as immutable publisher promises.  Dataset-viewer
    #: exports are moving derived artefacts, so their recorded sizes remain
    #: useful capacity estimates but their Parquet structure is the authority.
    enforce_source_sizes: bool = True
    #: Optional cache names shared by logical tasks backed by an identical source.
    cache_names: Mapping[str, str] = field(default_factory=dict)
    #: Write each publisher split as its own ROOT file in catalogue builds.
    #: This keeps complete datasets whose combined ROOT layout exceeds 2 GB
    #: within the writer's interoperable 32-bit file layout.
    split_files: bool = False

    NEEDS: ClassVar[tuple[str, ...]] = (
        "source_bytes",
        "converter",
        "transformation_note",
    )
    FILE_BACKED: ClassVar[bool] = True

    def __post_init__(self) -> None:
        Dataset.__post_init__(self)
        if bool(self.url) == bool(self.sources):
            raise TypeError("Large needs exactly one of url or sources")
        if self.sources and set(self.sources) != set(self.source_sizes):
            raise TypeError("Large needs a published size for every source shard")
        if self.sources and sum(self.source_sizes.values()) != self.source_bytes:
            raise ValueError("Large source shard sizes do not add up to source_bytes")
        roles = set(self.sources) if self.sources else {"archive"}
        _validate_cache_names(self.cache_names, roles)

    def urls(self, split: str) -> dict[str, str]:
        """The archive or complete set of shards behind every split."""
        return dict(self.sources) if self.sources else {"archive": self.url}

    def expected_source_bytes(self, role: str) -> int:
        if not self.enforce_source_sizes:
            return 0
        if self.sources:
            return self.source_sizes.get(role, 0)
        return self.source_bytes if role == "archive" else 0

    def source_payload_bytes(self) -> int:
        """The declared total is already deduplicated across splits and shards."""
        return self.source_bytes

    def transformation_summary(self) -> str:
        return self.transformation_note

    def sorting(self) -> str:
        """How its answer is represented."""
        return f"{len(self.classes)} classes" if self.classes else "a number or series to predict"

    def layout(self) -> str:
        """How its classes are separated."""
        if self.layout_note:
            return self.layout_note
        return "one tree per class" if self.classes else "one tree of every row"

    def rows(self, raw: Mapping[str, Any], split: str) -> Loaded:
        """Stream the archive with its registered, bounded-memory reader."""
        if any(not isinstance(source, Path) for source in raw.values()):
            raise TypeError(f"the sources of {self.label} must be disk-backed archives")
        if self.converter.startswith("open:"):
            from ._open_large import load as load_open

            return load_open(self.converter.removeprefix("open:"), raw, split)
        from ._uci_large import load as load_uci

        return load_uci(self.converter, raw["archive"], split)


def _validate_cache_names(cache_names: Mapping[str, str], roles: set[str]) -> None:
    """Require explicit shared cache names to stay within one source declaration."""
    if not set(cache_names).issubset(roles):
        raise TypeError("Large cache names must refer to registered source roles")
    if any(not value or Path(value).name != value for value in cache_names.values()):
        raise ValueError("Large cache names must be non-empty plain filenames")


_COVER_FIELDS: tuple[tuple[str, str], ...] = (
    *(
        (name, "i")
        for name in (
            "elevation",
            "aspect",
            "slope",
            "horizontal_distance_to_hydrology",
            "vertical_distance_to_hydrology",
            "horizontal_distance_to_roadways",
            "hillshade_9am",
            "hillshade_noon",
            "hillshade_3pm",
            "horizontal_distance_to_fire_points",
            *(f"wilderness_area_{area}" for area in range(1, 5)),
            *(f"soil_type_{soil}" for soil in range(1, 41)),
        )
    ),
    ("cover_type", "label"),
)


#: The 47 balanced EMNIST classes: the ten digits, the capitals, and the
#: eleven lower-case letters that do not look like their own capital.
_EMNIST_CLASSES: tuple[str, ...] = (
    *(str(digit) for digit in range(10)),
    *(f"upper_{chr(letter)}" for letter in range(ord("a"), ord("z") + 1)),
    *(f"lower_{letter}" for letter in "abdefghnqrt"),
)

_EMNIST_FILES: dict[str, tuple[str, str]] = {
    split: (
        f"gzip/emnist-balanced-{split}-images-idx3-ubyte.gz",
        f"gzip/emnist-balanced-{split}-labels-idx1-ubyte.gz",
    )
    for split in ("train", "test")
}

#: Each mushroom field and the letters it is written with, in the order the
#: dataset's own description gives them, so the codes are the published ones.
_MUSHROOM: tuple[tuple[str, str], ...] = (
    ("cap_shape", "bcxfks"),
    ("cap_surface", "fgys"),
    ("cap_colour", "nbcgrpuewy"),
    ("bruises", "tf"),
    ("odour", "alcyfmnps"),
    ("gill_attachment", "adfn"),
    ("gill_spacing", "cwd"),
    ("gill_size", "bn"),
    ("gill_colour", "knbhgropuewy"),
    ("stalk_shape", "et"),
    ("stalk_root", "bcuezr"),
    ("stalk_surface_above_ring", "fyks"),
    ("stalk_surface_below_ring", "fyks"),
    ("stalk_colour_above_ring", "nbcgopewy"),
    ("stalk_colour_below_ring", "nbcgopewy"),
    ("veil_type", "pu"),
    ("veil_colour", "nowy"),
    ("ring_number", "not"),
    ("ring_type", "ceflnpsz"),
    ("spore_print_colour", "knbhrouwy"),
    ("population", "acnsvy"),
    ("habitat", "glmpuwd"),
)

_ADULT_CODES: dict[str, tuple[str, ...]] = {
    "workclass": (
        "Federal-gov",
        "Local-gov",
        "Never-worked",
        "Private",
        "Self-emp-inc",
        "Self-emp-not-inc",
        "State-gov",
        "Without-pay",
    ),
    "education": (
        "10th",
        "11th",
        "12th",
        "1st-4th",
        "5th-6th",
        "7th-8th",
        "9th",
        "Assoc-acdm",
        "Assoc-voc",
        "Bachelors",
        "Doctorate",
        "HS-grad",
        "Masters",
        "Preschool",
        "Prof-school",
        "Some-college",
    ),
    "marital_status": (
        "Divorced",
        "Married-AF-spouse",
        "Married-civ-spouse",
        "Married-spouse-absent",
        "Never-married",
        "Separated",
        "Widowed",
    ),
    "occupation": (
        "Adm-clerical",
        "Armed-Forces",
        "Craft-repair",
        "Exec-managerial",
        "Farming-fishing",
        "Handlers-cleaners",
        "Machine-op-inspct",
        "Other-service",
        "Priv-house-serv",
        "Prof-specialty",
        "Protective-serv",
        "Sales",
        "Tech-support",
        "Transport-moving",
    ),
    "relationship": (
        "Husband",
        "Not-in-family",
        "Other-relative",
        "Own-child",
        "Unmarried",
        "Wife",
    ),
    "race": ("Amer-Indian-Eskimo", "Asian-Pac-Islander", "Black", "Other", "White"),
    "sex": ("Female", "Male"),
    "native_country": (
        "Cambodia",
        "Canada",
        "China",
        "Columbia",
        "Cuba",
        "Dominican-Republic",
        "Ecuador",
        "El-Salvador",
        "England",
        "France",
        "Germany",
        "Greece",
        "Guatemala",
        "Haiti",
        "Holand-Netherlands",
        "Honduras",
        "Hong",
        "Hungary",
        "India",
        "Iran",
        "Ireland",
        "Italy",
        "Jamaica",
        "Japan",
        "Laos",
        "Mexico",
        "Nicaragua",
        "Outlying-US(Guam-USVI-etc)",
        "Peru",
        "Philippines",
        "Poland",
        "Portugal",
        "Puerto-Rico",
        "Scotland",
        "South",
        "Taiwan",
        "Thailand",
        "Trinadad&Tobago",
        "United-States",
        "Vietnam",
        "Yugoslavia",
    ),
}

_ADULT_FIELDS: tuple[tuple[str, str], ...] = (
    ("age", "i"),
    ("workclass", "workclass"),
    ("fnlwgt", "i"),
    ("education", "education"),
    ("education_years", "i"),
    ("marital_status", "marital_status"),
    ("occupation", "occupation"),
    ("relationship", "relationship"),
    ("race", "race"),
    ("sex", "sex"),
    ("capital_gain", "i"),
    ("capital_loss", "i"),
    ("hours_per_week", "i"),
    ("native_country", "native_country"),
    ("income", "label"),
)

_LETTER_FIELDS: tuple[tuple[str, str], ...] = (
    ("letter", "label"),
    *(
        (name, "i")
        for name in (
            "x_box",
            "y_box",
            "width",
            "height",
            "on_pixels",
            "x_bar",
            "y_bar",
            "x2_bar",
            "y2_bar",
            "xy_bar",
            "x2y_bar",
            "xy2_bar",
            "x_edge",
            "x_edge_by_y",
            "y_edge",
            "y_edge_by_x",
        )
    ),
)

_DIGIT_FIELDS: tuple[tuple[str, str], ...] = (
    *((f"pixel_{at:02d}", "i") for at in range(64)),
    ("digit", "label"),
)

_WDBC_FIELDS: tuple[tuple[str, str], ...] = (
    ("id", "i"),
    ("diagnosis", "label"),
    *(
        (f"{name}_{kind}", "d")
        for kind in ("mean", "error", "worst")
        for name in (
            "radius",
            "texture",
            "perimeter",
            "area",
            "smoothness",
            "compactness",
            "concavity",
            "concave_points",
            "symmetry",
            "fractal_dimension",
        )
    ),
)

_WINE_FIELDS: tuple[tuple[str, str], ...] = (
    ("cultivar", "label"),
    *(
        (name, "d")
        for name in (
            "alcohol",
            "malic_acid",
            "ash",
            "alcalinity_of_ash",
            "magnesium",
            "total_phenols",
            "flavanoids",
            "nonflavanoid_phenols",
            "proanthocyanins",
            "colour_intensity",
            "hue",
            "od280_od315",
            "proline",
        )
    ),
)

_SEED_FIELDS: tuple[tuple[str, str], ...] = (
    *(
        (name, "d")
        for name in (
            "area",
            "perimeter",
            "compactness",
            "kernel_length",
            "kernel_width",
            "asymmetry_coefficient",
            "kernel_groove_length",
        )
    ),
    ("variety", "label"),
)

_BEAN_FIELDS: tuple[tuple[str, str], ...] = (
    ("area", "i"),
    ("perimeter", "d"),
    ("major_axis_length", "d"),
    ("minor_axis_length", "d"),
    ("aspect_ratio", "d"),
    ("eccentricity", "d"),
    ("convex_area", "i"),
    ("equivalent_diameter", "d"),
    ("extent", "d"),
    ("solidity", "d"),
    ("roundness", "d"),
    ("compactness", "d"),
    *((f"shape_factor_{at}", "d") for at in range(1, 5)),
    ("bean", "label"),
)

#: The words Spambase counts, in the order its own description lists them.
#: The six punctuation counts are named rather than spelled, because a branch
#: called ``char_freq_$`` is not a name anything downstream can use.
_SPAM_WORDS: tuple[str, ...] = (
    "make",
    "address",
    "all",
    "3d",
    "our",
    "over",
    "remove",
    "internet",
    "order",
    "mail",
    "receive",
    "will",
    "people",
    "report",
    "addresses",
    "free",
    "business",
    "email",
    "you",
    "credit",
    "your",
    "font",
    "000",
    "money",
    "hp",
    "hpl",
    "george",
    "650",
    "lab",
    "labs",
    "telnet",
    "857",
    "data",
    "415",
    "85",
    "technology",
    "1999",
    "parts",
    "pm",
    "direct",
    "cs",
    "meeting",
    "original",
    "project",
    "re",
    "edu",
    "table",
    "conference",
)

_SPAM_FIELDS: tuple[tuple[str, str], ...] = (
    *((f"word_freq_{word}", "d") for word in _SPAM_WORDS),
    *(
        (f"char_freq_{name}", "d")
        for name in ("semicolon", "bracket", "square_bracket", "exclamation", "dollar", "hash")
    ),
    ("capital_run_length_average", "d"),
    ("capital_run_length_longest", "i"),
    ("capital_run_length_total", "i"),
    ("spam", "label"),
)

_IONOSPHERE_FIELDS: tuple[tuple[str, str], ...] = (
    *((f"pulse_{at // 2 + 1}_{'real' if at % 2 == 0 else 'imaginary'}", "d") for at in range(34)),
    ("returns", "label"),
)

_GLASS_FIELDS: tuple[tuple[str, str], ...] = (
    ("id", "i"),
    ("refractive_index", "d"),
    *(
        (name, "d")
        for name in (
            "sodium",
            "magnesium",
            "aluminium",
            "silicon",
            "potassium",
            "calcium",
            "barium",
            "iron",
        )
    ),
    ("kind", "label"),
)

_ABALONE_FIELDS: tuple[tuple[str, str], ...] = (
    ("sex", "label"),
    *(
        (name, "d")
        for name in (
            "length",
            "diameter",
            "height",
            "whole_weight",
            "shucked_weight",
            "viscera_weight",
            "shell_weight",
        )
    ),
    ("rings", "i"),
)

_BANKNOTE_FIELDS: tuple[tuple[str, str], ...] = (
    ("variance", "d"),
    ("skewness", "d"),
    ("kurtosis", "d"),
    ("entropy", "d"),
    ("authenticity", "label"),
)

_WINE_QUALITY_FIELDS: tuple[tuple[str, str], ...] = (
    *(
        (name, "d")
        for name in (
            "fixed_acidity",
            "volatile_acidity",
            "citric_acid",
            "residual_sugar",
            "chlorides",
            "free_sulfur_dioxide",
            "total_sulfur_dioxide",
            "density",
            "ph",
            "sulphates",
            "alcohol",
        )
    ),
    ("quality", "label"),
)

#: What the six activities a phone was told to recognise are, in the order
#: the labels file numbers them from one.
_HAR_ACTIVITIES: tuple[str, ...] = (
    "walking",
    "walking_upstairs",
    "walking_downstairs",
    "sitting",
    "standing",
    "laying",
)

#: The shower of light a gamma ray leaves in the telescope, measured ten ways.
_MAGIC_FIELDS: tuple[tuple[str, str], ...] = (
    *(
        (name, "d")
        for name in (
            "length",
            "width",
            "size",
            "conc",
            "conc1",
            "asym",
            "m3_long",
            "m3_trans",
            "alpha",
            "dist",
        )
    ),
    ("shower", "label"),
)

#: Eight numbers a pulsar candidate is reduced to: four from the folded pulse
#: profile, four from the curve of signal against dispersion measure.
_HTRU_FIELDS: tuple[tuple[str, str], ...] = (
    *(
        (f"{where}_{what}", "d")
        for where in ("profile", "dmsnr")
        for what in ("mean", "stdev", "kurtosis", "skew")
    ),
    ("candidate", "label"),
)

_MPG_FIELDS: tuple[tuple[str, str], ...] = (
    ("mpg", "target"),
    ("cylinders", "i"),
    ("displacement", "d"),
    ("horsepower", "d"),
    ("weight", "d"),
    ("acceleration", "d"),
    ("model_year", "i"),
    ("origin", "i"),
    ("car_name", "text"),
)

_BIKE_FIELDS: tuple[tuple[str, str], ...] = (
    ("instant", "i"),
    ("date", "date"),
    *((name, "i") for name in ("season", "year", "month", "hour", "holiday", "weekday")),
    ("workingday", "i"),
    ("weather", "i"),
    *((name, "d") for name in ("temperature", "feels_like", "humidity", "windspeed")),
    ("casual", "i"),
    ("registered", "i"),
    ("count", "target"),
)

_ENERGY_FIELDS: tuple[tuple[str, str], ...] = (
    ("relative_compactness", "d"),
    ("surface_area", "d"),
    ("wall_area", "d"),
    ("roof_area", "d"),
    ("overall_height", "d"),
    ("orientation", "i"),
    ("glazing_area", "d"),
    ("glazing_distribution", "i"),
    ("heating_load", "target"),
    ("cooling_load", "target"),
)

_ESTATE_FIELDS: tuple[tuple[str, str], ...] = (
    ("serial", "i"),
    ("transaction_date", "d"),
    ("house_age", "d"),
    ("distance_to_station", "d"),
    ("convenience_stores", "i"),
    ("latitude", "d"),
    ("longitude", "d"),
    ("price_per_unit_area", "target"),
)

#: The fourteen of the seventy-six columns that everyone uses, in the order
#: the processed files write them.
_HEART_FIELDS: tuple[tuple[str, str], ...] = (
    *(
        (name, "d")
        for name in (
            "age",
            "sex",
            "chest_pain",
            "rest_blood_pressure",
            "cholesterol",
            "fasting_sugar",
            "rest_ecg",
            "max_heart_rate",
            "exercise_angina",
            "st_depression",
            "st_slope",
            "vessels",
            "thallium",
        )
    ),
    ("diagnosis", "label"),
)

_CAR_FIELDS: tuple[tuple[str, str], ...] = (
    ("buying", "price"),
    ("maintenance", "price"),
    ("doors", "doors"),
    ("persons", "persons"),
    ("luggage_boot", "boot"),
    ("safety", "safety"),
    ("acceptability", "label"),
)

#: The categories the car set is written in, worst to best in each field.
_CAR_CODES: dict[str, Mapping[str, int] | Sequence[str]] = {
    "price": ("low", "med", "high", "vhigh"),
    "doors": ("2", "3", "4", "5more"),
    "persons": ("2", "4", "more"),
    "boot": ("small", "med", "big"),
    "safety": ("low", "med", "high"),
}

_YEAST_FIELDS: tuple[tuple[str, str], ...] = (
    ("protein", "text"),
    *((name, "d") for name in ("mcg", "gvh", "alm", "mit", "erl", "pox", "vac", "nuc")),
    ("site", "label"),
)

#: Where in the cell a yeast protein ends up, by the abbreviation the file uses.
_YEAST_SITES: dict[str, int] = {
    name: at
    for at, name in enumerate(
        ("CYT", "NUC", "MIT", "ME3", "ME2", "ME1", "EXC", "VAC", "POX", "ERL")
    )
}

_YES_NO: tuple[str, ...] = ("no", "yes")

#: The thirty-three answers each pupil's row holds, the last of them the mark
#: to predict. The names are the ones the questionnaire uses.
_STUDENT_FIELDS: tuple[tuple[str, str], ...] = (
    ("school", "school"),
    ("sex", "sex"),
    ("age", "i"),
    ("address", "address"),
    ("famsize", "famsize"),
    ("parents_status", "pstatus"),
    ("mother_education", "i"),
    ("father_education", "i"),
    ("mother_job", "job"),
    ("father_job", "job"),
    ("reason", "reason"),
    ("guardian", "guardian"),
    ("travel_time", "i"),
    ("study_time", "i"),
    ("failures", "i"),
    *(
        (name, "yesno")
        for name in (
            "school_support",
            "family_support",
            "paid_classes",
            "activities",
            "nursery",
            "wants_higher",
            "internet",
            "romantic",
        )
    ),
    *(
        (name, "i")
        for name in (
            "family_relations",
            "free_time",
            "going_out",
            "workday_alcohol",
            "weekend_alcohol",
            "health",
            "absences",
            "first_period",
            "second_period",
        )
    ),
    ("final_grade", "target"),
)

_STUDENT_CODES: dict[str, Mapping[str, int] | Sequence[str]] = {
    "school": ("GP", "MS"),
    "sex": ("F", "M"),
    "address": ("R", "U"),
    "famsize": ("LE3", "GT3"),
    "pstatus": ("A", "T"),
    "job": ("at_home", "health", "other", "services", "teacher"),
    "reason": ("course", "home", "other", "reputation"),
    "guardian": ("father", "mother", "other"),
    "yesno": _YES_NO,
}


#: Every dataset this module knows, by the name :func:`convert` takes.
#: The months a table spells with the first three letters of their name, and
#: the days a week is written the same way. A ``"date"`` column would be
#: better, but these files hold the month without the year to put it in.
_MONTHS: dict[str, int] = {
    name: at
    for at, name in enumerate(
        ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1
    )
}
_DAYS: dict[str, int] = {
    name: at for at, name in enumerate(("mon", "tue", "wed", "thu", "fri", "sat", "sun"), 1)
}

#: The days a table spells out in full, Monday first, the way ISO numbers them.
_WEEKDAYS: dict[str, int] = {
    name: at
    for at, name in enumerate(
        ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"), 1
    )
}

#: How the medical sets write down a patient's sex, and how the two of them
#: that ask a yes-or-no question with a capital letter write the answer.
_SEXES: tuple[str, ...] = ("Female", "Male")
_YES_NO_CAPS: tuple[str, ...] = ("No", "Yes")

#: The aerofoil in a wind tunnel, and the noise it made.
_AIRFOIL_FIELDS: tuple[tuple[str, str], ...] = (
    ("frequency", "i"),
    ("angle", "d"),
    ("chord", "d"),
    ("velocity", "d"),
    ("thickness", "d"),
    ("sound_pressure", "target"),
)

#: How a 1985 import was described in the trade press, and what it cost. The
#: make is text because there are twenty-two of them and no code for any.
_AUTOMOBILE_FIELDS: tuple[tuple[str, str], ...] = (
    ("symboling", "i"),
    ("normalised_losses", "i"),
    ("make", "text"),
    ("fuel_type", "fuel"),
    ("aspiration", "aspiration"),
    ("doors", "doors"),
    ("body_style", "body"),
    ("drive_wheels", "drive"),
    ("engine_location", "where"),
    ("wheel_base", "d"),
    ("length", "d"),
    ("width", "d"),
    ("height", "d"),
    ("curb_weight", "i"),
    ("engine_type", "engine"),
    ("cylinders", "cylinders"),
    ("engine_size", "i"),
    ("fuel_system", "fuel_system"),
    ("bore", "d"),
    ("stroke", "d"),
    ("compression_ratio", "d"),
    ("horsepower", "i"),
    ("peak_rpm", "i"),
    ("city_mpg", "i"),
    ("highway_mpg", "i"),
    ("price", "target"),
)
_AUTOMOBILE_CODES: dict[str, tuple[str, ...] | dict[str, int]] = {
    "fuel": ("diesel", "gas"),
    "aspiration": ("std", "turbo"),
    "doors": {"two": 2, "four": 4},
    "body": ("convertible", "hardtop", "hatchback", "sedan", "wagon"),
    "drive": ("4wd", "fwd", "rwd"),
    "where": ("front", "rear"),
    "engine": ("dohc", "dohcv", "l", "ohc", "ohcf", "ohcv", "rotor"),
    "cylinders": {"two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "eight": 8, "twelve": 12},
    "fuel_system": ("1bbl", "2bbl", "4bbl", "idi", "mfi", "mpfi", "spdi", "spfi"),
}

#: Weights on the two arms of a balance, and which way it tipped.
_BALANCE_FIELDS: tuple[tuple[str, str], ...] = (
    ("label", "label"),
    ("left_weight", "i"),
    ("left_distance", "i"),
    ("right_weight", "i"),
    ("right_distance", "i"),
)

#: One telephone call of a Portuguese bank's campaign, and whether it sold
#: anything. ``pdays`` is -1 when nobody had called before.
_BANK_FIELDS: tuple[tuple[str, str], ...] = (
    ("age", "i"),
    ("job", "job"),
    ("marital", "marital"),
    ("education", "education"),
    ("credit_default", "yesno"),
    ("balance", "i"),
    ("housing", "yesno"),
    ("loan", "yesno"),
    ("contact", "contact"),
    ("day", "i"),
    ("month", "month"),
    ("duration", "i"),
    ("campaign", "i"),
    ("pdays", "i"),
    ("previous", "i"),
    ("outcome", "outcome"),
    ("label", "label"),
)
_BANK_CODES: dict[str, tuple[str, ...] | dict[str, int]] = {
    "job": (
        "admin.",
        "blue-collar",
        "entrepreneur",
        "housemaid",
        "management",
        "retired",
        "self-employed",
        "services",
        "student",
        "technician",
        "unemployed",
        "unknown",
    ),
    "marital": ("divorced", "married", "single"),
    "education": ("primary", "secondary", "tertiary", "unknown"),
    "yesno": _YES_NO,
    "contact": ("cellular", "telephone", "unknown"),
    "month": _MONTHS,
    "outcome": ("failure", "other", "success", "unknown"),
}

#: How often a donor gave blood, and whether they gave again in March 2007.
_BLOOD_FIELDS: tuple[tuple[str, str], ...] = (
    ("recency", "i"),
    ("frequency", "i"),
    ("volume", "i"),
    ("time", "i"),
    ("label", "label"),
)

#: The eighteen knobs on an ocean model, and whether the run finished. Two
#: columns say which of the Latin hypercube studies the run belongs to.
_CLIMATE_FIELDS: tuple[tuple[str, str], ...] = (
    ("study", "i"),
    ("run", "i"),
    *_plain(
        (
            "vconst_corr",
            "vconst_2",
            "vconst_3",
            "vconst_4",
            "vconst_5",
            "vconst_7",
            "ah_corr",
            "ah_bolus",
            "slm_corr",
            "efficiency_factor",
            "tidal_mix_max",
            "vertical_decay_scale",
            "convect_corr",
            "bckgrnd_vdc1",
            "bckgrnd_vdc_ban",
            "bckgrnd_vdc_eq",
            "bckgrnd_vdc_psim",
            "prandtl",
        ),
        "d",
    ),
    ("label", "label"),
)

#: A 1987 mainframe, its published relative performance to predict, and the
#: estimate the paper that came with the data published beside it.
_HARDWARE_FIELDS: tuple[tuple[str, str], ...] = (
    ("vendor", "text"),
    ("model", "text"),
    ("cycle_time", "i"),
    ("min_memory", "i"),
    ("max_memory", "i"),
    ("cache", "i"),
    ("min_channels", "i"),
    ("max_channels", "i"),
    ("performance", "target"),
    ("estimated_performance", "d"),
)

#: What went into a batch of concrete, and the three things measured of it.
_SLUMP_FIELDS: tuple[tuple[str, str], ...] = (
    ("number", "i"),
    ("cement", "d"),
    ("slag", "d"),
    ("fly_ash", "d"),
    ("water", "d"),
    ("superplasticiser", "d"),
    ("coarse_aggregate", "d"),
    ("fine_aggregate", "d"),
    ("slump", "target"),
    ("flow", "target"),
    ("compressive_strength", "target"),
)

#: A 1987 Indonesian survey. Every answer is already a number, so the codes
#: are the file's own: education and standard of living run low to high.
_CMC_FIELDS: tuple[tuple[str, str], ...] = (
    ("wife_age", "i"),
    ("wife_education", "i"),
    ("husband_education", "i"),
    ("children", "i"),
    ("wife_islam", "i"),
    ("wife_working", "i"),
    ("husband_occupation", "i"),
    ("living_standard", "i"),
    ("media_exposure", "i"),
    ("label", "label"),
)

#: Thirty-three signs a dermatologist or a microscope found, then the age.
_DERM_FIELDS: tuple[tuple[str, str], ...] = (
    *_plain(
        (
            "erythema",
            "scaling",
            "definite_borders",
            "itching",
            "koebner_phenomenon",
            "polygonal_papules",
            "follicular_papules",
            "oral_mucosal_involvement",
            "knee_and_elbow_involvement",
            "scalp_involvement",
            "family_history",
            "melanin_incontinence",
            "eosinophils_in_infiltrate",
            "pnl_infiltrate",
            "fibrosis_of_papillary_dermis",
            "exocytosis",
            "acanthosis",
            "hyperkeratosis",
            "parakeratosis",
            "clubbing_of_rete_ridges",
            "elongation_of_rete_ridges",
            "thinning_of_suprapapillary_epidermis",
            "spongiform_pustule",
            "munro_microabcess",
            "focal_hypergranulosis",
            "disappearance_of_granular_layer",
            "vacuolisation_and_damage_of_basal_layer",
            "spongiosis",
            "saw_tooth_appearance_of_retes",
            "follicular_horn_plug",
            "perifollicular_parakeratosis",
            "inflammatory_mononuclear_infiltrate",
            "band_like_infiltrate",
            "age",
        )
    ),
    ("label", "label"),
)

#: Sixteen symptoms a patient was asked about, and the two they were not.
_DIABETES_FIELDS: tuple[tuple[str, str], ...] = (
    ("age", "i"),
    ("sex", "sex"),
    *_plain(
        (
            "polyuria",
            "polydipsia",
            "sudden_weight_loss",
            "weakness",
            "polyphagia",
            "genital_thrush",
            "visual_blurring",
            "itching",
            "irritability",
            "delayed_healing",
            "partial_paresis",
            "muscle_stiffness",
            "alopecia",
            "obesity",
        ),
        "yesno",
    ),
    ("label", "label"),
)

#: Seven scores a program gave a protein, and the accession number it has in
#: SWISS-PROT, which is text because it names the protein rather than measures
#: anything about it.
_ECOLI_FIELDS: tuple[tuple[str, str], ...] = (
    ("sequence", "text"),
    ("mcg", "d"),
    ("gvh", "d"),
    ("lip", "d"),
    ("chg", "d"),
    ("aac", "d"),
    ("alm1", "d"),
    ("alm2", "d"),
    ("label", "label"),
)

#: Nine answers about a man's health and habits, each already normalised to
#: run from -1 to 1, and whether his semen analysis came back normal.
_FERTILITY_FIELDS: tuple[tuple[str, str], ...] = (
    ("season", "d"),
    ("age", "d"),
    ("childhood_diseases", "i"),
    ("accident", "i"),
    ("surgery", "i"),
    ("high_fevers", "i"),
    ("alcohol", "d"),
    ("smoking", "i"),
    ("hours_sitting", "d"),
    ("label", "label"),
)

#: Where in the park a fire started, when, what the Canadian fire weather
#: indices said that day, and how many hectares went up.
_FIRE_FIELDS: tuple[tuple[str, str], ...] = (
    ("x", "i"),
    ("y", "i"),
    ("month", "month"),
    ("day", "day"),
    ("ffmc", "d"),
    ("dmc", "d"),
    ("dc", "d"),
    ("isi", "d"),
    ("temperature", "d"),
    ("humidity", "i"),
    ("wind", "d"),
    ("rain", "d"),
    ("burnt_area", "target"),
)

#: A day's work by one team in a Bangladeshi clothing factory. ``wip`` is
#: blank for the finishing teams, who have no work in progress to count.
_GARMENT_FIELDS: tuple[tuple[str, str], ...] = (
    ("when", "date"),
    ("quarter", "quarter"),
    ("department", "department"),
    ("day", "weekday"),
    ("team", "i"),
    ("targeted_productivity", "d"),
    ("smv", "d"),
    ("work_in_progress", "d"),
    ("over_time", "i"),
    ("incentive", "d"),
    ("idle_time", "d"),
    ("idle_men", "i"),
    ("style_changes", "i"),
    ("workers", "d"),
    ("productivity", "target"),
)

#: Twenty things a German bank knew about an applicant in 1994. The
#: categorical ones are written as the file's own A-codes, and each is
#: numbered in the order the documentation lists it.
_GERMAN_FIELDS: tuple[tuple[str, str], ...] = (
    ("checking_account", "checking"),
    ("duration", "i"),
    ("credit_history", "history"),
    ("purpose", "purpose"),
    ("amount", "i"),
    ("savings", "savings"),
    ("employment", "employment"),
    ("instalment_rate", "i"),
    ("personal_status", "personal"),
    ("other_debtors", "debtors"),
    ("residence_since", "i"),
    ("property", "property"),
    ("age", "i"),
    ("other_instalments", "instalments"),
    ("housing", "housing"),
    ("existing_credits", "i"),
    ("job", "job"),
    ("dependents", "i"),
    ("telephone", "telephone"),
    ("foreign_worker", "foreign"),
    ("label", "label"),
)
_GERMAN_CODES: dict[str, tuple[str, ...]] = {
    "checking": ("A11", "A12", "A13", "A14"),
    "history": ("A30", "A31", "A32", "A33", "A34"),
    "purpose": (
        "A40",
        "A41",
        "A42",
        "A43",
        "A44",
        "A45",
        "A46",
        "A47",
        "A48",
        "A49",
        "A410",
    ),
    "savings": ("A61", "A62", "A63", "A64", "A65"),
    "employment": ("A71", "A72", "A73", "A74", "A75"),
    "personal": ("A91", "A92", "A93", "A94", "A95"),
    "debtors": ("A101", "A102", "A103"),
    "property": ("A121", "A122", "A123", "A124"),
    "instalments": ("A141", "A142", "A143"),
    "housing": ("A151", "A152", "A153"),
    "job": ("A171", "A172", "A173", "A174"),
    "telephone": ("A191", "A192"),
    "foreign": ("A201", "A202"),
}

#: A breast cancer operation at Billings Hospital, and whether the patient
#: was still alive five years later.
_HABERMAN_FIELDS: tuple[tuple[str, str], ...] = (
    ("age", "i"),
    ("year", "i"),
    ("nodes", "i"),
    ("label", "label"),
)

#: Blood work from a heart failure clinic in Faisalabad, and ``time``, the
#: days the patient was followed for before the study ended.
_FAILURE_FIELDS: tuple[tuple[str, str], ...] = (
    ("age", "d"),
    ("anaemia", "i"),
    ("creatinine_phosphokinase", "i"),
    ("diabetes", "i"),
    ("ejection_fraction", "i"),
    ("high_blood_pressure", "i"),
    ("platelets", "d"),
    ("serum_creatinine", "d"),
    ("serum_sodium", "i"),
    ("sex", "i"),
    ("smoking", "i"),
    ("time", "i"),
    ("label", "label"),
)

#: Nineteen things asked of a hepatitis patient. The yes-or-no answers are
#: written 1 for no and 2 for yes, which is the file's own numbering.
_HEPATITIS_FIELDS: tuple[tuple[str, str], ...] = (
    ("label", "label"),
    ("age", "i"),
    *_plain(
        (
            "sex",
            "steroid",
            "antivirals",
            "fatigue",
            "malaise",
            "anorexia",
            "liver_big",
            "liver_firm",
            "spleen_palpable",
            "spiders",
            "ascites",
            "varices",
        )
    ),
    ("bilirubin", "d"),
    ("alkaline_phosphate", "i"),
    ("sgot", "i"),
    ("albumin", "d"),
    ("prothrombin_time", "i"),
    ("histology", "i"),
)

#: Blood work from 583 patients in Andhra Pradesh. Four rows never had the
#: albumin to globulin ratio worked out and leave it blank.
_ILPD_FIELDS: tuple[tuple[str, str], ...] = (
    ("age", "i"),
    ("sex", "sex"),
    ("total_bilirubin", "d"),
    ("direct_bilirubin", "d"),
    ("alkaline_phosphotase", "i"),
    ("alamine_aminotransferase", "i"),
    ("aspartate_aminotransferase", "i"),
    ("total_proteins", "d"),
    ("albumin", "d"),
    ("albumin_globulin_ratio", "d"),
    ("label", "label"),
)

#: Nineteen numbers describing a 3x3 patch cut out of an outdoor photograph.
_SEGMENT_FIELDS: tuple[tuple[str, str], ...] = (
    ("label", "label"),
    *_plain(
        (
            "region_centroid_col",
            "region_centroid_row",
            "region_pixel_count",
            "short_line_density_5",
            "short_line_density_2",
            "vedge_mean",
            "vedge_sd",
            "hedge_mean",
            "hedge_sd",
            "intensity_mean",
            "rawred_mean",
            "rawblue_mean",
            "rawgreen_mean",
            "exred_mean",
            "exblue_mean",
            "exgreen_mean",
            "value_mean",
            "saturation_mean",
            "hue_mean",
        ),
        "d",
    ),
)

#: Five blood tests and the drinking to predict from them. The seventh column
#: is not a class: whoever made the file used it to split the rows in two, and
#: reading it as a diagnosis is the mistake this dataset is known for.
_BUPA_FIELDS: tuple[tuple[str, str], ...] = (
    ("mcv", "i"),
    ("alkaline_phosphotase", "i"),
    ("sgpt", "i"),
    ("sgot", "i"),
    ("gamma_glutamyl_transpeptidase", "i"),
    ("drinks", "target"),
    ("selector", "i"),
)

#: Eighteen findings from a lymph node X-ray, each already numbered by where
#: it comes in the list the documentation gives for that field.
_LYMPH_FIELDS: tuple[tuple[str, str], ...] = (
    ("label", "label"),
    *_plain(
        (
            "lymphatics",
            "block_of_afferent",
            "block_of_lymph_c",
            "block_of_lymph_s",
            "bypass",
            "extravasates",
            "regeneration",
            "early_uptake",
            "lymph_nodes_diminished",
            "lymph_nodes_enlarged",
            "changes_in_lymph",
            "defect_in_node",
            "changes_in_node",
            "changes_in_structure",
            "special_forms",
            "dislocation",
            "exclusion_of_node",
            "number_of_nodes",
        )
    ),
)

#: What a radiologist wrote down about a lump on a mammogram.
_MAMMOGRAM_FIELDS: tuple[tuple[str, str], ...] = (
    ("birads", "i"),
    ("age", "i"),
    ("shape", "i"),
    ("margin", "i"),
    ("density", "i"),
    ("label", "label"),
)

#: Six readings taken in rural Bangladeshi clinics, and the risk given.
_MATERNAL_FIELDS: tuple[tuple[str, str], ...] = (
    ("age", "i"),
    ("systolic_bp", "i"),
    ("diastolic_bp", "i"),
    ("blood_sugar", "d"),
    ("body_temperature", "d"),
    ("heart_rate", "i"),
    ("label", "label"),
)

#: Eight things a Ljubljana nursery asked about a family, and the ranking the
#: application got. Every combination of the eight is in the file once.
_NURSERY_FIELDS: tuple[tuple[str, str], ...] = (
    ("parents", "parents"),
    ("has_nursery", "nursery"),
    ("form", "form"),
    ("children", "children"),
    ("housing", "housing"),
    ("finance", "finance"),
    ("social", "social"),
    ("health", "health"),
    ("label", "label"),
)
_NURSERY_CODES: dict[str, tuple[str, ...] | dict[str, int]] = {
    "parents": ("usual", "pretentious", "great_pret"),
    "nursery": ("proper", "less_proper", "improper", "critical", "very_crit"),
    "form": ("complete", "completed", "incomplete", "foster"),
    "children": {"1": 1, "2": 2, "3": 3, "more": 4},
    "housing": ("convenient", "less_conv", "critical"),
    "finance": ("convenient", "inconv"),
    "social": ("nonprob", "slightly_prob", "problematic"),
    "health": ("recommended", "priority", "not_recom"),
}

#: A minute in an office, and whether anyone was in it. The first column is
#: the row's number in the file, which the file itself writes out.
_OCCUPANCY_FIELDS: tuple[tuple[str, str], ...] = (
    ("number", "i"),
    ("when", "time"),
    ("temperature", "d"),
    ("humidity", "d"),
    ("light", "d"),
    ("carbon_dioxide", "d"),
    ("humidity_ratio", "d"),
    ("label", "label"),
)

#: A session on a shopping site, and whether it ended in a sale.
_SHOPPER_FIELDS: tuple[tuple[str, str], ...] = (
    ("administrative", "i"),
    ("administrative_duration", "d"),
    ("informational", "i"),
    ("informational_duration", "d"),
    ("product_related", "i"),
    ("product_related_duration", "d"),
    ("bounce_rate", "d"),
    ("exit_rate", "d"),
    ("page_value", "d"),
    ("special_day", "d"),
    ("month", "month"),
    ("operating_system", "i"),
    ("browser", "i"),
    ("region", "i"),
    ("traffic_type", "i"),
    ("visitor_type", "visitor"),
    ("weekend", "truefalse"),
    ("label", "label"),
)
_SHOPPER_CODES: dict[str, tuple[str, ...] | dict[str, int]] = {
    "month": {
        "Feb": 2,
        "Mar": 3,
        "Apr": 4,
        "May": 5,
        "June": 6,
        "Jul": 7,
        "Aug": 8,
        "Sep": 9,
        "Oct": 10,
        "Nov": 11,
        "Dec": 12,
    },
    "visitor": ("New_Visitor", "Other", "Returning_Visitor"),
    "truefalse": {"FALSE": 0, "TRUE": 1},
}

#: Twenty-two measurements of one sustained vowel, and whose voice it was.
#: The recording is named rather than measured, so it is text.
_PARKINSONS_FIELDS: tuple[tuple[str, str], ...] = (
    ("recording", "text"),
    ("fo", "d"),
    ("fhi", "d"),
    ("flo", "d"),
    ("jitter_percent", "d"),
    ("jitter_absolute", "d"),
    ("rap", "d"),
    ("ppq", "d"),
    ("jitter_ddp", "d"),
    ("shimmer", "d"),
    ("shimmer_db", "d"),
    ("apq3", "d"),
    ("apq5", "d"),
    ("apq", "d"),
    ("shimmer_dda", "d"),
    ("nhr", "d"),
    ("hnr", "d"),
    ("label", "label"),
    ("rpde", "d"),
    ("dfa", "d"),
    ("spread1", "d"),
    ("spread2", "d"),
    ("d2", "d"),
    ("ppe", "d"),
)

#: The same voice measurements taken at home over six months, with the two
#: halves of the clinician's score to predict from them.
_TELEMONITORING_FIELDS: tuple[tuple[str, str], ...] = (
    ("subject", "i"),
    ("age", "i"),
    ("sex", "i"),
    ("test_time", "d"),
    ("motor_updrs", "target"),
    ("total_updrs", "target"),
    ("jitter_percent", "d"),
    ("jitter_absolute", "d"),
    ("rap", "d"),
    ("ppq5", "d"),
    ("jitter_ddp", "d"),
    ("shimmer", "d"),
    ("shimmer_db", "d"),
    ("apq3", "d"),
    ("apq5", "d"),
    ("apq11", "d"),
    ("shimmer_dda", "d"),
    ("nhr", "d"),
    ("hnr", "d"),
    ("rpde", "d"),
    ("dfa", "d"),
    ("ppe", "d"),
)

#: Eight points off a pen's path across a tablet, resampled so that every
#: digit is written with the same number of them.
_PENDIGIT_FIELDS: tuple[tuple[str, str], ...] = (
    *tuple(field for at in range(1, 9) for field in ((f"x{at}", "i"), (f"y{at}", "i"))),
    ("label", "label"),
)

#: Thirty rules of thumb for spotting a phishing site. Each is -1, 0 or 1:
#: suspicious, in between, or fine.
_PHISHING_FIELDS: tuple[tuple[str, str], ...] = (
    *_plain(
        (
            "having_ip_address",
            "url_length",
            "shortening_service",
            "having_at_symbol",
            "double_slash_redirecting",
            "prefix_suffix",
            "having_sub_domain",
            "ssl_final_state",
            "domain_registration_length",
            "favicon",
            "port",
            "https_token",
            "request_url",
            "url_of_anchor",
            "links_in_tags",
            "server_form_handler",
            "submitting_to_email",
            "abnormal_url",
            "redirect",
            "on_mouseover",
            "right_click",
            "popup_window",
            "iframe",
            "age_of_domain",
            "dns_record",
            "web_traffic",
            "page_rank",
            "google_index",
            "links_pointing_to_page",
            "statistical_report",
        )
    ),
    ("label", "label"),
)

#: The ambient conditions a gas turbine ran in, and the megawatts it made.
_POWER_FIELDS: tuple[tuple[str, str], ...] = (
    ("temperature", "d"),
    ("exhaust_vacuum", "d"),
    ("ambient_pressure", "d"),
    ("relative_humidity", "d"),
    ("power_output", "target"),
)

#: Molecular descriptors, and the concentration that killed half of a
#: population in 48 hours - Daphnia magna here, fathead minnow below.
_AQUATIC_FIELDS: tuple[tuple[str, str], ...] = (
    ("tpsa", "d"),
    ("saacc", "d"),
    ("h_050", "i"),
    ("mlogp", "d"),
    ("rdchi", "d"),
    ("gats1p", "d"),
    ("nitrogen_atoms", "i"),
    ("c_040", "i"),
    ("lc50", "target"),
)
_FISH_FIELDS: tuple[tuple[str, str], ...] = (
    ("cic0", "d"),
    ("sm1_dz", "d"),
    ("gats1i", "d"),
    ("ndsch", "i"),
    ("ndssc", "i"),
    ("mlogp", "d"),
    ("lc50", "target"),
)

#: The shape of one grain, measured off a photograph of it.
_RAISIN_FIELDS: tuple[tuple[str, str], ...] = (
    ("area", "i"),
    ("major_axis", "d"),
    ("minor_axis", "d"),
    ("eccentricity", "d"),
    ("convex_area", "i"),
    ("extent", "d"),
    ("perimeter", "d"),
    ("label", "label"),
)
_RICE_FIELDS: tuple[tuple[str, str], ...] = (
    ("area", "i"),
    ("perimeter", "d"),
    ("major_axis", "d"),
    ("minor_axis", "d"),
    ("eccentricity", "d"),
    ("convex_area", "i"),
    ("extent", "d"),
    ("label", "label"),
)

#: Four Landsat bands for each of the nine pixels around one, read out left
#: to right and top to bottom, so the middle pixel is the fifth of the nine.
_SATELLITE_FIELDS: tuple[tuple[str, str], ...] = (
    *tuple((f"pixel{at}_band{band}", "i") for at in range(1, 10) for band in range(1, 5)),
    ("label", "label"),
)

#: A shift in a Polish coal mine, and whether the seismometers went off.
_SEISMIC_FIELDS: tuple[tuple[str, str], ...] = (
    ("seismic", "hazard"),
    ("seismoacoustic", "hazard"),
    ("shift", "shift"),
    ("genergy", "d"),
    ("gpuls", "d"),
    ("gdenergy", "d"),
    ("gdpuls", "d"),
    ("ghazard", "hazard"),
    *_plain(("bumps", "bumps2", "bumps3", "bumps4", "bumps5", "bumps6", "bumps7", "bumps89")),
    ("energy", "d"),
    ("max_energy", "d"),
    ("label", "label"),
)

#: How a servomechanism was set up, and how long it took to settle.
_SERVO_FIELDS: tuple[tuple[str, str], ...] = (
    ("motor", "part"),
    ("screw", "part"),
    ("pgain", "i"),
    ("vgain", "i"),
    ("rise_time", "target"),
)

#: An active region on the sun, and how many flares of each size came out of
#: it in the next 24 hours.
_FLARE_FIELDS: tuple[tuple[str, str], ...] = (
    ("label", "label"),
    ("spot_size", "spots"),
    ("spot_distribution", "spread"),
    ("activity", "i"),
    ("evolution", "i"),
    ("previous_activity", "i"),
    ("historically_complex", "i"),
    ("became_complex", "i"),
    ("area", "i"),
    ("largest_spot_area", "i"),
    ("c_flares", "i"),
    ("m_flares", "i"),
    ("x_flares", "i"),
)

#: Sixty energies off a sonar chirp, and what it bounced off.
_SONAR_FIELDS: tuple[tuple[str, str], ...] = (
    *_plain(tuple(f"band_{at}" for at in range(1, 61)), "d"),
    ("label", "label"),
)

#: Thirty-five things a farmer could see, already numbered by the file.
_SOYBEAN_FIELDS: tuple[tuple[str, str], ...] = (
    ("label", "label"),
    *_plain(
        (
            "date",
            "plant_stand",
            "precipitation",
            "temperature",
            "hail",
            "crop_history",
            "area_damaged",
            "severity",
            "seed_treatment",
            "germination",
            "plant_growth",
            "leaves",
            "leafspots_halo",
            "leafspots_margin",
            "leafspot_size",
            "leaf_shread",
            "leaf_malformation",
            "leaf_mildew",
            "stem",
            "lodging",
            "stem_cankers",
            "canker_lesion",
            "fruiting_bodies",
            "external_decay",
            "mycelium",
            "internal_discolouration",
            "sclerotia",
            "fruit_pods",
            "fruit_spots",
            "seed",
            "mould_growth",
            "seed_discolouration",
            "seed_size",
            "shrivelling",
            "roots",
        )
    ),
)
_SOYBEAN_DISEASES: tuple[str, ...] = (
    "diaporthe-stem-canker",
    "charcoal-rot",
    "rhizoctonia-root-rot",
    "phytophthora-rot",
    "brown-stem-rot",
    "powdery-mildew",
    "downy-mildew",
    "brown-spot",
    "bacterial-blight",
    "bacterial-pustule",
    "purple-seed-stain",
    "anthracnose",
    "phyllosticta-leaf-spot",
    "alternarialeaf-spot",
    "frog-eye-leaf-spot",
    "diaporthe-pod-&-stem-blight",
    "cyst-nematode",
    "2-4-d-injury",
    "herbicide-injury",
)

#: The thirteen columns Statlog kept of the Cleveland heart study, all of
#: them written as decimals however whole the number is.
_STATLOG_HEART_FIELDS: tuple[tuple[str, str], ...] = (
    *_plain(
        (
            "age",
            "sex",
            "chest_pain",
            "resting_bp",
            "cholesterol",
            "fasting_sugar",
            "resting_ecg",
            "max_heart_rate",
            "exercise_angina",
            "st_depression",
            "st_slope",
            "vessels",
            "thal",
        ),
        "d",
    ),
    ("label", "label"),
)

#: A quarter of an hour in a South Korean steel plant. ``seconds_from_midnight``
#: is the file's own NSM column, and says the same thing as the time of day in
#: ``when``.
_STEEL_FIELDS: tuple[tuple[str, str], ...] = (
    ("when", "time"),
    ("usage", "d"),
    ("lagging_reactive_power", "d"),
    ("leading_reactive_power", "d"),
    ("carbon_dioxide", "d"),
    ("lagging_power_factor", "d"),
    ("leading_power_factor", "d"),
    ("seconds_from_midnight", "i"),
    ("week_status", "week"),
    ("day", "weekday"),
    ("label", "label"),
)

#: The nine squares of a finished game, x first. A square is 1 for x, -1 for
#: o and 0 for blank, so that a board adds up the way a player would read it.
_TICTACTOE_FIELDS: tuple[tuple[str, str], ...] = (
    *tuple(
        (f"{row}_{column}", "square")
        for row in ("top", "middle", "bottom")
        for column in ("left", "middle", "right")
    ),
    ("label", "label"),
)

#: Six angles and distances measured off a lower spine.
_VERTEBRAL_FIELDS: tuple[tuple[str, str], ...] = (
    *_plain(
        (
            "pelvic_incidence",
            "pelvic_tilt",
            "lumbar_lordosis_angle",
            "sacral_slope",
            "pelvic_radius",
            "spondylolisthesis_grade",
        ),
        "d",
    ),
    ("label", "label"),
)

#: How strong each of seven wifi points was, and which room that was in.
_WIFI_FIELDS: tuple[tuple[str, str], ...] = (
    *_plain(tuple(f"signal_{at}" for at in range(1, 8))),
    ("label", "label"),
)

#: The hull of a sailing yacht, and the resistance the tank measured.
_YACHT_FIELDS: tuple[tuple[str, str], ...] = (
    ("longitudinal_position", "d"),
    ("prismatic_coefficient", "d"),
    ("length_displacement_ratio", "d"),
    ("beam_draught_ratio", "d"),
    ("length_beam_ratio", "d"),
    ("froude_number", "d"),
    ("residuary_resistance", "target"),
)

#: Sixteen things an animal either does or has, then how many legs.
_ZOO_FIELDS: tuple[tuple[str, str], ...] = (
    ("animal", "text"),
    *_plain(
        (
            "hair",
            "feathers",
            "eggs",
            "milk",
            "airborne",
            "aquatic",
            "predator",
            "toothed",
            "backbone",
            "breathes",
            "venomous",
            "fins",
            "legs",
            "tail",
            "domestic",
            "catsize",
        )
    ),
    ("label", "label"),
)


#: The hundred sets below are read from the archive's own ``data.csv``, which
#: is the file its Python package downloads: one header row naming the columns
#: the way the dataset's own documentation names them, then a row per example.
#: The fields keep those names, a categorical column keeps the categories the
#: file actually writes, and a column the archive marks as the thing to predict
#: becomes the label or the target depending on whether it names a class or
#: measures a number.
_ABSENTEEISM_AT_WORK_FIELDS: tuple[tuple[str, str], ...] = (
    ("id", "i"),
    ("reason_for_absence", "i"),
    ("month_of_absence", "i"),
    ("day_of_the_week", "i"),
    ("seasons", "i"),
    ("transportation_expense", "i"),
    ("distance_from_residence_to_work", "i"),
    ("service_time", "i"),
    ("age", "i"),
    ("work_load_average_day", "d"),
    ("hit_target", "i"),
    ("disciplinary_failure", "i"),
    ("education", "i"),
    ("son", "i"),
    ("social_drinker", "i"),
    ("social_smoker", "i"),
    ("pet", "i"),
    ("weight", "i"),
    ("height", "i"),
    ("body_mass_index", "i"),
    ("absenteeism_time_in_hours", "target"),
)

_ACUTE_INFLAMMATIONS_FIELDS: tuple[tuple[str, str], ...] = (
    ("temperature", "d"),
    ("nausea", "nausea"),
    ("lumbar_pain", "nausea"),
    ("urine_pushing", "nausea"),
    ("micturition_pains", "nausea"),
    ("burning_urethra", "nausea"),
    ("bladder_inflammation", "label"),
    ("nephritis", "nausea"),
)

_AIDS_CLINICAL_TRIALS_FIELDS: tuple[tuple[str, str], ...] = (
    ("pidnum", "i"),
    ("cid", "label"),
    ("time", "i"),
    ("trt", "i"),
    ("age", "i"),
    ("wtkg", "d"),
    ("hemo", "i"),
    ("homo", "i"),
    ("drugs", "i"),
    ("karnof", "i"),
    ("oprior", "i"),
    ("z30", "i"),
    ("zprior", "i"),
    ("preanti", "i"),
    ("race", "i"),
    ("gender", "i"),
    ("str2", "i"),
    ("strat", "i"),
    ("symptom", "i"),
    ("treat", "i"),
    ("offtrt", "i"),
    ("cd40", "i"),
    ("cd420", "i"),
    ("cd80", "i"),
    ("cd820", "i"),
)

_ANDROID_PERMISSIONS_FIELDS: tuple[tuple[str, str], ...] = (
    ("android_permission_get_accounts", "i"),
    ("com_sonyericsson_home_permission_broadcast_badge", "i"),
    ("android_permission_read_profile", "i"),
    ("android_permission_manage_accounts", "i"),
    ("android_permission_write_sync_settings", "i"),
    ("android_permission_read_external_storage", "i"),
    ("android_permission_receive_sms", "i"),
    ("com_android_launcher_permission_read_settings", "i"),
    ("android_permission_write_settings", "i"),
    ("com_google_android_providers_gsf_permission_read_gservices", "i"),
    ("android_permission_download_without_notification", "i"),
    ("android_permission_get_tasks", "i"),
    ("android_permission_write_external_storage", "i"),
    ("android_permission_record_audio", "i"),
    ("com_huawei_android_launcher_permission_change_badge", "i"),
    ("com_oppo_launcher_permission_read_settings", "i"),
    ("android_permission_change_network_state", "i"),
    ("com_android_launcher_permission_install_shortcut", "i"),
    ("android_permission_android_permission_read_phone_state", "i"),
    ("android_permission_call_phone", "i"),
    ("android_permission_write_contacts", "i"),
    ("android_permission_read_phone_state", "i"),
    ("com_samsung_android_providers_context_permission_write_use_app_feature_survey", "i"),
    ("android_permission_modify_audio_settings", "i"),
    ("android_permission_access_location_extra_commands", "i"),
    ("android_permission_internet", "i"),
    ("android_permission_mount_unmount_filesystems", "i"),
    ("com_majeur_launcher_permission_update_badge", "i"),
    ("android_permission_authenticate_accounts", "i"),
    ("com_htc_launcher_permission_read_settings", "i"),
    ("android_permission_access_wifi_state", "i"),
    ("android_permission_flashlight", "i"),
    ("android_permission_read_app_badge", "i"),
    ("android_permission_use_credentials", "i"),
    ("android_permission_change_configuration", "i"),
    ("android_permission_read_sync_settings", "i"),
    ("android_permission_broadcast_sticky", "i"),
    ("com_anddoes_launcher_permission_update_count", "i"),
    ("com_android_alarm_permission_set_alarm", "i"),
    ("com_google_android_c2dm_permission_receive", "i"),
    ("android_permission_kill_background_processes", "i"),
    ("com_sonymobile_home_permission_provider_insert_badge", "i"),
    ("com_sec_android_provider_badge_permission_read", "i"),
    ("android_permission_write_calendar", "i"),
    ("android_permission_send_sms", "i"),
    ("com_huawei_android_launcher_permission_write_settings", "i"),
    ("android_permission_request_install_packages", "i"),
    ("android_permission_set_wallpaper_hints", "i"),
    ("android_permission_set_wallpaper", "i"),
    ("com_oppo_launcher_permission_write_settings", "i"),
    ("android_permission_restart_packages", "i"),
    ("me_everything_badger_permission_badge_count_write", "i"),
    ("android_permission_access_mock_location", "i"),
    ("android_permission_access_coarse_location", "i"),
    ("android_permission_read_logs", "i"),
    ("com_google_android_gms_permission_activity_recognition", "i"),
    ("com_amazon_device_messaging_permission_receive", "i"),
    ("android_permission_system_alert_window", "i"),
    ("android_permission_disable_keyguard", "i"),
    ("android_permission_use_fingerprint", "i"),
    ("me_everything_badger_permission_badge_count_read", "i"),
    ("android_permission_change_wifi_state", "i"),
    ("android_permission_read_contacts", "i"),
    ("com_android_vending_billing", "i"),
    ("android_permission_read_calendar", "i"),
    ("android_permission_receive_boot_completed", "i"),
    ("android_permission_wake_lock", "i"),
    ("android_permission_access_fine_location", "i"),
    ("android_permission_bluetooth", "i"),
    ("android_permission_camera", "i"),
    ("com_android_vending_check_license", "i"),
    ("android_permission_foreground_service", "i"),
    ("android_permission_bluetooth_admin", "i"),
    ("android_permission_vibrate", "i"),
    ("android_permission_nfc", "i"),
    ("android_permission_receive_user_present", "i"),
    ("android_permission_clear_app_cache", "i"),
    ("com_android_launcher_permission_uninstall_shortcut", "i"),
    ("com_sec_android_iap_permission_billing", "i"),
    ("com_htc_launcher_permission_update_shortcut", "i"),
    ("com_sec_android_provider_badge_permission_write", "i"),
    ("android_permission_access_network_state", "i"),
    ("com_google_android_finsky_permission_bind_get_install_referrer_service", "i"),
    ("com_huawei_android_launcher_permission_read_settings", "i"),
    ("android_permission_read_sms", "i"),
    ("android_permission_process_incoming_calls", "i"),
    ("result", "label"),
)

_ANNEALING_FIELDS: tuple[tuple[str, str], ...] = (
    ("famiily", "famiily"),
    ("product_type", "product_type"),
    ("steel", "steel"),
    ("carbon", "i"),
    ("hardness", "i"),
    ("temper_rolling", "temper_rolling"),
    ("condition", "condition"),
    ("formability", "i"),
    ("strength", "i"),
    ("non_ageing", "non_ageing"),
    ("surface_finish", "surface_finish"),
    ("surface_quality", "surface_quality"),
    ("enamelability", "i"),
    ("bc", "bc"),
    ("bf", "bc"),
    ("bt", "bc"),
    ("bw_me", "bw_me"),
    ("bl", "bc"),
    ("m", "i"),
    ("chrom", "product_type"),
    ("phos", "surface_finish"),
    ("cbond", "bc"),
    ("marvi", "i"),
    ("exptl", "bc"),
    ("ferro", "bc"),
    ("corr", "i"),
    ("blue_bright_varn_clean", "blue_bright_varn_clean"),
    ("lustre", "bc"),
    ("jurofm", "i"),
    ("s", "i"),
    ("p", "i"),
    ("shape", "shape"),
    ("thick", "d"),
    ("width", "d"),
    ("len", "i"),
    ("oil", "oil"),
    ("bore", "i"),
    ("packing", "i"),
    ("class", "label"),
)

_APPLIANCES_ENERGY_FIELDS: tuple[tuple[str, str], ...] = (
    ("date", "text"),
    ("appliances", "target"),
    ("lights", "i"),
    ("t1", "d"),
    ("rh_1", "d"),
    ("t2", "d"),
    ("rh_2", "d"),
    ("t3", "d"),
    ("rh_3", "d"),
    ("t4", "d"),
    ("rh_4", "d"),
    ("t5", "d"),
    ("rh_5", "d"),
    ("t6", "d"),
    ("rh_6", "d"),
    ("t7", "d"),
    ("rh_7", "d"),
    ("t8", "d"),
    ("rh_8", "d"),
    ("t9", "d"),
    ("rh_9", "d"),
    ("t_out", "d"),
    ("press_mm_hg", "d"),
    ("rh_out", "d"),
    ("windspeed", "d"),
    ("visibility", "d"),
    ("tdewpoint", "d"),
    ("rv1", "d"),
    ("rv2", "d"),
)

_AUCTION_VERIFICATION_FIELDS: tuple[tuple[str, str], ...] = (
    ("process_b1_capacity", "i"),
    ("process_b2_capacity", "i"),
    ("process_b3_capacity", "i"),
    ("process_b4_capacity", "i"),
    ("property_price", "i"),
    ("property_product", "i"),
    ("property_winner", "i"),
    ("verification_result", "label"),
    ("verification_time", "d"),
)

_AUDIOLOGY_FIELDS: tuple[tuple[str, str], ...] = (
    ("age_gt_60", "age_gt_60"),
    ("air", "air"),
    ("airbonegap", "age_gt_60"),
    ("ar_c", "ar_c"),
    ("ar_u", "ar_c"),
    ("bone", "bone"),
    ("boneabnormal", "age_gt_60"),
    ("bser", "bser"),
    ("history_buzzing", "age_gt_60"),
    ("history_dizziness", "age_gt_60"),
    ("history_fluctuating", "age_gt_60"),
    ("history_fullness", "age_gt_60"),
    ("history_heredity", "age_gt_60"),
    ("history_nausea", "age_gt_60"),
    ("history_noise", "age_gt_60"),
    ("history_recruitment", "age_gt_60"),
    ("history_ringing", "age_gt_60"),
    ("history_roaring", "age_gt_60"),
    ("history_vomiting", "age_gt_60"),
    ("late_wave_poor", "age_gt_60"),
    ("m_at_2k", "age_gt_60"),
    ("m_cond_lt_1k", "m_cond_lt_1k"),
    ("m_gt_1k", "age_gt_60"),
    ("m_m_gt_2k", "age_gt_60"),
    ("m_m_sn", "age_gt_60"),
    ("m_m_sn_gt_1k", "age_gt_60"),
    ("m_m_sn_gt_2k", "age_gt_60"),
    ("m_m_sn_gt_500", "age_gt_60"),
    ("m_p_sn_gt_2k", "m_cond_lt_1k"),
    ("m_s_gt_500", "age_gt_60"),
    ("m_s_sn", "age_gt_60"),
    ("m_s_sn_gt_1k", "m_cond_lt_1k"),
    ("m_s_sn_gt_2k", "age_gt_60"),
    ("m_s_sn_3k", "age_gt_60"),
    ("m_s_sn_4k", "age_gt_60"),
    ("m_sn_2_3k", "age_gt_60"),
    ("m_sn_gt_1k", "age_gt_60"),
    ("m_sn_gt_2k", "age_gt_60"),
    ("m_sn_gt_3k", "age_gt_60"),
    ("m_sn_gt_4k", "age_gt_60"),
    ("m_sn_gt_500", "age_gt_60"),
    ("m_sn_gt_6k", "m_cond_lt_1k"),
    ("m_sn_lt_1k", "age_gt_60"),
    ("m_sn_lt_2k", "age_gt_60"),
    ("m_sn_lt_3k", "age_gt_60"),
    ("middle_wave_poor", "age_gt_60"),
    ("mod_gt_4k", "age_gt_60"),
    ("mod_mixed", "m_cond_lt_1k"),
    ("mod_s_mixed", "m_cond_lt_1k"),
    ("mod_s_sn_gt_500", "age_gt_60"),
    ("mod_sn", "m_cond_lt_1k"),
    ("mod_sn_gt_1k", "age_gt_60"),
    ("mod_sn_gt_2k", "age_gt_60"),
    ("mod_sn_gt_3k", "age_gt_60"),
    ("mod_sn_gt_4k", "age_gt_60"),
    ("mod_sn_gt_500", "age_gt_60"),
    ("notch_4k", "age_gt_60"),
    ("notch_at_4k", "age_gt_60"),
    ("o_ar_c", "ar_c"),
    ("o_ar_u", "ar_c"),
    ("s_sn_gt_1k", "age_gt_60"),
    ("s_sn_gt_2k", "age_gt_60"),
    ("s_sn_gt_4k", "age_gt_60"),
    ("speech", "speech"),
    ("static_normal", "age_gt_60"),
    ("tymp", "tymp"),
    ("viith_nerve_signs", "age_gt_60"),
    ("wave_v_delayed", "age_gt_60"),
    ("waveform_itov_prolonged", "age_gt_60"),
    ("identifier", "text"),
    ("class", "label"),
)

_AUTISM_SCREENING_ADULT_FIELDS: tuple[tuple[str, str], ...] = (
    ("a1_score", "i"),
    ("a2_score", "i"),
    ("a3_score", "i"),
    ("a4_score", "i"),
    ("a5_score", "i"),
    ("a6_score", "i"),
    ("a7_score", "i"),
    ("a8_score", "i"),
    ("a9_score", "i"),
    ("a10_score", "i"),
    ("age", "i"),
    ("gender", "gender"),
    ("ethnicity", "ethnicity"),
    ("jaundice", "jaundice"),
    ("family_pdd", "jaundice"),
    ("country_of_res", "text"),
    ("used_app_before", "jaundice"),
    ("result", "i"),
    ("age_desc", "age_desc"),
    ("relation", "relation"),
    ("class", "label"),
)

_AUTISM_SCREENING_CHILD_FIELDS: tuple[tuple[str, str], ...] = (
    ("a1_score", "i"),
    ("a2_score", "i"),
    ("a3_score", "i"),
    ("a4_score", "i"),
    ("a5_score", "i"),
    ("a6_score", "i"),
    ("a7_score", "i"),
    ("a8_score", "i"),
    ("a9_score", "i"),
    ("a10_score", "i"),
    ("age", "i"),
    ("gender", "gender"),
    ("ethnicity", "ethnicity"),
    ("jaundice", "jaundice"),
    ("autism", "jaundice"),
    ("country_of_res", "text"),
    ("used_app_before", "jaundice"),
    ("result", "i"),
    ("age_desc", "age_desc"),
    ("relation", "relation"),
    ("class", "label"),
)

_BEIJING_PM25_FIELDS: tuple[tuple[str, str], ...] = (
    ("no", "i"),
    ("year", "i"),
    ("month", "i"),
    ("day", "i"),
    ("hour", "i"),
    ("pm2_5", "target"),
    ("dewp", "i"),
    ("temp", "d"),
    ("pres", "d"),
    ("cbwd", "cbwd"),
    ("iws", "d"),
    ("is", "i"),
    ("ir", "i"),
)

_BONE_MARROW_TRANSPLANT_FIELDS: tuple[tuple[str, str], ...] = (
    ("recipientgender", "i"),
    ("stemcellsource", "i"),
    ("donorage", "d"),
    ("donorage35", "i"),
    ("iiiv", "i"),
    ("gendermatch", "i"),
    ("donorabo", "i"),
    ("recipientabo", "i"),
    ("recipientrh", "i"),
    ("abomatch", "i"),
    ("cmvstatus", "i"),
    ("donorcmv", "i"),
    ("recipientcmv", "i"),
    ("disease", "disease"),
    ("riskgroup", "i"),
    ("txpostrelapse", "i"),
    ("diseasegroup", "i"),
    ("hlamatch", "i"),
    ("hlamismatch", "i"),
    ("antigen", "i"),
    ("allele", "i"),
    ("hlagri", "i"),
    ("recipientage", "d"),
    ("recipientage10", "i"),
    ("recipientageint", "i"),
    ("relapse", "i"),
    ("agvhdiiiiv", "i"),
    ("extcgvhd", "i"),
    ("cd34kgx10d6", "d"),
    ("cd3dcd34", "d"),
    ("cd3dkgx10d8", "d"),
    ("rbodymass", "d"),
    ("ancrecovery", "i"),
    ("pltrecovery", "i"),
    ("time_to_agvhd_iii_iv", "i"),
    ("survival_time", "i"),
    ("survival_status", "label"),
)

_BREAST_CANCER_COIMBRA_FIELDS: tuple[tuple[str, str], ...] = (
    ("age", "i"),
    ("bmi", "d"),
    ("glucose", "i"),
    ("insulin", "d"),
    ("homa", "d"),
    ("leptin", "d"),
    ("adiponectin", "d"),
    ("resistin", "d"),
    ("mcp_1", "d"),
    ("classification", "label"),
)

_BREAST_CANCER_ORIGINAL_FIELDS: tuple[tuple[str, str], ...] = (
    ("sample_code_number", "i"),
    ("clump_thickness", "i"),
    ("uniformity_of_cell_size", "i"),
    ("uniformity_of_cell_shape", "i"),
    ("marginal_adhesion", "i"),
    ("single_epithelial_cell_size", "i"),
    ("bare_nuclei", "i"),
    ("bland_chromatin", "i"),
    ("normal_nucleoli", "i"),
    ("mitoses", "i"),
    ("class", "label"),
)

_BREAST_CANCER_PROGNOSTIC_FIELDS: tuple[tuple[str, str], ...] = (
    ("id", "i"),
    ("time", "i"),
    ("radius1", "d"),
    ("texture1", "d"),
    ("perimeter1", "d"),
    ("area1", "d"),
    ("smoothness1", "d"),
    ("compactness1", "d"),
    ("concavity1", "d"),
    ("concave_points1", "d"),
    ("symmetry1", "d"),
    ("fractal_dimension1", "d"),
    ("radius2", "d"),
    ("texture2", "d"),
    ("perimeter2", "d"),
    ("area2", "d"),
    ("smoothness2", "d"),
    ("compactness2", "d"),
    ("concavity2", "d"),
    ("concave_points2", "d"),
    ("symmetry2", "d"),
    ("fractal_dimension2", "d"),
    ("radius3", "d"),
    ("texture3", "d"),
    ("perimeter3", "d"),
    ("area3", "d"),
    ("smoothness3", "d"),
    ("compactness3", "d"),
    ("concavity3", "d"),
    ("concave_points3", "d"),
    ("symmetry3", "d"),
    ("fractal_dimension3", "d"),
    ("tumor_size", "d"),
    ("lymph_node_status", "i"),
    ("outcome", "label"),
)

_BREAST_CANCER_RECURRENCE_FIELDS: tuple[tuple[str, str], ...] = (
    ("age", "age"),
    ("menopause", "menopause"),
    ("tumor_size", "tumor_size"),
    ("inv_nodes", "inv_nodes"),
    ("node_caps", "node_caps"),
    ("deg_malig", "i"),
    ("breast", "breast"),
    ("breast_quad", "breast_quad"),
    ("irradiat", "node_caps"),
    ("class", "label"),
)

_CARDIOTOCOGRAPHY_FIELDS: tuple[tuple[str, str], ...] = (
    ("lb", "i"),
    ("ac", "d"),
    ("fm", "d"),
    ("uc", "d"),
    ("dl", "d"),
    ("ds", "d"),
    ("dp", "d"),
    ("astv", "i"),
    ("mstv", "d"),
    ("altv", "i"),
    ("mltv", "d"),
    ("width", "i"),
    ("min", "i"),
    ("max", "i"),
    ("nmax", "i"),
    ("nzeros", "i"),
    ("mode", "i"),
    ("mean", "i"),
    ("median", "i"),
    ("variance", "i"),
    ("tendency", "i"),
    ("class", "i"),
    ("nsp", "label"),
)

_CDC_DIABETES_FIELDS: tuple[tuple[str, str], ...] = (
    ("id", "i"),
    ("diabetes_binary", "label"),
    ("highbp", "i"),
    ("highchol", "i"),
    ("cholcheck", "i"),
    ("bmi", "i"),
    ("smoker", "i"),
    ("stroke", "i"),
    ("heartdiseaseorattack", "i"),
    ("physactivity", "i"),
    ("fruits", "i"),
    ("veggies", "i"),
    ("hvyalcoholconsump", "i"),
    ("anyhealthcare", "i"),
    ("nodocbccost", "i"),
    ("genhlth", "i"),
    ("menthlth", "i"),
    ("physhlth", "i"),
    ("diffwalk", "i"),
    ("sex", "i"),
    ("age", "i"),
    ("education", "i"),
    ("income", "i"),
)

_CENSUS_INCOME_KDD_FIELDS: tuple[tuple[str, str], ...] = (
    ("aage", "i"),
    ("aclswkr", "aclswkr"),
    ("adtink", "i"),
    ("adtocc", "i"),
    ("ahga", "text"),
    ("ahrspay", "i"),
    ("ahscol", "ahscol"),
    ("amaritl", "amaritl"),
    ("amjind", "text"),
    ("amjocc", "text"),
    ("arace", "arace"),
    ("areorgn", "areorgn"),
    ("asex", "asex"),
    ("aunmem", "aunmem"),
    ("auntype", "auntype"),
    ("awkstat", "text"),
    ("capgain", "i"),
    ("gaploss", "i"),
    ("divval", "i"),
    ("filestat", "filestat"),
    ("grinreg", "grinreg"),
    ("grinst", "text"),
    ("hhdfmx", "text"),
    ("hhdrel", "text"),
    ("marsupwrt", "d"),
    ("migmtr1", "migmtr1"),
    ("migmtr3", "migmtr3"),
    ("migmtr4", "migmtr4"),
    ("migsame", "migsame"),
    ("migsun", "aunmem"),
    ("noemp", "i"),
    ("parent", "parent"),
    ("pefntvty", "text"),
    ("pemntvty", "text"),
    ("penatvty", "text"),
    ("prcitshp", "text"),
    ("seotr", "i"),
    ("vetqva", "aunmem"),
    ("vetyn", "i"),
    ("wkswork", "i"),
    ("year", "i"),
    ("income", "label"),
)

_CERVICAL_CANCER_BEHAVIOUR_FIELDS: tuple[tuple[str, str], ...] = (
    ("behavior_sexualrisk", "i"),
    ("behavior_eating", "i"),
    ("behavior_personalhygiene", "i"),
    ("intention_aggregation", "i"),
    ("intention_commitment", "i"),
    ("attitude_consistency", "i"),
    ("attitude_spontaneity", "i"),
    ("norm_significantperson", "i"),
    ("norm_fulfillment", "i"),
    ("perception_vulnerability", "i"),
    ("perception_severity", "i"),
    ("motivation_strength", "i"),
    ("motivation_willingness", "i"),
    ("socialsupport_emotionality", "i"),
    ("socialsupport_appreciation", "i"),
    ("socialsupport_instrumental", "i"),
    ("empowerment_knowledge", "i"),
    ("empowerment_abilities", "i"),
    ("empowerment_desires", "i"),
    ("ca_cervix", "label"),
)

_CERVICAL_CANCER_RISK_FIELDS: tuple[tuple[str, str], ...] = (
    ("age", "i"),
    ("number_of_sexual_partners", "d"),
    ("first_sexual_intercourse", "d"),
    ("num_of_pregnancies", "d"),
    ("smokes", "d"),
    ("smokes_years", "d"),
    ("smokes_packs_year", "d"),
    ("hormonal_contraceptives", "d"),
    ("hormonal_contraceptives_years", "d"),
    ("iud", "d"),
    ("iud_years", "d"),
    ("stds", "d"),
    ("stds_number", "d"),
    ("stds_condylomatosis", "d"),
    ("stds_cervical_condylomatosis", "d"),
    ("stds_vaginal_condylomatosis", "d"),
    ("stds_vulvo_perineal_condylomatosis", "d"),
    ("stds_syphilis", "d"),
    ("stds_pelvic_inflammatory_disease", "d"),
    ("stds_genital_herpes", "d"),
    ("stds_molluscum_contagiosum", "d"),
    ("stds_aids", "d"),
    ("stds_hiv", "d"),
    ("stds_hepatitis_b", "d"),
    ("stds_hpv", "d"),
    ("stds_number_of_diagnosis", "i"),
    ("stds_time_since_first_diagnosis", "d"),
    ("stds_time_since_last_diagnosis", "d"),
    ("dx_cancer", "i"),
    ("dx_cin", "i"),
    ("dx_hpv", "i"),
    ("dx", "i"),
    ("hinselmann", "i"),
    ("schiller", "i"),
    ("citology", "i"),
    ("biopsy", "label"),
)

_CHALLENGER_O_RINGS_FIELDS: tuple[tuple[str, str], ...] = (
    ("num_o_rings", "target"),
    ("num_thermal_distress", "target"),
    ("launch_temp", "i"),
    ("leak_check_pressure", "i"),
    ("temporal_order", "i"),
)

_CHESS_ENDGAME_FIELDS: tuple[tuple[str, str], ...] = (
    ("white_king_file", "white_king_file"),
    ("white_king_rank", "i"),
    ("white_rook_file", "white_rook_file"),
    ("white_rook_rank", "i"),
    ("black_king_file", "white_rook_file"),
    ("black_king_rank", "i"),
    ("white_depth_of_win", "label"),
)

_CHRONIC_KIDNEY_DISEASE_FIELDS: tuple[tuple[str, str], ...] = (
    ("age", "i"),
    ("bp", "i"),
    ("sg", "d"),
    ("al", "i"),
    ("su", "i"),
    ("rbc", "rbc"),
    ("pc", "rbc"),
    ("pcc", "pcc"),
    ("ba", "pcc"),
    ("bgr", "i"),
    ("bu", "d"),
    ("sc", "d"),
    ("sod", "d"),
    ("pot", "d"),
    ("hemo", "d"),
    ("pcv", "i"),
    ("wbcc", "i"),
    ("rbcc", "d"),
    ("htn", "htn"),
    ("dm", "htn"),
    ("cad", "htn"),
    ("appet", "appet"),
    ("pe", "htn"),
    ("ane", "htn"),
    ("class", "label"),
)

_COMMUNITIES_CRIME_FIELDS: tuple[tuple[str, str], ...] = (
    ("state", "i"),
    ("county", "i"),
    ("community", "i"),
    ("communityname", "text"),
    ("fold", "i"),
    ("population", "d"),
    ("householdsize", "d"),
    ("racepctblack", "d"),
    ("racepctwhite", "d"),
    ("racepctasian", "d"),
    ("racepcthisp", "d"),
    ("agepct12t21", "d"),
    ("agepct12t29", "d"),
    ("agepct16t24", "d"),
    ("agepct65up", "d"),
    ("numburban", "d"),
    ("pcturban", "d"),
    ("medincome", "d"),
    ("pctwwage", "d"),
    ("pctwfarmself", "d"),
    ("pctwinvinc", "d"),
    ("pctwsocsec", "d"),
    ("pctwpubasst", "d"),
    ("pctwretire", "d"),
    ("medfaminc", "d"),
    ("percapinc", "d"),
    ("whitepercap", "d"),
    ("blackpercap", "d"),
    ("indianpercap", "d"),
    ("asianpercap", "d"),
    ("otherpercap", "d"),
    ("hisppercap", "d"),
    ("numunderpov", "d"),
    ("pctpopunderpov", "d"),
    ("pctless9thgrade", "d"),
    ("pctnothsgrad", "d"),
    ("pctbsormore", "d"),
    ("pctunemployed", "d"),
    ("pctemploy", "d"),
    ("pctemplmanu", "d"),
    ("pctemplprofserv", "d"),
    ("pctoccupmanu", "d"),
    ("pctoccupmgmtprof", "d"),
    ("malepctdivorce", "d"),
    ("malepctnevmarr", "d"),
    ("femalepctdiv", "d"),
    ("totalpctdiv", "d"),
    ("persperfam", "d"),
    ("pctfam2par", "d"),
    ("pctkids2par", "d"),
    ("pctyoungkids2par", "d"),
    ("pctteen2par", "d"),
    ("pctworkmomyoungkids", "d"),
    ("pctworkmom", "d"),
    ("numilleg", "d"),
    ("pctilleg", "d"),
    ("numimmig", "d"),
    ("pctimmigrecent", "d"),
    ("pctimmigrec5", "d"),
    ("pctimmigrec8", "d"),
    ("pctimmigrec10", "d"),
    ("pctrecentimmig", "d"),
    ("pctrecimmig5", "d"),
    ("pctrecimmig8", "d"),
    ("pctrecimmig10", "d"),
    ("pctspeakenglonly", "d"),
    ("pctnotspeakenglwell", "d"),
    ("pctlarghousefam", "d"),
    ("pctlarghouseoccup", "d"),
    ("persperoccuphous", "d"),
    ("persperownocchous", "d"),
    ("persperrentocchous", "d"),
    ("pctpersownoccup", "d"),
    ("pctpersdensehous", "d"),
    ("pcthousless3br", "d"),
    ("mednumbr", "d"),
    ("housvacant", "d"),
    ("pcthousoccup", "d"),
    ("pcthousownocc", "d"),
    ("pctvacantboarded", "d"),
    ("pctvacmore6mos", "d"),
    ("medyrhousbuilt", "d"),
    ("pcthousnophone", "d"),
    ("pctwofullplumb", "d"),
    ("ownocclowquart", "d"),
    ("ownoccmedval", "d"),
    ("ownocchiquart", "d"),
    ("rentlowq", "d"),
    ("rentmedian", "d"),
    ("renthighq", "d"),
    ("medrent", "d"),
    ("medrentpcthousinc", "d"),
    ("medowncostpctinc", "d"),
    ("medowncostpctincnomtg", "d"),
    ("numinshelters", "d"),
    ("numstreet", "d"),
    ("pctforeignborn", "d"),
    ("pctbornsamestate", "d"),
    ("pctsamehouse85", "d"),
    ("pctsamecity85", "d"),
    ("pctsamestate85", "d"),
    ("lemasswornft", "d"),
    ("lemasswftperpop", "d"),
    ("lemasswftfieldops", "d"),
    ("lemasswftfieldperpop", "d"),
    ("lemastotalreq", "d"),
    ("lemastotreqperpop", "d"),
    ("policreqperoffic", "d"),
    ("policperpop", "d"),
    ("racialmatchcommpol", "d"),
    ("pctpolicwhite", "d"),
    ("pctpolicblack", "d"),
    ("pctpolichisp", "d"),
    ("pctpolicasian", "d"),
    ("pctpolicminor", "d"),
    ("officassgndrugunits", "d"),
    ("numkindsdrugsseiz", "d"),
    ("policaveotworked", "d"),
    ("landarea", "d"),
    ("popdens", "d"),
    ("pctusepubtrans", "d"),
    ("policcars", "d"),
    ("policoperbudg", "d"),
    ("lemaspctpoliconpatr", "d"),
    ("lemasgangunitdeploy", "d"),
    ("lemaspctofficdrugun", "d"),
    ("policbudgperpop", "d"),
    ("violentcrimesperpop", "target"),
)

_CONCRETE_STRENGTH_FIELDS: tuple[tuple[str, str], ...] = (
    ("cement", "d"),
    ("blast_furnace_slag", "d"),
    ("fly_ash", "d"),
    ("water", "d"),
    ("superplasticizer", "d"),
    ("coarse_aggregate", "d"),
    ("fine_aggregate", "d"),
    ("age", "i"),
    ("concrete_compressive_strength", "target"),
)

_CONGRESSIONAL_VOTING_FIELDS: tuple[tuple[str, str], ...] = (
    ("class", "label"),
    ("handicapped_infants", "handicapped_infants"),
    ("water_project_cost_sharing", "handicapped_infants"),
    ("adoption_of_the_budget_resolution", "handicapped_infants"),
    ("physician_fee_freeze", "handicapped_infants"),
    ("el_salvador_aid", "handicapped_infants"),
    ("religious_groups_in_schools", "handicapped_infants"),
    ("anti_satellite_test_ban", "handicapped_infants"),
    ("aid_to_nicaraguan_contras", "handicapped_infants"),
    ("mx_missile", "handicapped_infants"),
    ("immigration", "handicapped_infants"),
    ("synfuels_corporation_cutback", "handicapped_infants"),
    ("education_spending", "handicapped_infants"),
    ("superfund_right_to_sue", "handicapped_infants"),
    ("crime", "handicapped_infants"),
    ("duty_free_exports", "handicapped_infants"),
    ("export_administration_act_south_africa", "handicapped_infants"),
)

_CONNECT_FOUR_FIELDS: tuple[tuple[str, str], ...] = (
    ("a1", "a1"),
    ("a2", "a1"),
    ("a3", "a1"),
    ("a4", "a1"),
    ("a5", "a1"),
    ("a6", "a1"),
    ("b1", "a1"),
    ("b2", "a1"),
    ("b3", "a1"),
    ("b4", "a1"),
    ("b5", "a1"),
    ("b6", "a1"),
    ("c1", "a1"),
    ("c2", "a1"),
    ("c3", "a1"),
    ("c4", "a1"),
    ("c5", "a1"),
    ("c6", "a1"),
    ("d1", "a1"),
    ("d2", "a1"),
    ("d3", "a1"),
    ("d4", "a1"),
    ("d5", "a1"),
    ("d6", "a1"),
    ("e1", "a1"),
    ("e2", "a1"),
    ("e3", "a1"),
    ("e4", "a1"),
    ("e5", "a1"),
    ("e6", "a1"),
    ("f1", "a1"),
    ("f2", "a1"),
    ("f3", "a1"),
    ("f4", "a1"),
    ("f5", "a1"),
    ("f6", "a1"),
    ("g1", "a1"),
    ("g2", "a1"),
    ("g3", "a1"),
    ("g4", "a1"),
    ("g5", "a1"),
    ("g6", "a1"),
    ("class", "label"),
)

_CREDIT_CARD_DEFAULT_FIELDS: tuple[tuple[str, str], ...] = (
    ("id", "i"),
    ("x1", "i"),
    ("x2", "i"),
    ("x3", "i"),
    ("x4", "i"),
    ("x5", "i"),
    ("x6", "i"),
    ("x7", "i"),
    ("x8", "i"),
    ("x9", "i"),
    ("x10", "i"),
    ("x11", "i"),
    ("x12", "i"),
    ("x13", "i"),
    ("x14", "i"),
    ("x15", "i"),
    ("x16", "i"),
    ("x17", "i"),
    ("x18", "i"),
    ("x19", "i"),
    ("x20", "i"),
    ("x21", "i"),
    ("x22", "i"),
    ("x23", "i"),
    ("y", "label"),
)

_CREDIT_SCREENING_FIELDS: tuple[tuple[str, str], ...] = (
    ("a1", "a1"),
    ("a2", "d"),
    ("a3", "d"),
    ("a4", "a4"),
    ("a5", "a5"),
    ("a6", "a6"),
    ("a7", "a7"),
    ("a8", "d"),
    ("a9", "a9"),
    ("a10", "a9"),
    ("a11", "i"),
    ("a12", "a9"),
    ("a13", "a13"),
    ("a14", "i"),
    ("a15", "i"),
    ("a16", "label"),
)

_DAILY_DEMAND_FIELDS: tuple[tuple[str, str], ...] = (
    ("week_of_the_month", "i"),
    ("day_of_the_week", "i"),
    ("non_urgent_order", "d"),
    ("urgent_order", "d"),
    ("order_type_a", "d"),
    ("order_type_b", "d"),
    ("order_type_c", "d"),
    ("fiscal_sector_orders", "d"),
    ("orders_from_the_traffic_controller_sector", "i"),
    ("banking_orders_1", "i"),
    ("banking_orders_2", "i"),
    ("banking_orders_3", "i"),
    ("total_orders", "target"),
)

_DARWIN_FIELDS: tuple[tuple[str, str], ...] = (
    ("id", "text"),
    ("air_time1", "i"),
    ("disp_index1", "d"),
    ("gmrt_in_air1", "d"),
    ("gmrt_on_paper1", "d"),
    ("max_x_extension1", "i"),
    ("max_y_extension1", "i"),
    ("mean_acc_in_air1", "d"),
    ("mean_acc_on_paper1", "d"),
    ("mean_gmrt1", "d"),
    ("mean_jerk_in_air1", "d"),
    ("mean_jerk_on_paper1", "d"),
    ("mean_speed_in_air1", "d"),
    ("mean_speed_on_paper1", "d"),
    ("num_of_pendown1", "i"),
    ("paper_time1", "i"),
    ("pressure_mean1", "d"),
    ("pressure_var1", "d"),
    ("total_time1", "i"),
    ("air_time2", "i"),
    ("disp_index2", "d"),
    ("gmrt_in_air2", "d"),
    ("gmrt_on_paper2", "d"),
    ("max_x_extension2", "i"),
    ("max_y_extension2", "i"),
    ("mean_acc_in_air2", "d"),
    ("mean_acc_on_paper2", "d"),
    ("mean_gmrt2", "d"),
    ("mean_jerk_in_air2", "d"),
    ("mean_jerk_on_paper2", "d"),
    ("mean_speed_in_air2", "d"),
    ("mean_speed_on_paper2", "d"),
    ("num_of_pendown2", "i"),
    ("paper_time2", "i"),
    ("pressure_mean2", "d"),
    ("pressure_var2", "d"),
    ("total_time2", "i"),
    ("air_time3", "i"),
    ("disp_index3", "d"),
    ("gmrt_in_air3", "d"),
    ("gmrt_on_paper3", "d"),
    ("max_x_extension3", "i"),
    ("max_y_extension3", "i"),
    ("mean_acc_in_air3", "d"),
    ("mean_acc_on_paper3", "d"),
    ("mean_gmrt3", "d"),
    ("mean_jerk_in_air3", "d"),
    ("mean_jerk_on_paper3", "d"),
    ("mean_speed_in_air3", "d"),
    ("mean_speed_on_paper3", "d"),
    ("num_of_pendown3", "i"),
    ("paper_time3", "i"),
    ("pressure_mean3", "d"),
    ("pressure_var3", "d"),
    ("total_time3", "i"),
    ("air_time4", "i"),
    ("disp_index4", "d"),
    ("gmrt_in_air4", "d"),
    ("gmrt_on_paper4", "d"),
    ("max_x_extension4", "i"),
    ("max_y_extension4", "i"),
    ("mean_acc_in_air4", "d"),
    ("mean_acc_on_paper4", "d"),
    ("mean_gmrt4", "d"),
    ("mean_jerk_in_air4", "d"),
    ("mean_jerk_on_paper4", "d"),
    ("mean_speed_in_air4", "d"),
    ("mean_speed_on_paper4", "d"),
    ("num_of_pendown4", "i"),
    ("paper_time4", "i"),
    ("pressure_mean4", "d"),
    ("pressure_var4", "d"),
    ("total_time4", "i"),
    ("air_time5", "i"),
    ("disp_index5", "d"),
    ("gmrt_in_air5", "d"),
    ("gmrt_on_paper5", "d"),
    ("max_x_extension5", "i"),
    ("max_y_extension5", "i"),
    ("mean_acc_in_air5", "d"),
    ("mean_acc_on_paper5", "d"),
    ("mean_gmrt5", "d"),
    ("mean_jerk_in_air5", "d"),
    ("mean_jerk_on_paper5", "d"),
    ("mean_speed_in_air5", "d"),
    ("mean_speed_on_paper5", "d"),
    ("num_of_pendown5", "i"),
    ("paper_time5", "i"),
    ("pressure_mean5", "d"),
    ("pressure_var5", "d"),
    ("total_time5", "i"),
    ("air_time6", "i"),
    ("disp_index6", "d"),
    ("gmrt_in_air6", "d"),
    ("gmrt_on_paper6", "d"),
    ("max_x_extension6", "i"),
    ("max_y_extension6", "i"),
    ("mean_acc_in_air6", "d"),
    ("mean_acc_on_paper6", "d"),
    ("mean_gmrt6", "d"),
    ("mean_jerk_in_air6", "d"),
    ("mean_jerk_on_paper6", "d"),
    ("mean_speed_in_air6", "d"),
    ("mean_speed_on_paper6", "d"),
    ("num_of_pendown6", "i"),
    ("paper_time6", "i"),
    ("pressure_mean6", "d"),
    ("pressure_var6", "d"),
    ("total_time6", "i"),
    ("air_time7", "i"),
    ("disp_index7", "d"),
    ("gmrt_in_air7", "d"),
    ("gmrt_on_paper7", "d"),
    ("max_x_extension7", "i"),
    ("max_y_extension7", "i"),
    ("mean_acc_in_air7", "d"),
    ("mean_acc_on_paper7", "d"),
    ("mean_gmrt7", "d"),
    ("mean_jerk_in_air7", "d"),
    ("mean_jerk_on_paper7", "d"),
    ("mean_speed_in_air7", "d"),
    ("mean_speed_on_paper7", "d"),
    ("num_of_pendown7", "i"),
    ("paper_time7", "i"),
    ("pressure_mean7", "d"),
    ("pressure_var7", "d"),
    ("total_time7", "i"),
    ("air_time8", "i"),
    ("disp_index8", "d"),
    ("gmrt_in_air8", "d"),
    ("gmrt_on_paper8", "d"),
    ("max_x_extension8", "i"),
    ("max_y_extension8", "i"),
    ("mean_acc_in_air8", "d"),
    ("mean_acc_on_paper8", "d"),
    ("mean_gmrt8", "d"),
    ("mean_jerk_in_air8", "d"),
    ("mean_jerk_on_paper8", "d"),
    ("mean_speed_in_air8", "d"),
    ("mean_speed_on_paper8", "d"),
    ("num_of_pendown8", "i"),
    ("paper_time8", "i"),
    ("pressure_mean8", "d"),
    ("pressure_var8", "d"),
    ("total_time8", "i"),
    ("air_time9", "i"),
    ("disp_index9", "d"),
    ("gmrt_in_air9", "d"),
    ("gmrt_on_paper9", "d"),
    ("max_x_extension9", "i"),
    ("max_y_extension9", "i"),
    ("mean_acc_in_air9", "d"),
    ("mean_acc_on_paper9", "d"),
    ("mean_gmrt9", "d"),
    ("mean_jerk_in_air9", "d"),
    ("mean_jerk_on_paper9", "d"),
    ("mean_speed_in_air9", "d"),
    ("mean_speed_on_paper9", "d"),
    ("num_of_pendown9", "i"),
    ("paper_time9", "i"),
    ("pressure_mean9", "d"),
    ("pressure_var9", "d"),
    ("total_time9", "i"),
    ("air_time10", "i"),
    ("disp_index10", "d"),
    ("gmrt_in_air10", "d"),
    ("gmrt_on_paper10", "d"),
    ("max_x_extension10", "i"),
    ("max_y_extension10", "i"),
    ("mean_acc_in_air10", "d"),
    ("mean_acc_on_paper10", "d"),
    ("mean_gmrt10", "d"),
    ("mean_jerk_in_air10", "d"),
    ("mean_jerk_on_paper10", "d"),
    ("mean_speed_in_air10", "d"),
    ("mean_speed_on_paper10", "d"),
    ("num_of_pendown10", "i"),
    ("paper_time10", "i"),
    ("pressure_mean10", "d"),
    ("pressure_var10", "d"),
    ("total_time10", "i"),
    ("air_time11", "i"),
    ("disp_index11", "d"),
    ("gmrt_in_air11", "d"),
    ("gmrt_on_paper11", "d"),
    ("max_x_extension11", "i"),
    ("max_y_extension11", "i"),
    ("mean_acc_in_air11", "d"),
    ("mean_acc_on_paper11", "d"),
    ("mean_gmrt11", "d"),
    ("mean_jerk_in_air11", "d"),
    ("mean_jerk_on_paper11", "d"),
    ("mean_speed_in_air11", "d"),
    ("mean_speed_on_paper11", "d"),
    ("num_of_pendown11", "i"),
    ("paper_time11", "i"),
    ("pressure_mean11", "d"),
    ("pressure_var11", "d"),
    ("total_time11", "i"),
    ("air_time12", "i"),
    ("disp_index12", "d"),
    ("gmrt_in_air12", "d"),
    ("gmrt_on_paper12", "d"),
    ("max_x_extension12", "i"),
    ("max_y_extension12", "i"),
    ("mean_acc_in_air12", "d"),
    ("mean_acc_on_paper12", "d"),
    ("mean_gmrt12", "d"),
    ("mean_jerk_in_air12", "d"),
    ("mean_jerk_on_paper12", "d"),
    ("mean_speed_in_air12", "d"),
    ("mean_speed_on_paper12", "d"),
    ("num_of_pendown12", "i"),
    ("paper_time12", "i"),
    ("pressure_mean12", "d"),
    ("pressure_var12", "d"),
    ("total_time12", "i"),
    ("air_time13", "i"),
    ("disp_index13", "d"),
    ("gmrt_in_air13", "d"),
    ("gmrt_on_paper13", "d"),
    ("max_x_extension13", "i"),
    ("max_y_extension13", "i"),
    ("mean_acc_in_air13", "d"),
    ("mean_acc_on_paper13", "d"),
    ("mean_gmrt13", "d"),
    ("mean_jerk_in_air13", "d"),
    ("mean_jerk_on_paper13", "d"),
    ("mean_speed_in_air13", "d"),
    ("mean_speed_on_paper13", "d"),
    ("num_of_pendown13", "i"),
    ("paper_time13", "i"),
    ("pressure_mean13", "d"),
    ("pressure_var13", "d"),
    ("total_time13", "i"),
    ("air_time14", "i"),
    ("disp_index14", "d"),
    ("gmrt_in_air14", "d"),
    ("gmrt_on_paper14", "d"),
    ("max_x_extension14", "i"),
    ("max_y_extension14", "i"),
    ("mean_acc_in_air14", "d"),
    ("mean_acc_on_paper14", "d"),
    ("mean_gmrt14", "d"),
    ("mean_jerk_in_air14", "d"),
    ("mean_jerk_on_paper14", "d"),
    ("mean_speed_in_air14", "d"),
    ("mean_speed_on_paper14", "d"),
    ("num_of_pendown14", "i"),
    ("paper_time14", "i"),
    ("pressure_mean14", "d"),
    ("pressure_var14", "d"),
    ("total_time14", "i"),
    ("air_time15", "i"),
    ("disp_index15", "d"),
    ("gmrt_in_air15", "d"),
    ("gmrt_on_paper15", "d"),
    ("max_x_extension15", "i"),
    ("max_y_extension15", "i"),
    ("mean_acc_in_air15", "d"),
    ("mean_acc_on_paper15", "d"),
    ("mean_gmrt15", "d"),
    ("mean_jerk_in_air15", "d"),
    ("mean_jerk_on_paper15", "d"),
    ("mean_speed_in_air15", "d"),
    ("mean_speed_on_paper15", "d"),
    ("num_of_pendown15", "i"),
    ("paper_time15", "i"),
    ("pressure_mean15", "d"),
    ("pressure_var15", "d"),
    ("total_time15", "i"),
    ("air_time16", "i"),
    ("disp_index16", "d"),
    ("gmrt_in_air16", "d"),
    ("gmrt_on_paper16", "d"),
    ("max_x_extension16", "i"),
    ("max_y_extension16", "i"),
    ("mean_acc_in_air16", "d"),
    ("mean_acc_on_paper16", "d"),
    ("mean_gmrt16", "d"),
    ("mean_jerk_in_air16", "d"),
    ("mean_jerk_on_paper16", "d"),
    ("mean_speed_in_air16", "d"),
    ("mean_speed_on_paper16", "d"),
    ("num_of_pendown16", "i"),
    ("paper_time16", "i"),
    ("pressure_mean16", "d"),
    ("pressure_var16", "d"),
    ("total_time16", "i"),
    ("air_time17", "i"),
    ("disp_index17", "d"),
    ("gmrt_in_air17", "d"),
    ("gmrt_on_paper17", "d"),
    ("max_x_extension17", "i"),
    ("max_y_extension17", "i"),
    ("mean_acc_in_air17", "d"),
    ("mean_acc_on_paper17", "d"),
    ("mean_gmrt17", "d"),
    ("mean_jerk_in_air17", "d"),
    ("mean_jerk_on_paper17", "d"),
    ("mean_speed_in_air17", "d"),
    ("mean_speed_on_paper17", "d"),
    ("num_of_pendown17", "i"),
    ("paper_time17", "i"),
    ("pressure_mean17", "d"),
    ("pressure_var17", "d"),
    ("total_time17", "i"),
    ("air_time18", "i"),
    ("disp_index18", "d"),
    ("gmrt_in_air18", "d"),
    ("gmrt_on_paper18", "d"),
    ("max_x_extension18", "i"),
    ("max_y_extension18", "i"),
    ("mean_acc_in_air18", "d"),
    ("mean_acc_on_paper18", "d"),
    ("mean_gmrt18", "d"),
    ("mean_jerk_in_air18", "d"),
    ("mean_jerk_on_paper18", "d"),
    ("mean_speed_in_air18", "d"),
    ("mean_speed_on_paper18", "d"),
    ("num_of_pendown18", "i"),
    ("paper_time18", "i"),
    ("pressure_mean18", "d"),
    ("pressure_var18", "d"),
    ("total_time18", "i"),
    ("air_time19", "i"),
    ("disp_index19", "d"),
    ("gmrt_in_air19", "d"),
    ("gmrt_on_paper19", "d"),
    ("max_x_extension19", "i"),
    ("max_y_extension19", "i"),
    ("mean_acc_in_air19", "d"),
    ("mean_acc_on_paper19", "d"),
    ("mean_gmrt19", "d"),
    ("mean_jerk_in_air19", "d"),
    ("mean_jerk_on_paper19", "d"),
    ("mean_speed_in_air19", "d"),
    ("mean_speed_on_paper19", "d"),
    ("num_of_pendown19", "i"),
    ("paper_time19", "i"),
    ("pressure_mean19", "d"),
    ("pressure_var19", "d"),
    ("total_time19", "i"),
    ("air_time20", "i"),
    ("disp_index20", "d"),
    ("gmrt_in_air20", "d"),
    ("gmrt_on_paper20", "d"),
    ("max_x_extension20", "i"),
    ("max_y_extension20", "i"),
    ("mean_acc_in_air20", "d"),
    ("mean_acc_on_paper20", "d"),
    ("mean_gmrt20", "d"),
    ("mean_jerk_in_air20", "d"),
    ("mean_jerk_on_paper20", "d"),
    ("mean_speed_in_air20", "d"),
    ("mean_speed_on_paper20", "d"),
    ("num_of_pendown20", "i"),
    ("paper_time20", "i"),
    ("pressure_mean20", "d"),
    ("pressure_var20", "d"),
    ("total_time20", "i"),
    ("air_time21", "i"),
    ("disp_index21", "d"),
    ("gmrt_in_air21", "d"),
    ("gmrt_on_paper21", "d"),
    ("max_x_extension21", "i"),
    ("max_y_extension21", "i"),
    ("mean_acc_in_air21", "d"),
    ("mean_acc_on_paper21", "d"),
    ("mean_gmrt21", "d"),
    ("mean_jerk_in_air21", "d"),
    ("mean_jerk_on_paper21", "d"),
    ("mean_speed_in_air21", "d"),
    ("mean_speed_on_paper21", "d"),
    ("num_of_pendown21", "i"),
    ("paper_time21", "i"),
    ("pressure_mean21", "d"),
    ("pressure_var21", "d"),
    ("total_time21", "i"),
    ("air_time22", "i"),
    ("disp_index22", "d"),
    ("gmrt_in_air22", "d"),
    ("gmrt_on_paper22", "d"),
    ("max_x_extension22", "i"),
    ("max_y_extension22", "i"),
    ("mean_acc_in_air22", "d"),
    ("mean_acc_on_paper22", "d"),
    ("mean_gmrt22", "d"),
    ("mean_jerk_in_air22", "d"),
    ("mean_jerk_on_paper22", "d"),
    ("mean_speed_in_air22", "d"),
    ("mean_speed_on_paper22", "d"),
    ("num_of_pendown22", "i"),
    ("paper_time22", "i"),
    ("pressure_mean22", "d"),
    ("pressure_var22", "d"),
    ("total_time22", "i"),
    ("air_time23", "i"),
    ("disp_index23", "d"),
    ("gmrt_in_air23", "d"),
    ("gmrt_on_paper23", "d"),
    ("max_x_extension23", "i"),
    ("max_y_extension23", "i"),
    ("mean_acc_in_air23", "d"),
    ("mean_acc_on_paper23", "d"),
    ("mean_gmrt23", "d"),
    ("mean_jerk_in_air23", "d"),
    ("mean_jerk_on_paper23", "d"),
    ("mean_speed_in_air23", "d"),
    ("mean_speed_on_paper23", "d"),
    ("num_of_pendown23", "i"),
    ("paper_time23", "i"),
    ("pressure_mean23", "d"),
    ("pressure_var23", "d"),
    ("total_time23", "i"),
    ("air_time24", "i"),
    ("disp_index24", "d"),
    ("gmrt_in_air24", "d"),
    ("gmrt_on_paper24", "d"),
    ("max_x_extension24", "i"),
    ("max_y_extension24", "i"),
    ("mean_acc_in_air24", "d"),
    ("mean_acc_on_paper24", "d"),
    ("mean_gmrt24", "d"),
    ("mean_jerk_in_air24", "d"),
    ("mean_jerk_on_paper24", "d"),
    ("mean_speed_in_air24", "d"),
    ("mean_speed_on_paper24", "d"),
    ("num_of_pendown24", "i"),
    ("paper_time24", "i"),
    ("pressure_mean24", "d"),
    ("pressure_var24", "d"),
    ("total_time24", "i"),
    ("air_time25", "i"),
    ("disp_index25", "d"),
    ("gmrt_in_air25", "d"),
    ("gmrt_on_paper25", "d"),
    ("max_x_extension25", "i"),
    ("max_y_extension25", "i"),
    ("mean_acc_in_air25", "d"),
    ("mean_acc_on_paper25", "d"),
    ("mean_gmrt25", "d"),
    ("mean_jerk_in_air25", "d"),
    ("mean_jerk_on_paper25", "d"),
    ("mean_speed_in_air25", "d"),
    ("mean_speed_on_paper25", "d"),
    ("num_of_pendown25", "i"),
    ("paper_time25", "i"),
    ("pressure_mean25", "d"),
    ("pressure_var25", "d"),
    ("total_time25", "i"),
    ("class", "label"),
)

_DIABETES_HOSPITALS_FIELDS: tuple[tuple[str, str], ...] = (
    ("encounter_id", "i"),
    ("patient_nbr", "i"),
    ("race", "race"),
    ("gender", "gender"),
    ("age", "age"),
    ("weight", "weight"),
    ("admission_type_id", "i"),
    ("discharge_disposition_id", "i"),
    ("admission_source_id", "i"),
    ("time_in_hospital", "i"),
    ("payer_code", "payer_code"),
    ("medical_specialty", "text"),
    ("num_lab_procedures", "i"),
    ("num_procedures", "i"),
    ("num_medications", "i"),
    ("number_outpatient", "i"),
    ("number_emergency", "i"),
    ("number_inpatient", "i"),
    ("diag_1", "text"),
    ("diag_2", "text"),
    ("diag_3", "text"),
    ("number_diagnoses", "i"),
    ("max_glu_serum", "max_glu_serum"),
    ("a1cresult", "a1cresult"),
    ("metformin", "metformin"),
    ("repaglinide", "metformin"),
    ("nateglinide", "metformin"),
    ("chlorpropamide", "metformin"),
    ("glimepiride", "metformin"),
    ("acetohexamide", "acetohexamide"),
    ("glipizide", "metformin"),
    ("glyburide", "metformin"),
    ("tolbutamide", "acetohexamide"),
    ("pioglitazone", "metformin"),
    ("rosiglitazone", "metformin"),
    ("acarbose", "metformin"),
    ("miglitol", "metformin"),
    ("troglitazone", "acetohexamide"),
    ("tolazamide", "tolazamide"),
    ("examide", "examide"),
    ("citoglipton", "examide"),
    ("insulin", "metformin"),
    ("glyburide_metformin", "metformin"),
    ("glipizide_metformin", "acetohexamide"),
    ("glimepiride_pioglitazone", "acetohexamide"),
    ("metformin_rosiglitazone", "acetohexamide"),
    ("metformin_pioglitazone", "acetohexamide"),
    ("change", "change"),
    ("diabetesmed", "diabetesmed"),
    ("readmitted", "label"),
)

_DIABETIC_RETINOPATHY_FIELDS: tuple[tuple[str, str], ...] = (
    ("quality", "i"),
    ("pre_screening", "i"),
    ("ma1", "i"),
    ("ma2", "i"),
    ("ma3", "i"),
    ("ma4", "i"),
    ("ma5", "i"),
    ("ma6", "i"),
    ("exudate1", "d"),
    ("exudate2", "d"),
    ("exudate3", "d"),
    ("exudate32", "d"),
    ("exudate5", "d"),
    ("exudate6", "d"),
    ("exudate7", "d"),
    ("exudate8", "d"),
    ("macula_opticdisc_distance", "d"),
    ("opticdisc_diameter", "d"),
    ("am_fm_classification", "i"),
    ("class", "label"),
)

_DOTA2_GAMES_FIELDS: tuple[tuple[str, str], ...] = (
    ("win", "label"),
    ("clusterid", "i"),
    ("gamemode", "i"),
    ("gametype", "i"),
    ("hero1", "i"),
    ("hero2", "i"),
    ("hero3", "i"),
    ("hero4", "i"),
    ("hero5", "i"),
    ("hero6", "i"),
    ("hero7", "i"),
    ("hero8", "i"),
    ("hero9", "i"),
    ("hero10", "i"),
    ("hero11", "i"),
    ("hero12", "i"),
    ("hero13", "i"),
    ("hero14", "i"),
    ("hero15", "i"),
    ("hero16", "i"),
    ("hero17", "i"),
    ("hero18", "i"),
    ("hero19", "i"),
    ("hero20", "i"),
    ("hero21", "i"),
    ("hero22", "i"),
    ("hero23", "i"),
    ("hero24", "i"),
    ("hero25", "i"),
    ("hero26", "i"),
    ("hero27", "i"),
    ("hero28", "i"),
    ("hero29", "i"),
    ("hero30", "i"),
    ("hero31", "i"),
    ("hero32", "i"),
    ("hero33", "i"),
    ("hero34", "i"),
    ("hero35", "i"),
    ("hero36", "i"),
    ("hero37", "i"),
    ("hero38", "i"),
    ("hero39", "i"),
    ("hero40", "i"),
    ("hero41", "i"),
    ("hero42", "i"),
    ("hero43", "i"),
    ("hero44", "i"),
    ("hero45", "i"),
    ("hero46", "i"),
    ("hero47", "i"),
    ("hero48", "i"),
    ("hero49", "i"),
    ("hero50", "i"),
    ("hero51", "i"),
    ("hero52", "i"),
    ("hero53", "i"),
    ("hero54", "i"),
    ("hero55", "i"),
    ("hero56", "i"),
    ("hero57", "i"),
    ("hero58", "i"),
    ("hero59", "i"),
    ("hero60", "i"),
    ("hero61", "i"),
    ("hero62", "i"),
    ("hero63", "i"),
    ("hero64", "i"),
    ("hero65", "i"),
    ("hero66", "i"),
    ("hero67", "i"),
    ("hero68", "i"),
    ("hero69", "i"),
    ("hero70", "i"),
    ("hero71", "i"),
    ("hero72", "i"),
    ("hero73", "i"),
    ("hero74", "i"),
    ("hero75", "i"),
    ("hero76", "i"),
    ("hero77", "i"),
    ("hero78", "i"),
    ("hero79", "i"),
    ("hero80", "i"),
    ("hero81", "i"),
    ("hero82", "i"),
    ("hero83", "i"),
    ("hero84", "i"),
    ("hero85", "i"),
    ("hero86", "i"),
    ("hero87", "i"),
    ("hero88", "i"),
    ("hero89", "i"),
    ("hero90", "i"),
    ("hero91", "i"),
    ("hero92", "i"),
    ("hero93", "i"),
    ("hero94", "i"),
    ("hero95", "i"),
    ("hero96", "i"),
    ("hero97", "i"),
    ("hero98", "i"),
    ("hero99", "i"),
    ("hero100", "i"),
    ("hero101", "i"),
    ("hero102", "i"),
    ("hero103", "i"),
    ("hero104", "i"),
    ("hero105", "i"),
    ("hero106", "i"),
    ("hero107", "i"),
    ("hero108", "i"),
    ("hero109", "i"),
    ("hero110", "i"),
    ("hero111", "i"),
    ("hero112", "i"),
    ("hero113", "i"),
)

_DRUG_CONSUMPTION_FIELDS: tuple[tuple[str, str], ...] = (
    ("id", "i"),
    ("age", "d"),
    ("gender", "d"),
    ("education", "d"),
    ("country", "d"),
    ("ethnicity", "d"),
    ("nscore", "d"),
    ("escore", "d"),
    ("oscore", "d"),
    ("ascore", "d"),
    ("cscore", "d"),
    ("impuslive", "d"),
    ("ss", "d"),
    ("alcohol", "alcohol"),
    ("amphet", "alcohol"),
    ("amyl", "alcohol"),
    ("benzos", "alcohol"),
    ("caff", "alcohol"),
    ("cannabis", "label"),
    ("choc", "alcohol"),
    ("coke", "alcohol"),
    ("crack", "alcohol"),
    ("ecstasy", "alcohol"),
    ("heroin", "alcohol"),
    ("ketamine", "alcohol"),
    ("legalh", "alcohol"),
    ("lsd", "alcohol"),
    ("meth", "alcohol"),
    ("mushrooms", "alcohol"),
    ("nicotine", "alcohol"),
    ("semer", "semer"),
    ("vsa", "alcohol"),
)

_EEG_EYE_STATE_FIELDS: tuple[tuple[str, str], ...] = (
    ("af3", "d"),
    ("f7", "d"),
    ("f3", "d"),
    ("fc5", "d"),
    ("t7", "d"),
    ("p7", "d"),
    ("o1", "d"),
    ("o2", "d"),
    ("p8", "d"),
    ("t8", "d"),
    ("fc6", "d"),
    ("f4", "d"),
    ("f8", "d"),
    ("af4", "d"),
    ("eyedetection", "label"),
)

_EL_NINO_FIELDS: tuple[tuple[str, str], ...] = (
    ("obs", "i"),
    ("year", "i"),
    ("month", "i"),
    ("day", "i"),
    ("date", "i"),
    ("latitude", "d"),
    ("longitude", "d"),
    ("zon_winds", "d"),
    ("mer_winds", "d"),
    ("humidity", "d"),
    ("air_temp", "target"),
    ("ss_temp", "d"),
)

_ENTRANCE_EXAM_FIELDS: tuple[tuple[str, str], ...] = (
    ("performance", "label"),
    ("gender", "gender"),
    ("caste", "caste"),
    ("coaching", "coaching"),
    ("time", "time_code"),
    ("class_ten_education", "class_ten_education"),
    ("twelve_education", "twelve_education"),
    ("medium", "medium"),
    ("class_x_percentage", "class_x_percentage"),
    ("class_xii_percentage", "class_x_percentage"),
    ("father_occupation", "father_occupation"),
    ("mother_occupation", "mother_occupation"),
)

_FACEBOOK_LIVE_SELLERS_FIELDS: tuple[tuple[str, str], ...] = (
    ("status_id", "i"),
    ("status_type", "label"),
    ("status_published", "time"),
    ("num_reactions", "i"),
    ("num_comments", "i"),
    ("num_shares", "i"),
    ("num_likes", "i"),
    ("num_loves", "i"),
    ("num_wows", "i"),
    ("num_hahas", "i"),
    ("num_sads", "i"),
    ("num_angrys", "i"),
)

_FACEBOOK_METRICS_FIELDS: tuple[tuple[str, str], ...] = (
    ("page_total_likes", "i"),
    ("type", "type"),
    ("category", "i"),
    ("post_month", "i"),
    ("post_weekday", "i"),
    ("post_hour", "i"),
    ("paid", "i"),
    ("lifetime_post_total_reach", "i"),
    ("lifetime_post_total_impressions", "i"),
    ("lifetime_engaged_users", "i"),
    ("lifetime_post_consumers", "i"),
    ("lifetime_post_consumptions", "i"),
    ("lifetime_post_impressions_by_people_who_have_liked_your_page", "i"),
    ("lifetime_post_reach_by_people_who_like_your_page", "i"),
    ("lifetime_people_who_have_liked_your_page_and_engaged_with_your_post", "i"),
    ("comment", "i"),
    ("like", "i"),
    ("share", "i"),
    ("total_interactions", "target"),
)

_FLAGS_FIELDS: tuple[tuple[str, str], ...] = (
    ("name", "text"),
    ("landmass", "i"),
    ("zone", "i"),
    ("area", "i"),
    ("population", "i"),
    ("language", "i"),
    ("religion", "label"),
    ("bars", "i"),
    ("stripes", "i"),
    ("colours", "i"),
    ("red", "i"),
    ("green", "i"),
    ("blue", "i"),
    ("gold", "i"),
    ("white", "i"),
    ("black", "i"),
    ("orange", "i"),
    ("mainhue", "mainhue"),
    ("circles", "i"),
    ("crosses", "i"),
    ("saltries", "i"),
    ("quarters", "i"),
    ("sunstars", "i"),
    ("crescent", "i"),
    ("triangle", "i"),
    ("icon", "i"),
    ("animate", "i"),
    ("text", "i"),
    ("topleft", "topleft"),
    ("botright", "mainhue"),
)

_GAS_TURBINE_EMISSIONS_FIELDS: tuple[tuple[str, str], ...] = (
    ("year", "i"),
    ("at", "d"),
    ("ap", "d"),
    ("ah", "d"),
    ("afdp", "d"),
    ("gtep", "d"),
    ("tit", "d"),
    ("tat", "d"),
    ("tey", "d"),
    ("cdp", "d"),
    ("co", "target"),
    ("nox", "target"),
)

_GENDER_BY_NAME_FIELDS: tuple[tuple[str, str], ...] = (
    ("name", "text"),
    ("gender", "label"),
    ("count", "i"),
    ("probability", "d"),
)

_GLIOMA_GRADING_FIELDS: tuple[tuple[str, str], ...] = (
    ("case_id", "text"),
    ("gender", "i"),
    ("age_at_diagnosis", "d"),
    ("race", "race"),
    ("idh1", "i"),
    ("tp53", "i"),
    ("atrx", "i"),
    ("pten", "i"),
    ("egfr", "i"),
    ("cic", "i"),
    ("muc16", "i"),
    ("pik3ca", "i"),
    ("nf1", "i"),
    ("pik3r1", "i"),
    ("fubp1", "i"),
    ("rb1", "i"),
    ("notch1", "i"),
    ("bcor", "i"),
    ("csmd3", "i"),
    ("smarca4", "i"),
    ("grin2a", "i"),
    ("idh2", "i"),
    ("fat4", "i"),
    ("pdgfra", "i"),
    ("grade", "label"),
)

_GRID_STABILITY_FIELDS: tuple[tuple[str, str], ...] = (
    ("tau1", "d"),
    ("tau2", "d"),
    ("tau3", "d"),
    ("tau4", "d"),
    ("p1", "d"),
    ("p2", "d"),
    ("p3", "d"),
    ("p4", "d"),
    ("g1", "d"),
    ("g2", "d"),
    ("g3", "d"),
    ("g4", "d"),
    ("stab", "d"),
    ("stabf", "label"),
)

_HCV_BLOOD_DONORS_FIELDS: tuple[tuple[str, str], ...] = (
    ("id", "i"),
    ("age", "i"),
    ("sex", "sex"),
    ("alb", "d"),
    ("alp", "d"),
    ("alt", "d"),
    ("ast", "d"),
    ("bil", "d"),
    ("che", "d"),
    ("chol", "d"),
    ("crea", "d"),
    ("cgt", "d"),
    ("prot", "d"),
    ("category", "label"),
)

_HEALTHY_AGING_POLL_FIELDS: tuple[tuple[str, str], ...] = (
    ("number_of_doctors_visited", "label"),
    ("age", "i"),
    ("physical_health", "i"),
    ("mental_health", "i"),
    ("dental_health", "i"),
    ("employment", "i"),
    ("stress_keeps_patient_from_sleeping", "i"),
    ("medication_keeps_patient_from_sleeping", "i"),
    ("pain_keeps_patient_from_sleeping", "i"),
    ("bathroom_needs_keeps_patient_from_sleeping", "i"),
    ("uknown_keeps_patient_from_sleeping", "i"),
    ("trouble_sleeping", "i"),
    ("prescription_sleep_medication", "i"),
    ("race", "i"),
    ("gender", "i"),
)

_HEPATITIS_C_EGYPT_FIELDS: tuple[tuple[str, str], ...] = (
    ("age", "i"),
    ("gender", "i"),
    ("bmi", "i"),
    ("fever", "i"),
    ("nausea_vomting", "i"),
    ("headache", "i"),
    ("diarrhea", "i"),
    ("fatigue_generalized_bone_ache", "i"),
    ("jaundice", "i"),
    ("epigastric_pain", "i"),
    ("wbc", "i"),
    ("rbc", "d"),
    ("hgb", "i"),
    ("plat", "d"),
    ("ast_1", "i"),
    ("alt_1", "i"),
    ("alt4", "d"),
    ("alt_12", "i"),
    ("alt_24", "i"),
    ("alt_36", "i"),
    ("alt_48", "i"),
    ("alt_after_24_w", "i"),
    ("rna_base", "i"),
    ("rna_4", "i"),
    ("rna_12", "i"),
    ("rna_eot", "i"),
    ("rna_ef", "i"),
    ("baseline_histological_grading", "i"),
    ("baselinehistological_staging", "label"),
)

_HIGHER_EDUCATION_STUDENTS_FIELDS: tuple[tuple[str, str], ...] = (
    ("student_id", "text"),
    ("student_age", "i"),
    ("sex", "i"),
    ("graduated_high_school_type", "i"),
    ("scholarship_type", "i"),
    ("additional_work", "i"),
    ("regular_artistic_or_sports_activity", "i"),
    ("do_you_have_a_partner", "i"),
    ("total_salary_if_available", "i"),
    ("transportation_to_the_university", "i"),
    ("accomodation_type_in_cyprus", "i"),
    ("mother_s_education", "i"),
    ("father_s_education", "i"),
    ("number_of_sisters_brothers_if_available", "i"),
    ("parental_status", "i"),
    ("mother_s_occupation", "i"),
    ("father_s_occupation", "i"),
    ("weekly_study_hours", "i"),
    ("reading_frequency_non_scientific_books_journals", "i"),
    ("reading_frequency_scientific_books_journals", "i"),
    ("attendance_to_the_seminars_conferences_related_to_the_department", "i"),
    ("impact_of_your_projects_activities_on_your_success", "i"),
    ("attendance_to_classes", "i"),
    ("preparation_to_midterm_exams_1", "i"),
    ("preparation_to_midterm_exams_2", "i"),
    ("taking_notes_in_classes", "i"),
    ("listening_in_classes", "i"),
    ("discussion_improves_my_interest_and_success_in_the_course", "i"),
    ("flip_classroom", "i"),
    ("cumulative_grade_point_average_in_the_last_semester_4_00", "i"),
    ("expected_cumulative_grade_point_average_in_the_graduation_4_00", "i"),
    ("course_id", "i"),
    ("output_grade", "label"),
)

_HORSE_COLIC_FIELDS: tuple[tuple[str, str], ...] = (
    ("surgery", "i"),
    ("age", "i"),
    ("hospital_number", "i"),
    ("rectal_temperature", "d"),
    ("pulse", "i"),
    ("respiratory_rate", "i"),
    ("temperature_of_extremities", "i"),
    ("peripheral_pulse", "i"),
    ("mucous_membranes", "i"),
    ("capillary_refill_time", "i"),
    ("pain", "i"),
    ("peristalsis", "i"),
    ("abdominal_distension", "i"),
    ("nasogastric_tube", "i"),
    ("nasogastric_reflux", "i"),
    ("nasogastric_reflux_ph", "d"),
    ("rectal_examination_feces", "i"),
    ("abdomen", "i"),
    ("packed_cell_volume", "d"),
    ("total_protein", "d"),
    ("abdominocentesis_appearance", "i"),
    ("abdominocentesis_total_protein", "d"),
    ("outcome", "i"),
    ("surgical_lesion", "label"),
    ("lesion_site", "i"),
    ("lesion_type", "i"),
    ("lesion_subtype", "i"),
    ("cp_data", "i"),
)

_IN_VEHICLE_COUPON_FIELDS: tuple[tuple[str, str], ...] = (
    ("destination", "destination"),
    ("passenger", "passenger"),
    ("weather", "weather"),
    ("temperature", "i"),
    ("time", "time_code"),
    ("coupon", "coupon"),
    ("expiration", "expiration"),
    ("gender", "gender"),
    ("age", "age"),
    ("maritalstatus", "maritalstatus"),
    ("has_children", "i"),
    ("education", "text"),
    ("occupation", "text"),
    ("income", "income"),
    ("car", "text"),
    ("bar", "bar"),
    ("coffeehouse", "bar"),
    ("carryaway", "bar"),
    ("restaurantlessthan20", "bar"),
    ("restaurant20to50", "bar"),
    ("tocoupon_geq5min", "i"),
    ("tocoupon_geq15min", "i"),
    ("tocoupon_geq25min", "i"),
    ("direction_same", "i"),
    ("direction_opp", "i"),
    ("y", "label"),
)

_INFRARED_THERMOGRAPHY_FIELDS: tuple[tuple[str, str], ...] = (
    ("subjectid", "text"),
    ("aveoralf", "target"),
    ("aveoralm", "target"),
    ("gender", "gender"),
    ("age", "age"),
    ("ethnicity", "text"),
    ("t_atm", "d"),
    ("humidity", "d"),
    ("distance", "d"),
    ("t_offset1", "d"),
    ("max1r13_1", "d"),
    ("max1l13_1", "d"),
    ("aveallr13_1", "d"),
    ("avealll13_1", "d"),
    ("t_rc1", "d"),
    ("t_rc_dry1", "d"),
    ("t_rc_wet1", "d"),
    ("t_rc_max1", "d"),
    ("t_lc1", "d"),
    ("t_lc_dry1", "d"),
    ("t_lc_wet1", "d"),
    ("t_lc_max1", "d"),
    ("rcc1", "d"),
    ("lcc1", "d"),
    ("canthimax1", "d"),
    ("canthi4max1", "d"),
    ("t_fhcc1", "d"),
    ("t_fhrc1", "d"),
    ("t_fhlc1", "d"),
    ("t_fhbc1", "d"),
    ("t_fhtc1", "d"),
    ("t_fh_max1", "d"),
    ("t_fhc_max1", "d"),
    ("t_max1", "d"),
    ("t_or1", "d"),
    ("t_or_max1", "d"),
)

_IOT_INTRUSION_FIELDS: tuple[tuple[str, str], ...] = (
    ("id", "i"),
    ("id_orig_p", "i"),
    ("id_resp_p", "i"),
    ("proto", "proto"),
    ("service", "service"),
    ("flow_duration", "d"),
    ("fwd_pkts_tot", "i"),
    ("bwd_pkts_tot", "i"),
    ("fwd_data_pkts_tot", "i"),
    ("bwd_data_pkts_tot", "i"),
    ("fwd_pkts_per_sec", "d"),
    ("bwd_pkts_per_sec", "d"),
    ("flow_pkts_per_sec", "d"),
    ("down_up_ratio", "d"),
    ("fwd_header_size_tot", "i"),
    ("fwd_header_size_min", "i"),
    ("fwd_header_size_max", "i"),
    ("bwd_header_size_tot", "i"),
    ("bwd_header_size_min", "i"),
    ("bwd_header_size_max", "i"),
    ("flow_fin_flag_count", "i"),
    ("flow_syn_flag_count", "i"),
    ("flow_rst_flag_count", "i"),
    ("fwd_psh_flag_count", "i"),
    ("bwd_psh_flag_count", "i"),
    ("flow_ack_flag_count", "i"),
    ("fwd_urg_flag_count", "i"),
    ("bwd_urg_flag_count", "i"),
    ("flow_cwr_flag_count", "i"),
    ("flow_ece_flag_count", "i"),
    ("fwd_pkts_payload_min", "i"),
    ("fwd_pkts_payload_max", "i"),
    ("fwd_pkts_payload_tot", "i"),
    ("fwd_pkts_payload_avg", "d"),
    ("fwd_pkts_payload_std", "d"),
    ("bwd_pkts_payload_min", "i"),
    ("bwd_pkts_payload_max", "i"),
    ("bwd_pkts_payload_tot", "i"),
    ("bwd_pkts_payload_avg", "d"),
    ("bwd_pkts_payload_std", "d"),
    ("flow_pkts_payload_min", "i"),
    ("flow_pkts_payload_max", "i"),
    ("flow_pkts_payload_tot", "i"),
    ("flow_pkts_payload_avg", "d"),
    ("flow_pkts_payload_std", "d"),
    ("fwd_iat_min", "d"),
    ("fwd_iat_max", "d"),
    ("fwd_iat_tot", "d"),
    ("fwd_iat_avg", "d"),
    ("fwd_iat_std", "d"),
    ("bwd_iat_min", "d"),
    ("bwd_iat_max", "d"),
    ("bwd_iat_tot", "d"),
    ("bwd_iat_avg", "d"),
    ("bwd_iat_std", "d"),
    ("flow_iat_min", "d"),
    ("flow_iat_max", "d"),
    ("flow_iat_tot", "d"),
    ("flow_iat_avg", "d"),
    ("flow_iat_std", "d"),
    ("payload_bytes_per_second", "d"),
    ("fwd_subflow_pkts", "d"),
    ("bwd_subflow_pkts", "d"),
    ("fwd_subflow_bytes", "d"),
    ("bwd_subflow_bytes", "d"),
    ("fwd_bulk_bytes", "d"),
    ("bwd_bulk_bytes", "d"),
    ("fwd_bulk_packets", "d"),
    ("bwd_bulk_packets", "d"),
    ("fwd_bulk_rate", "d"),
    ("bwd_bulk_rate", "d"),
    ("active_min", "d"),
    ("active_max", "d"),
    ("active_tot", "d"),
    ("active_avg", "d"),
    ("active_std", "d"),
    ("idle_min", "d"),
    ("idle_max", "d"),
    ("idle_tot", "d"),
    ("idle_avg", "d"),
    ("idle_std", "d"),
    ("fwd_init_window_size", "i"),
    ("bwd_init_window_size", "i"),
    ("fwd_last_window_size", "i"),
    ("attack_type", "label"),
)

_IRANIAN_CHURN_FIELDS: tuple[tuple[str, str], ...] = (
    ("call_failure", "i"),
    ("complains", "i"),
    ("subscription_length", "i"),
    ("charge_amount", "i"),
    ("seconds_of_use", "i"),
    ("frequency_of_use", "i"),
    ("frequency_of_sms", "i"),
    ("distinct_called_numbers", "i"),
    ("age_group", "i"),
    ("tariff_plan", "i"),
    ("status", "i"),
    ("age", "i"),
    ("customer_value", "d"),
    ("churn", "label"),
)

_ISOLET_FIELDS: tuple[tuple[str, str], ...] = (
    *_plain(tuple(f"attribute{at}" for at in range(1, 618)), "d"),
    ("class", "label"),
)

_ISTANBUL_EXCHANGE_FIELDS: tuple[tuple[str, str], ...] = (
    ("date", "text"),
    ("ise", "target"),
    ("ise2", "d"),
    ("sp", "d"),
    ("dax", "d"),
    ("ftse", "d"),
    ("nikkei", "d"),
    ("bovespa", "d"),
    ("eu", "d"),
    ("em", "d"),
)

_KIDNEY_RISK_FACTORS_FIELDS: tuple[tuple[str, str], ...] = (
    ("bp_diastolic", "i"),
    ("bp_limit", "i"),
    ("sg", "sg"),
    ("al", "al"),
    ("class", "label"),
    ("rbc", "i"),
    ("su", "su"),
    ("pc", "i"),
    ("pcc", "i"),
    ("ba", "i"),
    ("bgr", "bgr"),
    ("bu", "bu"),
    ("sod", "sod"),
    ("sc", "sc"),
    ("pot", "pot"),
    ("hemo", "hemo"),
    ("pcv", "pcv"),
    ("rbcc", "rbcc"),
    ("wbcc", "wbcc"),
    ("htn", "i"),
    ("dm", "i"),
    ("cad", "i"),
    ("appet", "i"),
    ("pe", "i"),
    ("ane", "i"),
    ("grf", "grf"),
    ("stage", "stage"),
    ("affected", "i"),
    ("age", "age"),
)

_LAND_MINES_FIELDS: tuple[tuple[str, str], ...] = (
    ("v", "d"),
    ("h", "d"),
    ("s", "d"),
    ("m", "label"),
)

_METRO_TRAFFIC_FIELDS: tuple[tuple[str, str], ...] = (
    ("holiday", "holiday"),
    ("temp", "d"),
    ("rain_1h", "d"),
    ("snow_1h", "d"),
    ("clouds_all", "i"),
    ("weather_main", "weather_main"),
    ("weather_description", "text"),
    ("date_time", "time"),
    ("traffic_volume", "target"),
)

_MICE_PROTEIN_FIELDS: tuple[tuple[str, str], ...] = (
    ("mouseid", "text"),
    ("dyrk1a_n", "d"),
    ("itsn1_n", "d"),
    ("bdnf_n", "d"),
    ("nr1_n", "d"),
    ("nr2a_n", "d"),
    ("pakt_n", "d"),
    ("pbraf_n", "d"),
    ("pcamkii_n", "d"),
    ("pcreb_n", "d"),
    ("pelk_n", "d"),
    ("perk_n", "d"),
    ("pjnk_n", "d"),
    ("pkca_n", "d"),
    ("pmek_n", "d"),
    ("pnr1_n", "d"),
    ("pnr2a_n", "d"),
    ("pnr2b_n", "d"),
    ("ppkcab_n", "d"),
    ("prsk_n", "d"),
    ("akt_n", "d"),
    ("braf_n", "d"),
    ("camkii_n", "d"),
    ("creb_n", "d"),
    ("elk_n", "d"),
    ("erk_n", "d"),
    ("gsk3b_n", "d"),
    ("jnk_n", "d"),
    ("mek_n", "d"),
    ("trka_n", "d"),
    ("rsk_n", "d"),
    ("app_n", "d"),
    ("bcatenin_n", "d"),
    ("sod1_n", "d"),
    ("mtor_n", "d"),
    ("p38_n", "d"),
    ("pmtor_n", "d"),
    ("dscr1_n", "d"),
    ("ampka_n", "d"),
    ("nr2b_n", "d"),
    ("pnumb_n", "d"),
    ("raptor_n", "d"),
    ("tiam1_n", "d"),
    ("pp70s6_n", "d"),
    ("numb_n", "d"),
    ("p70s6_n", "d"),
    ("pgsk3b_n", "d"),
    ("ppkcg_n", "d"),
    ("cdk5_n", "d"),
    ("s6_n", "d"),
    ("adarb1_n", "d"),
    ("acetylh3k9_n", "d"),
    ("rrp1_n", "d"),
    ("bax_n", "d"),
    ("arc_n", "d"),
    ("erbb4_n", "d"),
    ("nnos_n", "d"),
    ("tau_n", "d"),
    ("gfap_n", "d"),
    ("glur3_n", "d"),
    ("glur4_n", "d"),
    ("il1b_n", "d"),
    ("p3525_n", "d"),
    ("pcasp9_n", "d"),
    ("psd95_n", "d"),
    ("snca_n", "d"),
    ("ubiquitin_n", "d"),
    ("pgsk3b_tyr216_n", "d"),
    ("shh_n", "d"),
    ("bad_n", "d"),
    ("bcl2_n", "d"),
    ("ps6_n", "d"),
    ("pcfos_n", "d"),
    ("syp_n", "d"),
    ("h3ack18_n", "d"),
    ("egr1_n", "d"),
    ("h3mek4_n", "d"),
    ("cana_n", "d"),
    ("genotype", "genotype"),
    ("treatment", "treatment"),
    ("behavior", "behavior"),
    ("class", "label"),
)

_MONKS_PROBLEMS_FIELDS: tuple[tuple[str, str], ...] = (
    ("class", "label"),
    *_plain(tuple(f"a{at}" for at in range(1, 7)), "i"),
    ("id", "text"),
)

_MULTIVARIATE_GAIT_FIELDS: tuple[tuple[str, str], ...] = (
    ("subject", "i"),
    ("condition", "i"),
    ("replication", "i"),
    ("leg", "i"),
    ("joint", "i"),
    ("time", "i"),
    ("angle", "target"),
)

_MUSK_VERSION1_FIELDS: tuple[tuple[str, str], ...] = (
    ("molecule_name", "text"),
    ("conformation_name", "text"),
    *_plain(tuple(f"f{at}" for at in range(1, 167)), "i"),
    ("class", "label"),
)

_MUSK_VERSION2_FIELDS: tuple[tuple[str, str], ...] = (
    ("molecule_name", "text"),
    ("conformation_name", "text"),
    *_plain(tuple(f"f{at}" for at in range(1, 167)), "i"),
    ("class", "label"),
)

_NEWS_POPULARITY_FIELDS: tuple[tuple[str, str], ...] = (
    ("url", "text"),
    ("timedelta", "d"),
    ("n_tokens_title", "d"),
    ("n_tokens_content", "d"),
    ("n_unique_tokens", "d"),
    ("n_non_stop_words", "d"),
    ("n_non_stop_unique_tokens", "d"),
    ("num_hrefs", "d"),
    ("num_self_hrefs", "d"),
    ("num_imgs", "d"),
    ("num_videos", "d"),
    ("average_token_length", "d"),
    ("num_keywords", "d"),
    ("data_channel_is_lifestyle", "d"),
    ("data_channel_is_entertainment", "d"),
    ("data_channel_is_bus", "d"),
    ("data_channel_is_socmed", "d"),
    ("data_channel_is_tech", "d"),
    ("data_channel_is_world", "d"),
    ("kw_min_min", "d"),
    ("kw_max_min", "d"),
    ("kw_avg_min", "d"),
    ("kw_min_max", "d"),
    ("kw_max_max", "d"),
    ("kw_avg_max", "d"),
    ("kw_min_avg", "d"),
    ("kw_max_avg", "d"),
    ("kw_avg_avg", "d"),
    ("self_reference_min_shares", "d"),
    ("self_reference_max_shares", "d"),
    ("self_reference_avg_sharess", "d"),
    ("weekday_is_monday", "d"),
    ("weekday_is_tuesday", "d"),
    ("weekday_is_wednesday", "d"),
    ("weekday_is_thursday", "d"),
    ("weekday_is_friday", "d"),
    ("weekday_is_saturday", "d"),
    ("weekday_is_sunday", "d"),
    ("is_weekend", "d"),
    ("lda_00", "d"),
    ("lda_01", "d"),
    ("lda_02", "d"),
    ("lda_03", "d"),
    ("lda_04", "d"),
    ("global_subjectivity", "d"),
    ("global_sentiment_polarity", "d"),
    ("global_rate_positive_words", "d"),
    ("global_rate_negative_words", "d"),
    ("rate_positive_words", "d"),
    ("rate_negative_words", "d"),
    ("avg_positive_polarity", "d"),
    ("min_positive_polarity", "d"),
    ("max_positive_polarity", "d"),
    ("avg_negative_polarity", "d"),
    ("min_negative_polarity", "d"),
    ("max_negative_polarity", "d"),
    ("title_subjectivity", "d"),
    ("title_sentiment_polarity", "d"),
    ("abs_title_subjectivity", "d"),
    ("abs_title_sentiment_polarity", "d"),
    ("shares", "target"),
)

_NHANES_AGE_FIELDS: tuple[tuple[str, str], ...] = (
    ("seqn", "d"),
    ("age_group", "label"),
    ("ridageyr", "d"),
    ("riagendr", "d"),
    ("paq605", "d"),
    ("bmxbmi", "d"),
    ("lbxglu", "d"),
    ("diq010", "d"),
    ("lbxglt", "d"),
    ("lbxin", "d"),
)

_OBESITY_LEVELS_FIELDS: tuple[tuple[str, str], ...] = (
    ("gender", "gender"),
    ("age", "d"),
    ("height", "d"),
    ("weight", "d"),
    ("family_history_with_overweight", "family_history_with_overweight"),
    ("favc", "family_history_with_overweight"),
    ("fcvc", "d"),
    ("ncp", "d"),
    ("caec", "caec"),
    ("smoke", "family_history_with_overweight"),
    ("ch2o", "d"),
    ("scc", "family_history_with_overweight"),
    ("faf", "d"),
    ("tue", "d"),
    ("calc", "caec"),
    ("mtrans", "mtrans"),
    ("nobeyesdad", "label"),
)

_OZONE_LEVEL_FIELDS: tuple[tuple[str, str], ...] = (
    ("dataset", "dataset"),
    ("date", "date"),
    ("wsr0", "d"),
    ("wsr1", "d"),
    ("wsr2", "d"),
    ("wsr3", "d"),
    ("wsr4", "d"),
    ("wsr5", "d"),
    ("wsr6", "d"),
    ("wsr7", "d"),
    ("wsr8", "d"),
    ("wsr9", "d"),
    ("wsr10", "d"),
    ("wsr11", "d"),
    ("wsr12", "d"),
    ("wsr13", "d"),
    ("wsr14", "d"),
    ("wsr15", "d"),
    ("wsr16", "d"),
    ("wsr17", "d"),
    ("wsr18", "d"),
    ("wsr19", "d"),
    ("wsr20", "d"),
    ("wsr21", "d"),
    ("wsr22", "d"),
    ("wsr23", "d"),
    ("wsr_pk", "d"),
    ("wsr_av", "d"),
    ("t0", "d"),
    ("t1", "d"),
    ("t2", "d"),
    ("t3", "d"),
    ("t4", "d"),
    ("t5", "d"),
    ("t6", "d"),
    ("t7", "d"),
    ("t8", "d"),
    ("t9", "d"),
    ("t10", "d"),
    ("t11", "d"),
    ("t12", "d"),
    ("t13", "d"),
    ("t14", "d"),
    ("t15", "d"),
    ("t16", "d"),
    ("t17", "d"),
    ("t18", "d"),
    ("t19", "d"),
    ("t20", "d"),
    ("t21", "d"),
    ("t22", "d"),
    ("t23", "d"),
    ("t_pk", "d"),
    ("t_av", "d"),
    ("t85", "d"),
    ("rh85", "d"),
    ("u85", "d"),
    ("v85", "d"),
    ("ht85", "d"),
    ("t70", "d"),
    ("rh70", "d"),
    ("u70", "d"),
    ("v70", "d"),
    ("ht70", "d"),
    ("t50", "d"),
    ("rh50", "d"),
    ("u50", "d"),
    ("v50", "d"),
    ("ht50", "i"),
    ("ki", "d"),
    ("tt", "d"),
    ("slp", "i"),
    ("slp2", "i"),
    ("precp", "d"),
    ("class", "label"),
)

_PAGE_BLOCKS_FIELDS: tuple[tuple[str, str], ...] = (
    ("height", "i"),
    ("length", "i"),
    ("area", "i"),
    ("eccen", "d"),
    ("p_black", "d"),
    ("p_and", "d"),
    ("mean_tr", "d"),
    ("blackpix", "i"),
    ("blackand", "i"),
    ("wb_trans", "i"),
    ("class", "label"),
)

_PITTSBURGH_BRIDGES_FIELDS: tuple[tuple[str, str], ...] = (
    ("identif", "text"),
    ("river", "river"),
    ("location", "d"),
    ("erected", "label"),
    ("purpose", "purpose"),
    ("length", "length"),
    ("lanes", "i"),
    ("clear_g", "clear_g"),
    ("t_or_d", "t_or_d"),
    ("material", "material"),
    ("span", "length"),
    ("rel_l", "rel_l"),
    ("type", "type"),
)

_POKER_HAND_FIELDS: tuple[tuple[str, str], ...] = (
    ("s1", "i"),
    ("c1", "i"),
    ("s2", "i"),
    ("c2", "i"),
    ("s3", "i"),
    ("c3", "i"),
    ("s4", "i"),
    ("c4", "i"),
    ("s5", "i"),
    ("c5", "i"),
    ("class", "label"),
)

_POLISH_BANKRUPTCY_FIELDS: tuple[tuple[str, str], ...] = (
    ("year", "i"),
    *_plain(tuple(f"a{at}" for at in range(1, 65)), "d"),
    ("class", "label"),
)

_POST_OPERATIVE_PATIENT_FIELDS: tuple[tuple[str, str], ...] = (
    ("l_core", "l_core"),
    ("l_surf", "l_core"),
    ("l_o2", "l_o2"),
    ("l_bp", "l_core"),
    ("surf_stbl", "surf_stbl"),
    ("core_stbl", "core_stbl"),
    ("bp_stbl", "core_stbl"),
    ("comfort", "i"),
    ("adm_decs", "label"),
)

_PREDICTIVE_MAINTENANCE_FIELDS: tuple[tuple[str, str], ...] = (
    ("uid", "i"),
    ("product_id", "text"),
    ("type", "type"),
    ("air_temperature", "d"),
    ("process_temperature", "d"),
    ("rotational_speed", "i"),
    ("torque", "d"),
    ("tool_wear", "i"),
    ("machine_failure", "label"),
    ("twf", "i"),
    ("hdf", "i"),
    ("pwf", "i"),
    ("osf", "i"),
    ("rnf", "i"),
)

_ROOM_OCCUPANCY_COUNT_FIELDS: tuple[tuple[str, str], ...] = (
    ("date", "date_code"),
    ("time", "time"),
    ("s1_temp", "d"),
    ("s2_temp", "d"),
    ("s3_temp", "d"),
    ("s4_temp", "d"),
    ("s1_light", "i"),
    ("s2_light", "i"),
    ("s3_light", "i"),
    ("s4_light", "i"),
    ("s1_sound", "d"),
    ("s2_sound", "d"),
    ("s3_sound", "d"),
    ("s4_sound", "d"),
    ("s5_co2", "i"),
    ("s5_co2_slope", "d"),
    ("s6_pir", "i"),
    ("s7_pir", "i"),
    ("room_occupancy_count", "target"),
)

_SECONDARY_MUSHROOM_FIELDS: tuple[tuple[str, str], ...] = (
    ("class", "label"),
    ("cap_diameter", "d"),
    ("cap_shape", "cap_shape"),
    ("cap_surface", "cap_surface"),
    ("cap_color", "cap_color"),
    ("does_bruise_or_bleed", "does_bruise_or_bleed"),
    ("gill_attachment", "gill_attachment"),
    ("gill_spacing", "gill_spacing"),
    ("gill_color", "gill_color"),
    ("stem_height", "d"),
    ("stem_width", "d"),
    ("stem_root", "stem_root"),
    ("stem_surface", "stem_surface"),
    ("stem_color", "stem_color"),
    ("veil_type", "veil_type"),
    ("veil_color", "veil_color"),
    ("has_ring", "does_bruise_or_bleed"),
    ("ring_type", "ring_type"),
    ("spore_print_color", "spore_print_color"),
    ("habitat", "habitat"),
    ("season", "season"),
)

_SEOUL_BIKE_SHARING_FIELDS: tuple[tuple[str, str], ...] = (
    ("date", "date"),
    ("rented_bike_count", "target"),
    ("hour", "i"),
    ("temperature", "d"),
    ("humidity", "i"),
    ("wind_speed", "d"),
    ("visibility", "i"),
    ("dew_point_temperature", "d"),
    ("solar_radiation", "d"),
    ("rainfall", "d"),
    ("snowfall", "d"),
    ("seasons", "seasons"),
    ("holiday", "holiday"),
    ("functioning_day", "functioning_day"),
)

_SEPSIS_SURVIVAL_FIELDS: tuple[tuple[str, str], ...] = (
    ("age_years", "i"),
    ("sex_0male_1female", "i"),
    ("episode_number", "i"),
    ("hospital_outcome_1alive_0dead", "label"),
)

_SKIN_SEGMENTATION_FIELDS: tuple[tuple[str, str], ...] = (
    ("b", "i"),
    ("g", "i"),
    ("r", "i"),
    ("y", "label"),
)

_SOYBEAN_CULTIVARS_FIELDS: tuple[tuple[str, str], ...] = (
    ("season", "i"),
    ("cultivar", "cultivar"),
    ("repetition", "i"),
    ("ph", "d"),
    ("ifp", "d"),
    ("nlp", "d"),
    ("ngp", "d"),
    ("ngl", "d"),
    ("ns", "d"),
    ("mhg", "d"),
    ("gy", "target"),
)

_SOYBEAN_SMALL_FIELDS: tuple[tuple[str, str], ...] = (
    ("date", "i"),
    ("plant_stand", "i"),
    ("precip", "i"),
    ("temp", "i"),
    ("hail", "i"),
    ("crop_hist", "i"),
    ("area_damaged", "i"),
    ("severity", "i"),
    ("seed_tmt", "i"),
    ("germination", "i"),
    ("plant_growth", "i"),
    ("leaves", "i"),
    ("leafspots_halo", "i"),
    ("leafspots_marg", "i"),
    ("leafspot_size", "i"),
    ("leaf_shread", "i"),
    ("leaf_malf", "i"),
    ("leaf_mild", "i"),
    ("stem", "i"),
    ("lodging", "i"),
    ("stem_cankers", "i"),
    ("canker_lesion", "i"),
    ("fruiting_bodies", "i"),
    ("external_decay", "i"),
    ("mycelium", "i"),
    ("int_discolor", "i"),
    ("sclerotia", "i"),
    ("fruit_pods", "i"),
    ("fruit_spots", "i"),
    ("seed", "i"),
    ("mold_growth", "i"),
    ("seed_discolor", "i"),
    ("seed_size", "i"),
    ("shriveling", "i"),
    ("roots", "i"),
    ("class", "label"),
)

_SPECT_HEART_FIELDS: tuple[tuple[str, str], ...] = (
    ("overall_diagnosis", "label"),
    *_plain(tuple(f"f{at}" for at in range(1, 23)), "i"),
)

_SPECTF_HEART_FIELDS: tuple[tuple[str, str], ...] = (
    ("diagnosis", "label"),
    ("f1r", "i"),
    ("f1s", "i"),
    ("f2r", "i"),
    ("f2s", "i"),
    ("f3r", "i"),
    ("f3s", "i"),
    ("f4r", "i"),
    ("f4s", "i"),
    ("f5r", "i"),
    ("f5s", "i"),
    ("f6r", "i"),
    ("f6s", "i"),
    ("f7r", "i"),
    ("f7s", "i"),
    ("f8r", "i"),
    ("f8s", "i"),
    ("f9r", "i"),
    ("f9s", "i"),
    ("f10r", "i"),
    ("f10s", "i"),
    ("f11r", "i"),
    ("f11s", "i"),
    ("f12r", "i"),
    ("f12s", "i"),
    ("f13r", "i"),
    ("f13s", "i"),
    ("f14r", "i"),
    ("f14s", "i"),
    ("f15r", "i"),
    ("f15s", "i"),
    ("f16r", "i"),
    ("f16s", "i"),
    ("f17r", "i"),
    ("f17s", "i"),
    ("f18r", "i"),
    ("f18s", "i"),
    ("f19r", "i"),
    ("f19s", "i"),
    ("f20r", "i"),
    ("f20s", "i"),
    ("f21r", "i"),
    ("f21s", "i"),
    ("f22r", "i"),
    ("f22s", "i"),
)

_SPLICE_JUNCTIONS_FIELDS: tuple[tuple[str, str], ...] = (
    ("class", "label"),
    ("instancename", "text"),
    ("base1", "base1"),
    ("base2", "base1"),
    *_plain(tuple(f"base{at}" for at in range(3, 14)), "base3"),
    ("base14", "base14"),
    ("base15", "base3"),
    ("base16", "base3"),
    ("base17", "base3"),
    ("base18", "base3"),
    *_plain(tuple(f"base{at}" for at in range(19, 35)), "base14"),
    ("base35", "base35"),
    ("base36", "base36"),
    *_plain(tuple(f"base{at}" for at in range(37, 61)), "base14"),
)

_STEEL_PLATES_FIELDS: tuple[tuple[str, str], ...] = (
    ("x_minimum", "i"),
    ("x_maximum", "i"),
    ("y_minimum", "i"),
    ("y_maximum", "i"),
    ("pixels_areas", "i"),
    ("x_perimeter", "i"),
    ("y_perimeter", "i"),
    ("sum_of_luminosity", "i"),
    ("minimum_of_luminosity", "i"),
    ("maximum_of_luminosity", "i"),
    ("length_of_conveyer", "i"),
    ("typeofsteel_a300", "i"),
    ("typeofsteel_a400", "i"),
    ("steel_plate_thickness", "i"),
    ("edges_index", "d"),
    ("empty_index", "d"),
    ("square_index", "d"),
    ("outside_x_index", "d"),
    ("edges_x_index", "d"),
    ("edges_y_index", "d"),
    ("outside_global_index", "d"),
    ("logofareas", "d"),
    ("log_x_index", "d"),
    ("log_y_index", "d"),
    ("orientation_index", "d"),
    ("luminosity_index", "d"),
    ("sigmoidofareas", "d"),
    ("pastry", "target"),
    ("z_scratch", "target"),
    ("k_scratch", "target"),
    ("stains", "target"),
    ("dirtiness", "target"),
    ("bumps", "target"),
    ("other_faults", "target"),
)

_STUDENT_ACADEMICS_FIELDS: tuple[tuple[str, str], ...] = (
    ("ge", "ge"),
    ("cst", "cst"),
    ("tnp", "tnp"),
    ("twp", "tnp"),
    ("iap", "tnp"),
    ("esp", "label"),
    ("arr", "arr"),
    ("ms", "ms"),
    ("ls", "ls"),
    ("as", "as"),
    ("fmi", "fmi"),
    ("fs", "fs"),
    ("fq", "fq"),
    ("mq", "fq"),
    ("fo", "fo"),
    ("mo", "mo"),
    ("nf", "fs"),
    ("sh", "sh"),
    ("ss", "ss"),
    ("me", "me"),
    ("tt", "fs"),
    ("atd", "sh"),
)

_STUDENT_DROPOUT_FIELDS: tuple[tuple[str, str], ...] = (
    ("marital_status", "i"),
    ("application_mode", "i"),
    ("application_order", "i"),
    ("course", "i"),
    ("daytime_evening_attendance", "i"),
    ("previous_qualification", "i"),
    ("previous_qualification_grade", "d"),
    ("nacionality", "i"),
    ("mother_s_qualification", "i"),
    ("father_s_qualification", "i"),
    ("mother_s_occupation", "i"),
    ("father_s_occupation", "i"),
    ("admission_grade", "d"),
    ("displaced", "i"),
    ("educational_special_needs", "i"),
    ("debtor", "i"),
    ("tuition_fees_up_to_date", "i"),
    ("gender", "i"),
    ("scholarship_holder", "i"),
    ("age_at_enrollment", "i"),
    ("international", "i"),
    ("curricular_units_1st_sem_credited", "i"),
    ("curricular_units_1st_sem_enrolled", "i"),
    ("curricular_units_1st_sem_evaluations", "i"),
    ("curricular_units_1st_sem_approved", "i"),
    ("curricular_units_1st_sem_grade", "d"),
    ("curricular_units_1st_sem_without_evaluations", "i"),
    ("curricular_units_2nd_sem_credited", "i"),
    ("curricular_units_2nd_sem_enrolled", "i"),
    ("curricular_units_2nd_sem_evaluations", "i"),
    ("curricular_units_2nd_sem_approved", "i"),
    ("curricular_units_2nd_sem_grade", "d"),
    ("curricular_units_2nd_sem_without_evaluations", "i"),
    ("unemployment_rate", "d"),
    ("inflation_rate", "d"),
    ("gdp", "d"),
    ("target", "label"),
)

_SUPERCONDUCTIVITY_FIELDS: tuple[tuple[str, str], ...] = (
    ("number_of_elements", "i"),
    ("mean_atomic_mass", "d"),
    ("wtd_mean_atomic_mass", "d"),
    ("gmean_atomic_mass", "d"),
    ("wtd_gmean_atomic_mass", "d"),
    ("entropy_atomic_mass", "d"),
    ("wtd_entropy_atomic_mass", "d"),
    ("range_atomic_mass", "d"),
    ("wtd_range_atomic_mass", "d"),
    ("std_atomic_mass", "d"),
    ("wtd_std_atomic_mass", "d"),
    ("mean_fie", "d"),
    ("wtd_mean_fie", "d"),
    ("gmean_fie", "d"),
    ("wtd_gmean_fie", "d"),
    ("entropy_fie", "d"),
    ("wtd_entropy_fie", "d"),
    ("range_fie", "d"),
    ("wtd_range_fie", "d"),
    ("std_fie", "d"),
    ("wtd_std_fie", "d"),
    ("mean_atomic_radius", "d"),
    ("wtd_mean_atomic_radius", "d"),
    ("gmean_atomic_radius", "d"),
    ("wtd_gmean_atomic_radius", "d"),
    ("entropy_atomic_radius", "d"),
    ("wtd_entropy_atomic_radius", "d"),
    ("range_atomic_radius", "i"),
    ("wtd_range_atomic_radius", "d"),
    ("std_atomic_radius", "d"),
    ("wtd_std_atomic_radius", "d"),
    ("mean_density", "d"),
    ("wtd_mean_density", "d"),
    ("gmean_density", "d"),
    ("wtd_gmean_density", "d"),
    ("entropy_density", "d"),
    ("wtd_entropy_density", "d"),
    ("range_density", "d"),
    ("wtd_range_density", "d"),
    ("std_density", "d"),
    ("wtd_std_density", "d"),
    ("mean_electronaffinity", "d"),
    ("wtd_mean_electronaffinity", "d"),
    ("gmean_electronaffinity", "d"),
    ("wtd_gmean_electronaffinity", "d"),
    ("entropy_electronaffinity", "d"),
    ("wtd_entropy_electronaffinity", "d"),
    ("range_electronaffinity", "d"),
    ("wtd_range_electronaffinity", "d"),
    ("std_electronaffinity", "d"),
    ("wtd_std_electronaffinity", "d"),
    ("mean_fusionheat", "d"),
    ("wtd_mean_fusionheat", "d"),
    ("gmean_fusionheat", "d"),
    ("wtd_gmean_fusionheat", "d"),
    ("entropy_fusionheat", "d"),
    ("wtd_entropy_fusionheat", "d"),
    ("range_fusionheat", "d"),
    ("wtd_range_fusionheat", "d"),
    ("std_fusionheat", "d"),
    ("wtd_std_fusionheat", "d"),
    ("mean_thermalconductivity", "d"),
    ("wtd_mean_thermalconductivity", "d"),
    ("gmean_thermalconductivity", "d"),
    ("wtd_gmean_thermalconductivity", "d"),
    ("entropy_thermalconductivity", "d"),
    ("wtd_entropy_thermalconductivity", "d"),
    ("range_thermalconductivity", "d"),
    ("wtd_range_thermalconductivity", "d"),
    ("std_thermalconductivity", "d"),
    ("wtd_std_thermalconductivity", "d"),
    ("mean_valence", "d"),
    ("wtd_mean_valence", "d"),
    ("gmean_valence", "d"),
    ("wtd_gmean_valence", "d"),
    ("entropy_valence", "d"),
    ("wtd_entropy_valence", "d"),
    ("range_valence", "i"),
    ("wtd_range_valence", "d"),
    ("std_valence", "d"),
    ("wtd_std_valence", "d"),
    ("critical_temp", "target"),
)

_SUPPORT2_FIELDS: tuple[tuple[str, str], ...] = (
    ("id", "i"),
    ("age", "d"),
    ("death", "i"),
    ("sex", "sex"),
    ("hospdead", "label"),
    ("slos", "i"),
    ("d_time", "i"),
    ("dzgroup", "dzgroup"),
    ("dzclass", "dzclass"),
    ("num_co", "i"),
    ("edu", "i"),
    ("income", "income"),
    ("scoma", "i"),
    ("charges", "d"),
    ("totcst", "d"),
    ("totmcst", "d"),
    ("avtisst", "d"),
    ("race", "race"),
    ("sps", "d"),
    ("aps", "i"),
    ("surv2m", "d"),
    ("surv6m", "d"),
    ("hday", "i"),
    ("diabetes", "i"),
    ("dementia", "i"),
    ("ca", "ca"),
    ("prg2m", "d"),
    ("prg6m", "d"),
    ("dnr", "dnr"),
    ("dnrday", "i"),
    ("meanbp", "d"),
    ("wblc", "d"),
    ("hrt", "d"),
    ("resp", "i"),
    ("temp", "d"),
    ("pafi", "d"),
    ("alb", "d"),
    ("bili", "d"),
    ("crea", "d"),
    ("sod", "i"),
    ("ph", "d"),
    ("glucose", "d"),
    ("bun", "d"),
    ("urine", "d"),
    ("adlp", "i"),
    ("adls", "i"),
    ("sfdm2", "sfdm2"),
    ("adlsc", "d"),
)

_TAIWANESE_BANKRUPTCY_FIELDS: tuple[tuple[str, str], ...] = (
    ("bankrupt", "label"),
    ("roa_c_before_interest_and_depreciation_before_interest", "d"),
    ("roa_a_before_interest_and_after_tax", "d"),
    ("roa_b_before_interest_and_depreciation_after_tax", "d"),
    ("operating_gross_margin", "d"),
    ("realized_sales_gross_margin", "d"),
    ("operating_profit_rate", "d"),
    ("pre_tax_net_interest_rate", "d"),
    ("after_tax_net_interest_rate", "d"),
    ("non_industry_income_and_expenditure_revenue", "d"),
    ("continuous_interest_rate_after_tax", "d"),
    ("operating_expense_rate", "d"),
    ("research_and_development_expense_rate", "d"),
    ("cash_flow_rate", "d"),
    ("interest_bearing_debt_interest_rate", "d"),
    ("tax_rate_a", "d"),
    ("net_value_per_share_b", "d"),
    ("net_value_per_share_a", "d"),
    ("net_value_per_share_c", "d"),
    ("persistent_eps_in_the_last_four_seasons", "d"),
    ("cash_flow_per_share", "d"),
    ("revenue_per_share_yuan", "d"),
    ("operating_profit_per_share_yuan", "d"),
    ("per_share_net_profit_before_tax_yuan", "d"),
    ("realized_sales_gross_profit_growth_rate", "d"),
    ("operating_profit_growth_rate", "d"),
    ("after_tax_net_profit_growth_rate", "d"),
    ("regular_net_profit_growth_rate", "d"),
    ("continuous_net_profit_growth_rate", "d"),
    ("total_asset_growth_rate", "d"),
    ("net_value_growth_rate", "d"),
    ("total_asset_return_growth_rate_ratio", "d"),
    ("cash_reinvestment", "d"),
    ("current_ratio", "d"),
    ("quick_ratio", "d"),
    ("interest_expense_ratio", "d"),
    ("total_debt_total_net_worth", "d"),
    ("debt_ratio", "d"),
    ("net_worth_assets", "d"),
    ("long_term_fund_suitability_ratio_a", "d"),
    ("borrowing_dependency", "d"),
    ("contingent_liabilities_net_worth", "d"),
    ("operating_profit_paid_in_capital", "d"),
    ("net_profit_before_tax_paid_in_capital", "d"),
    ("inventory_and_accounts_receivable_net_value", "d"),
    ("total_asset_turnover", "d"),
    ("accounts_receivable_turnover", "d"),
    ("average_collection_days", "d"),
    ("inventory_turnover_rate_times", "d"),
    ("fixed_assets_turnover_frequency", "d"),
    ("net_worth_turnover_rate_times", "d"),
    ("revenue_per_person", "d"),
    ("operating_profit_per_person", "d"),
    ("allocation_rate_per_person", "d"),
    ("working_capital_to_total_assets", "d"),
    ("quick_assets_total_assets", "d"),
    ("current_assets_total_assets", "d"),
    ("cash_total_assets", "d"),
    ("quick_assets_current_liability", "d"),
    ("cash_current_liability", "d"),
    ("current_liability_to_assets", "d"),
    ("operating_funds_to_liability", "d"),
    ("inventory_working_capital", "d"),
    ("inventory_current_liability", "d"),
    ("current_liabilities_liability", "d"),
    ("working_capital_equity", "d"),
    ("current_liabilities_equity", "d"),
    ("long_term_liability_to_current_assets", "d"),
    ("retained_earnings_to_total_assets", "d"),
    ("total_income_total_expense", "d"),
    ("total_expense_assets", "d"),
    ("current_asset_turnover_rate", "d"),
    ("quick_asset_turnover_rate", "d"),
    ("working_capitcal_turnover_rate", "d"),
    ("cash_turnover_rate", "d"),
    ("cash_flow_to_sales", "d"),
    ("fixed_assets_to_assets", "d"),
    ("current_liability_to_liability", "d"),
    ("current_liability_to_equity", "d"),
    ("equity_to_long_term_liability", "d"),
    ("cash_flow_to_total_assets", "d"),
    ("cash_flow_to_liability", "d"),
    ("cfo_to_assets", "d"),
    ("cash_flow_to_equity", "d"),
    ("current_liability_to_current_assets", "d"),
    ("liability_assets_flag", "i"),
    ("net_income_to_total_assets", "d"),
    ("total_assets_to_gnp_price", "d"),
    ("no_credit_interval", "d"),
    ("gross_profit_to_sales", "d"),
    ("net_income_to_stockholder_s_equity", "d"),
    ("liability_to_equity", "d"),
    ("degree_of_financial_leverage_dfl", "d"),
    ("interest_coverage_ratio_interest_expense_to_ebit", "d"),
    ("net_income_flag", "i"),
    ("equity_to_liability", "d"),
)

_TENNIS_MAJORS_FIELDS: tuple[tuple[str, str], ...] = (
    ("tournament", "tournament"),
    ("player1", "text"),
    ("player2", "text"),
    ("round", "i"),
    ("result", "label"),
    ("fnl1", "i"),
    ("fnl2", "i"),
    ("fsp_1", "i"),
    ("fsw_1", "i"),
    ("ssp_1", "i"),
    ("ssw_1", "i"),
    ("ace_1", "i"),
    ("dbf_1", "i"),
    ("wnr_1", "i"),
    ("ufe_1", "i"),
    ("bpc_1", "i"),
    ("bpw_1", "i"),
    ("npa_1", "i"),
    ("npw_1", "i"),
    ("tpw_1", "i"),
    ("st1_1", "i"),
    ("st2_1", "i"),
    ("st3_1", "i"),
    ("st4_1", "i"),
    ("st5_1", "i"),
    ("fsp_2", "i"),
    ("fsw_2", "i"),
    ("ssp_2", "i"),
    ("ssw_2", "i"),
    ("ace_2", "i"),
    ("dbf_2", "i"),
    ("wnr_2", "i"),
    ("ufe_2", "i"),
    ("bpc_2", "i"),
    ("bpw_2", "i"),
    ("npa_2", "i"),
    ("npw_2", "i"),
    ("tpw_2", "i"),
    ("st1_2", "i"),
    ("st2_2", "i"),
    ("st3_2", "i"),
    ("st4_2", "i"),
    ("st5_2", "i"),
)

_TETOUAN_POWER_FIELDS: tuple[tuple[str, str], ...] = (
    ("datetime", "time"),
    ("temperature", "d"),
    ("humidity", "d"),
    ("wind_speed", "d"),
    ("general_diffuse_flows", "d"),
    ("diffuse_flows", "d"),
    ("zone_1_power_consumption", "target"),
    ("zone_2_power_consumption", "target"),
    ("zone_3_power_consumption", "target"),
)

_THORACIC_SURGERY_FIELDS: tuple[tuple[str, str], ...] = (
    ("dgn", "dgn"),
    ("pre4", "d"),
    ("pre5", "d"),
    ("pre6", "pre6"),
    ("pre7", "pre7"),
    ("pre8", "pre7"),
    ("pre9", "pre7"),
    ("pre10", "pre7"),
    ("pre11", "pre7"),
    ("pre14", "pre14"),
    ("pre17", "pre7"),
    ("pre19", "pre7"),
    ("pre25", "pre7"),
    ("pre30", "pre7"),
    ("pre32", "pre7"),
    ("age", "i"),
    ("risk1yr", "label"),
)

_THYROID_RECURRENCE_FIELDS: tuple[tuple[str, str], ...] = (
    ("age", "i"),
    ("gender", "gender"),
    ("smoking", "smoking"),
    ("hx_smoking", "smoking"),
    ("hx_radiothreapy", "smoking"),
    ("thyroid_function", "thyroid_function"),
    ("physical_examination", "physical_examination"),
    ("adenopathy", "adenopathy"),
    ("pathology", "pathology"),
    ("focality", "focality"),
    ("risk", "risk"),
    ("t", "t"),
    ("n", "n"),
    ("m", "m"),
    ("stage", "stage"),
    ("response", "response"),
    ("recurred", "label"),
)

_USER_KNOWLEDGE_FIELDS: tuple[tuple[str, str], ...] = (
    ("stg", "d"),
    ("scg", "d"),
    ("str", "d"),
    ("lpr", "d"),
    ("peg", "d"),
    ("uns", "label"),
)

_WAVEFORM_FIELDS: tuple[tuple[str, str], ...] = (
    *_plain(tuple(f"attribute{at}" for at in range(1, 22)), "d"),
    ("class", "label"),
)

_WEBSITE_PHISHING_FIELDS: tuple[tuple[str, str], ...] = (
    ("sfh", "i"),
    ("popupwindow", "i"),
    ("sslfinal_state", "i"),
    ("request_url", "i"),
    ("url_of_anchor", "i"),
    ("web_traffic", "i"),
    ("url_length", "i"),
    ("age_of_domain", "i"),
    ("having_ip_address", "i"),
    ("result", "label"),
)

_WHOLESALE_CUSTOMERS_FIELDS: tuple[tuple[str, str], ...] = (
    ("channel", "label"),
    ("region", "i"),
    ("fresh", "i"),
    ("milk", "i"),
    ("grocery", "i"),
    ("frozen", "i"),
    ("detergents_paper", "i"),
    ("delicassen", "i"),
)

_YOUTUBE_SPAM_FIELDS: tuple[tuple[str, str], ...] = (
    ("video", "video"),
    ("comment_id", "text"),
    ("author", "text"),
    ("date", "text"),
    ("content", "text"),
    ("class", "label"),
)

#: The sets below are the teaching tables of the R world, read from the CSV
#: Rdatasets serves each as: one header row naming the columns the way the R
#: package documents them, a first column of row names, then a row per example.
#: A row-name column that counts from one is kept as ``row``, and one that names
#: the thing measured - a car, a canton, a state - is kept as ``name``. What each
#: may be passed on under is what the package it ships in says.
_ACUTE_MYELOID_LEUKAEMIA_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("id", "i"),
    ("trt", "label"),
    ("sex", "sex"),
    ("flt3", "flt3"),
    ("futime", "i"),
    ("death", "i"),
    ("txtime", "i"),
    ("crtime", "i"),
    ("rltime", "i"),
)

_ALCOHOL_BY_COUNTRY_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("country", "text"),
    ("beer_servings", "i"),
    ("spirit_servings", "i"),
    ("wine_servings", "i"),
    ("total_litres_of_pure_alcohol", "target"),
)

_ANIMAL_SCAT_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("species", "label"),
    ("month", "month"),
    ("year", "i"),
    ("site", "site"),
    ("location", "location"),
    ("age", "i"),
    ("number", "i"),
    ("length", "d"),
    ("diameter", "d"),
    ("taper", "d"),
    ("ti", "d"),
    ("mass", "d"),
    ("d13c", "d"),
    ("d15n", "d"),
    ("cn", "d"),
    ("ropey", "i"),
    ("segmented", "i"),
    ("flat", "i"),
    ("scrape", "i"),
)

_ANOREXIA_TREATMENT_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("treat", "label"),
    ("prewt", "d"),
    ("postwt", "d"),
)

_BAD_DRIVERS_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("state", "text"),
    ("num_drivers", "d"),
    ("perc_speeding", "i"),
    ("perc_alcohol", "i"),
    ("perc_not_distracted", "i"),
    ("perc_no_previous", "i"),
    ("insurance_premiums", "target"),
    ("losses", "d"),
)

_BASEBALL_BATTING_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("playerid", "text"),
    ("yearid", "i"),
    ("stint", "i"),
    ("teamid", "text"),
    ("lgid", "lgid"),
    ("g", "i"),
    ("ab", "i"),
    ("r", "i"),
    ("h", "i"),
    ("x2b", "i"),
    ("x3b", "i"),
    ("hr", "target"),
    ("rbi", "i"),
    ("sb", "i"),
    ("cs", "i"),
    ("bb", "i"),
    ("so", "i"),
    ("ibb", "i"),
    ("hbp", "i"),
    ("sh", "i"),
    ("sf", "i"),
    ("gidp", "i"),
)

_BASEBALL_FIELDING_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("playerid", "text"),
    ("yearid", "i"),
    ("stint", "i"),
    ("teamid", "text"),
    ("lgid", "lgid"),
    ("pos", "pos"),
    ("g", "i"),
    ("gs", "i"),
    ("innouts", "i"),
    ("po", "i"),
    ("a", "i"),
    ("e", "target"),
    ("dp", "i"),
    ("pb", "i"),
    ("wp", "i"),
    ("sb", "i"),
    ("cs", "i"),
    ("zr", "i"),
)

_BASEBALL_HALL_OF_FAME_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("playerid", "text"),
    ("yearid", "i"),
    ("votedby", "text"),
    ("ballots", "i"),
    ("needed", "i"),
    ("votes", "target"),
    ("inducted", "inducted"),
    ("category", "category"),
    ("needed_note", "text"),
)

_BASEBALL_HITTERS_FIELDS: tuple[tuple[str, str], ...] = (
    ("name", "text"),
    ("atbat", "i"),
    ("hits", "i"),
    ("hmrun", "i"),
    ("runs", "i"),
    ("rbi", "i"),
    ("walks", "i"),
    ("years", "i"),
    ("catbat", "i"),
    ("chits", "i"),
    ("chmrun", "i"),
    ("cruns", "i"),
    ("crbi", "i"),
    ("cwalks", "i"),
    ("league", "league"),
    ("division", "division"),
    ("putouts", "i"),
    ("assists", "i"),
    ("errors", "i"),
    ("salary", "target"),
    ("newleague", "league"),
)

_BASEBALL_MANAGERS_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("playerid", "text"),
    ("yearid", "i"),
    ("teamid", "text"),
    ("lgid", "lgid"),
    ("inseason", "i"),
    ("g", "i"),
    ("w", "target"),
    ("l", "i"),
    ("rank", "i"),
    ("plyrmgr", "plyrmgr"),
)

_BASEBALL_PITCHING_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("playerid", "text"),
    ("yearid", "i"),
    ("stint", "i"),
    ("teamid", "text"),
    ("lgid", "lgid"),
    ("w", "i"),
    ("l", "i"),
    ("g", "i"),
    ("gs", "i"),
    ("cg", "i"),
    ("sho", "i"),
    ("sv", "i"),
    ("ipouts", "i"),
    ("h", "i"),
    ("er", "i"),
    ("hr", "i"),
    ("bb", "i"),
    ("so", "i"),
    ("baopp", "d"),
    ("era", "target"),
    ("ibb", "i"),
    ("wp", "i"),
    ("hbp", "i"),
    ("bk", "i"),
    ("bfp", "i"),
    ("gf", "i"),
    ("r", "i"),
    ("sh", "i"),
    ("sf", "i"),
    ("gidp", "i"),
)

_BASEBALL_PLAYERS_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("playerid", "text"),
    ("birthyear", "i"),
    ("birthmonth", "i"),
    ("birthday", "i"),
    ("birthcity", "text"),
    ("birthcountry", "text"),
    ("birthstate", "text"),
    ("deathyear", "i"),
    ("deathmonth", "i"),
    ("deathday", "i"),
    ("deathcountry", "text"),
    ("deathstate", "text"),
    ("deathcity", "text"),
    ("namefirst", "text"),
    ("namelast", "text"),
    ("namegiven", "text"),
    ("weight", "i"),
    ("height", "target"),
    ("bats", "bats"),
    ("throws", "throws"),
    ("debut", "date"),
    ("bbrefid", "text"),
    ("finalgame", "date"),
    ("retroid", "text"),
    ("deathdate", "date"),
    ("birthdate", "date"),
)

_BASEBALL_SALARIES_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("yearid", "i"),
    ("teamid", "text"),
    ("lgid", "lgid"),
    ("playerid", "text"),
    ("salary", "target"),
)

_BECHDEL_TEST_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("year", "i"),
    ("imdb", "text"),
    ("title", "text"),
    ("test", "test"),
    ("clean_test", "clean_test"),
    ("binary", "label"),
    ("budget", "i"),
    ("domgross", "i"),
    ("intgross", "d"),
    ("code", "text"),
    ("budget_2013", "i"),
    ("domgross_2013", "i"),
    ("intgross_2013", "d"),
    ("period_code", "i"),
    ("decade_code", "i"),
)

_BILIARY_CHOLANGITIS_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("id", "i"),
    ("time", "target"),
    ("status", "i"),
    ("trt", "i"),
    ("age", "d"),
    ("sex", "sex"),
    ("ascites", "i"),
    ("hepato", "i"),
    ("spiders", "i"),
    ("edema", "d"),
    ("bili", "d"),
    ("chol", "i"),
    ("albumin", "d"),
    ("copper", "i"),
    ("alk_phos", "d"),
    ("ast", "d"),
    ("trig", "i"),
    ("platelet", "i"),
    ("protime", "d"),
    ("stage", "i"),
)

_BLACK_CHERRY_TREES_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("girth", "d"),
    ("height", "i"),
    ("volume", "target"),
)

_BLADDER_TUMOURS_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("id", "i"),
    ("rx", "i"),
    ("number", "i"),
    ("size", "i"),
    ("stop", "target"),
    ("event", "i"),
    ("enum", "i"),
)

_BOATING_TRIPS_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("trips", "target"),
    ("quality", "i"),
    ("ski", "ski"),
    ("income", "i"),
    ("userfee", "ski"),
    ("costc", "d"),
    ("costs", "d"),
    ("costh", "d"),
)

_BOSTON_HOUSING_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("crim", "d"),
    ("zn", "d"),
    ("indus", "d"),
    ("chas", "i"),
    ("nox", "d"),
    ("rm", "d"),
    ("age", "d"),
    ("dis", "d"),
    ("rad", "i"),
    ("tax", "i"),
    ("ptratio", "d"),
    ("black", "d"),
    ("lstat", "d"),
    ("medv", "target"),
)

_BREAST_CANCER_GBSG_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("pid", "i"),
    ("age", "i"),
    ("meno", "i"),
    ("size", "i"),
    ("grade", "i"),
    ("nodes", "i"),
    ("pgr", "i"),
    ("er", "i"),
    ("hormon", "i"),
    ("rfstime", "target"),
    ("status", "i"),
)

_BRUSHTAIL_POSSUMS_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("site", "i"),
    ("pop", "label"),
    ("sex", "sex"),
    ("age", "i"),
    ("head_l", "d"),
    ("skull_w", "d"),
    ("total_l", "d"),
    ("tail_l", "d"),
)

_CALIFORNIA_SCHOOLS_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("district", "i"),
    ("school", "text"),
    ("county", "text"),
    ("grades", "grades"),
    ("students", "i"),
    ("teachers", "d"),
    ("calworks", "d"),
    ("lunch", "d"),
    ("computer", "i"),
    ("expenditure", "d"),
    ("income", "d"),
    ("english", "d"),
    ("read", "target"),
    ("math", "target"),
)

_CANADIAN_INTERLOCKS_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("assets", "i"),
    ("sector", "sector"),
    ("nation", "nation"),
    ("interlocks", "target"),
)

_CANADIAN_WOMENS_WORK_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("partic", "label"),
    ("hincome", "i"),
    ("children", "children"),
    ("region", "region"),
)

_CANDY_RANKINGS_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("competitorname", "text"),
    ("chocolate", "chocolate"),
    ("fruity", "chocolate"),
    ("caramel", "chocolate"),
    ("peanutyalmondy", "chocolate"),
    ("nougat", "chocolate"),
    ("crispedricewafer", "chocolate"),
    ("hard", "chocolate"),
    ("bar", "chocolate"),
    ("pluribus", "chocolate"),
    ("sugarpercent", "d"),
    ("pricepercent", "d"),
    ("winpercent", "target"),
)

_CAR_SEAT_SALES_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("sales", "target"),
    ("compprice", "i"),
    ("income", "i"),
    ("advertising", "i"),
    ("population", "i"),
    ("price", "i"),
    ("shelveloc", "shelveloc"),
    ("age", "i"),
    ("education", "i"),
    ("urban", "urban"),
    ("us", "urban"),
)

_CARD_DEFAULT_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("default", "label"),
    ("student", "student"),
    ("balance", "d"),
    ("income", "d"),
)

_CAT_HEARTS_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("sex", "label"),
    ("bwt", "d"),
    ("hwt", "d"),
)

_CHICAGO_TAXI_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("tip", "label"),
    ("distance", "d"),
    ("company", "company"),
    ("local", "local"),
    ("dow", "dow"),
    ("month", "month"),
    ("hour", "i"),
)

_CHICK_WEIGHTS_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("weight", "target"),
    ("time", "i"),
    ("chick", "i"),
    ("diet", "i"),
)

_CHILE_PLEBISCITE_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("region", "region"),
    ("population", "i"),
    ("sex", "sex"),
    ("age", "i"),
    ("education", "education"),
    ("income", "i"),
    ("statusquo", "d"),
    ("vote", "label"),
)

_CHOCOLATE_CAKES_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("replicate", "i"),
    ("recipe", "recipe"),
    ("temperature", "i"),
    ("angle", "target"),
    ("temp", "i"),
)

_COLLEGE_DISTANCE_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("gender", "gender"),
    ("ethnicity", "ethnicity"),
    ("score", "target"),
    ("fcollege", "fcollege"),
    ("mcollege", "fcollege"),
    ("home", "fcollege"),
    ("urban", "fcollege"),
    ("unemp", "d"),
    ("wage", "d"),
    ("distance", "d"),
    ("tuition", "d"),
    ("education", "i"),
    ("income", "income"),
    ("region", "region"),
)

_COLLEGE_MAJORS_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("rank", "i"),
    ("major_code", "i"),
    ("major", "text"),
    ("major_category", "major_category"),
    ("total", "i"),
    ("sample_size", "i"),
    ("men", "i"),
    ("women", "i"),
    ("sharewomen", "d"),
    ("employed", "i"),
    ("employed_fulltime", "i"),
    ("employed_parttime", "i"),
    ("employed_fulltime_yearround", "i"),
    ("unemployed", "i"),
    ("unemployment_rate", "d"),
    ("p25th", "i"),
    ("median", "target"),
    ("p75th", "i"),
    ("college_jobs", "i"),
    ("non_college_jobs", "i"),
    ("low_wage_jobs", "i"),
)

_COLON_CANCER_TRIAL_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("id", "i"),
    ("study", "i"),
    ("rx", "label"),
    ("sex", "i"),
    ("age", "i"),
    ("obstruct", "i"),
    ("perfor", "i"),
    ("adhere", "i"),
    ("nodes", "i"),
    ("status", "i"),
    ("differ", "i"),
    ("extent", "i"),
    ("surg", "i"),
    ("node4", "i"),
    ("time", "i"),
    ("etype", "i"),
)

_COMMERCIAL_OILS_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("palmitic", "d"),
    ("stearic", "d"),
    ("oleic", "d"),
    ("linoleic", "d"),
    ("linolenic", "d"),
    ("eicosanoic", "d"),
    ("eicosenoic", "d"),
    ("class", "label"),
)

_CONGRESS_AGE_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("congress", "i"),
    ("chamber", "chamber"),
    ("bioguide", "text"),
    ("firstname", "text"),
    ("middlename", "text"),
    ("lastname", "text"),
    ("suffix", "suffix"),
    ("birthday", "date"),
    ("state", "text"),
    ("party", "party"),
    ("incumbent", "incumbent"),
    ("termstart", "date"),
    ("age", "target"),
)

_COW_MILK_PROTEIN_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("protein", "target"),
    ("time", "i"),
    ("cow", "text"),
    ("diet", "diet"),
)

_CPS_WAGES_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("wage", "target"),
    ("education", "i"),
    ("experience", "i"),
    ("age", "i"),
    ("ethnicity", "ethnicity"),
    ("region", "region"),
    ("gender", "gender"),
    ("occupation", "occupation"),
    ("sector", "sector"),
    ("union", "union"),
    ("married", "union"),
)

_CREDIT_CARD_APPLICATIONS_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("card", "label"),
    ("reports", "i"),
    ("age", "d"),
    ("income", "d"),
    ("share", "d"),
    ("expenditure", "d"),
    ("owner", "owner"),
    ("selfemp", "owner"),
    ("dependents", "i"),
    ("months", "i"),
    ("majorcards", "i"),
    ("active", "i"),
)

_CREDIT_CARD_BALANCE_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("id", "i"),
    ("income", "d"),
    ("limit", "i"),
    ("rating", "i"),
    ("cards", "i"),
    ("age", "i"),
    ("education", "i"),
    ("gender", "gender"),
    ("student", "student"),
    ("married", "student"),
    ("ethnicity", "ethnicity"),
    ("balance", "target"),
)

_DEVELOPER_SURVEY_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("country", "country"),
    ("salary", "target"),
    ("yearscodedjob", "i"),
    ("opensource", "i"),
    ("hobby", "i"),
    ("companysizenumber", "i"),
    ("remote", "remote"),
    ("careersatisfaction", "i"),
    ("data_scientist", "i"),
    ("database_administrator", "i"),
    ("desktop_applications_developer", "i"),
    ("developer_with_stats_math_background", "i"),
    ("devops", "i"),
    ("embedded_developer", "i"),
    ("graphic_designer", "i"),
    ("graphics_programming", "i"),
    ("machine_learning_specialist", "i"),
    ("mobile_developer", "i"),
    ("quality_assurance_engineer", "i"),
    ("systems_administrator", "i"),
    ("web_developer", "i"),
)

_DIAMONDS_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("carat", "d"),
    ("cut", "cut"),
    ("color", "color"),
    ("clarity", "clarity"),
    ("depth", "d"),
    ("table", "d"),
    ("price", "target"),
    ("x", "d"),
    ("y", "d"),
    ("z", "d"),
)

_DNASE_ASSAY_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("run", "i"),
    ("conc", "d"),
    ("density", "target"),
)

_DOCTOR_VISITS_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("visits", "target"),
    ("gender", "gender"),
    ("age", "d"),
    ("income", "d"),
    ("illness", "i"),
    ("reduced", "i"),
    ("health", "i"),
    ("private", "private"),
    ("freepoor", "private"),
    ("freerepat", "private"),
    ("nchronic", "private"),
    ("lchronic", "private"),
)

_DOCTORAL_PUBLICATIONS_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("articles", "target"),
    ("gender", "gender"),
    ("married", "married"),
    ("kids", "i"),
    ("prestige", "d"),
    ("mentor", "i"),
)

_EARTHQUAKE_INTENSITY_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("quake", "i"),
    ("richter", "d"),
    ("distance", "d"),
    ("soil", "i"),
    ("accel", "target"),
)

_ECONOMIC_GROWTH_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("oil", "oil"),
    ("inter", "oil"),
    ("oecd", "oil"),
    ("gdp60", "i"),
    ("gdp85", "i"),
    ("gdpgrowth", "target"),
    ("popgrowth", "d"),
    ("invest", "d"),
    ("school", "d"),
    ("literacy60", "i"),
)

_ECONOMICS_JOURNALS_FIELDS: tuple[tuple[str, str], ...] = (
    ("name", "text"),
    ("title", "text"),
    ("publisher", "text"),
    ("society", "society"),
    ("price", "i"),
    ("pages", "i"),
    ("charpp", "i"),
    ("citations", "i"),
    ("foundingyear", "i"),
    ("subs", "target"),
    ("field", "field"),
)

_EMAIL_SPAM_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("spam", "label"),
    ("to_multiple", "i"),
    ("from", "i"),
    ("cc", "i"),
    ("sent_email", "i"),
    ("time", "time"),
    ("image", "i"),
    ("attach", "i"),
    ("dollar", "i"),
    ("winner", "winner"),
    ("inherit", "i"),
    ("viagra", "i"),
    ("password", "i"),
    ("num_char", "d"),
    ("line_breaks", "i"),
    ("format", "i"),
    ("re_subj", "i"),
    ("exclaim_subj", "i"),
    ("urgent_subj", "i"),
    ("exclaim_mess", "i"),
    ("number", "number"),
)

_EPILEPSY_SEIZURES_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("y", "target"),
    ("trt", "trt"),
    ("base", "i"),
    ("age", "i"),
    ("v4", "i"),
    ("subject", "i"),
    ("period", "i"),
    ("lbase", "d"),
    ("lage", "d"),
)

_EXERCISE_HISTORIES_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("subject", "text"),
    ("age", "d"),
    ("exercise", "d"),
    ("group", "label"),
)

_EXTRAMARITAL_AFFAIRS_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("affairs", "target"),
    ("gender", "gender"),
    ("age", "d"),
    ("yearsmarried", "d"),
    ("children", "children"),
    ("religiousness", "i"),
    ("education", "i"),
    ("occupation", "i"),
    ("rating", "i"),
)

_FANDANGO_RATINGS_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("film", "text"),
    ("year", "i"),
    ("rottentomatoes", "i"),
    ("rottentomatoes_user", "i"),
    ("metacritic", "i"),
    ("metacritic_user", "d"),
    ("imdb", "d"),
    ("fandango_stars", "target"),
    ("fandango_ratingvalue", "d"),
    ("rt_norm", "d"),
    ("rt_user_norm", "d"),
    ("metacritic_norm", "d"),
    ("metacritic_user_nom", "d"),
    ("imdb_norm", "d"),
    ("rt_norm_round", "d"),
    ("rt_user_norm_round", "d"),
    ("metacritic_norm_round", "d"),
    ("metacritic_user_norm_round", "d"),
    ("imdb_norm_round", "d"),
    ("metacritic_user_vote_count", "i"),
    ("imdb_user_vote_count", "i"),
    ("fandango_votes", "i"),
    ("fandango_difference", "d"),
)

_FAST_FOOD_NUTRITION_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("restaurant", "restaurant"),
    ("item", "text"),
    ("calories", "target"),
    ("cal_fat", "i"),
    ("total_fat", "i"),
    ("sat_fat", "d"),
    ("trans_fat", "d"),
    ("cholesterol", "i"),
    ("sodium", "i"),
    ("total_carb", "i"),
    ("fiber", "i"),
    ("sugar", "i"),
    ("protein", "i"),
    ("vit_a", "i"),
    ("vit_c", "i"),
    ("calcium", "i"),
    ("salad", "salad"),
)

_FATTY_LIVER_DISEASE_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("id", "i"),
    ("age", "i"),
    ("male", "i"),
    ("weight", "d"),
    ("height", "i"),
    ("bmi", "target"),
    ("case_id", "i"),
    ("futime", "i"),
    ("status", "i"),
)

_FERTILITY_LABOUR_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("morekids", "label"),
    ("gender1", "gender1"),
    ("gender2", "gender1"),
    ("age", "i"),
    ("afam", "afam"),
    ("hispanic", "afam"),
    ("other", "afam"),
    ("work", "i"),
)

_FIJI_EARTHQUAKES_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("lat", "d"),
    ("long", "d"),
    ("depth", "i"),
    ("mag", "target"),
    ("stations", "i"),
)

_FLORIDA_2000_VOTE_FIELDS: tuple[tuple[str, str], ...] = (
    ("name", "text"),
    ("gore", "i"),
    ("bush", "i"),
    ("buchanan", "target"),
    ("nader", "i"),
    ("browne", "i"),
    ("hagelin", "i"),
    ("harris", "i"),
    ("mcreynolds", "i"),
    ("moorehead", "i"),
    ("phillips", "i"),
    ("total", "i"),
)

_FLYING_ETIQUETTE_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("respondent_id", "d"),
    ("gender", "gender"),
    ("age", "age"),
    ("height", "height"),
    ("children_under_18", "children_under_18"),
    ("household_income", "household_income"),
    ("education", "education"),
    ("location", "location"),
    ("frequency", "frequency"),
    ("recline_frequency", "recline_frequency"),
    ("recline_obligation", "children_under_18"),
    ("recline_rude", "label"),
    ("recline_eliminate", "children_under_18"),
    ("switch_seats_friends", "switch_seats_friends"),
    ("switch_seats_family", "switch_seats_friends"),
    ("wake_up_bathroom", "switch_seats_friends"),
    ("wake_up_walk", "switch_seats_friends"),
    ("baby", "switch_seats_friends"),
    ("unruly_child", "switch_seats_friends"),
    ("two_arm_rests", "text"),
    ("middle_arm_rest", "text"),
    ("shade", "text"),
    ("unsold_seat", "switch_seats_friends"),
    ("talk_stranger", "switch_seats_friends"),
    ("get_up", "get_up"),
    ("electronics", "children_under_18"),
    ("smoked", "children_under_18"),
)

_FREE_LIGHT_CHAIN_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("age", "i"),
    ("sex", "label"),
    ("sample_yr", "i"),
    ("kappa", "d"),
    ("lambda", "d"),
    ("flc_grp", "i"),
    ("creatinine", "d"),
    ("mgus", "i"),
    ("futime", "i"),
    ("death", "i"),
    ("chapter", "chapter"),
)

_FUEL_ECONOMY_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("manufacturer", "manufacturer"),
    ("model", "text"),
    ("displ", "d"),
    ("year", "i"),
    ("cyl", "i"),
    ("trans", "trans"),
    ("drv", "drv"),
    ("cty", "i"),
    ("hwy", "target"),
    ("fl", "fl"),
    ("class", "class"),
)

_GALTON_HEIGHTS_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("family", "text"),
    ("father", "d"),
    ("mother", "d"),
    ("sex", "sex"),
    ("height", "target"),
    ("nkids", "i"),
)

_GAPMINDER_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("country", "text"),
    ("continent", "continent"),
    ("year", "i"),
    ("lifeexp", "target"),
    ("pop", "i"),
    ("gdppercap", "d"),
)

_GESTATION_BIRTHS_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("id", "i"),
    ("plurality", "plurality"),
    ("outcome", "outcome"),
    ("date", "date"),
    ("gestation", "i"),
    ("sex", "sex"),
    ("wt", "target"),
    ("parity", "i"),
    ("race", "race"),
    ("age", "i"),
    ("ed", "ed"),
    ("ht", "i"),
    ("wt_1", "i"),
    ("drace", "drace"),
    ("dage", "i"),
    ("ded", "ed"),
    ("dht", "i"),
    ("dwt", "i"),
    ("marital", "marital"),
    ("inc", "inc"),
    ("smoke", "smoke"),
    ("time", "time_code"),
    ("number", "number"),
)

_GRANULOMATOUS_DISEASE_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("id", "i"),
    ("center", "center"),
    ("random", "date"),
    ("treat", "label"),
    ("sex", "sex"),
    ("age", "i"),
    ("height", "d"),
    ("weight", "d"),
    ("inherit", "inherit"),
    ("steroids", "i"),
    ("propylac", "i"),
    ("hos_cat", "hos_cat"),
    ("tstart", "i"),
    ("enum", "i"),
    ("tstop", "i"),
    ("status", "i"),
)

_GREENHOUSE_GASES_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("year", "i"),
    ("gas", "label"),
    ("concentration", "d"),
)

_GROUSE_TICKS_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("index_", "i"),
    ("ticks", "target"),
    ("brood", "i"),
    ("height", "i"),
    ("year", "i"),
    ("location", "i"),
    ("cheight", "d"),
)

_GUNS_AND_CRIME_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("year", "i"),
    ("violent", "d"),
    ("murder", "d"),
    ("robbery", "d"),
    ("prisoners", "i"),
    ("afam", "d"),
    ("cauc", "d"),
    ("male", "d"),
    ("population", "d"),
    ("income", "d"),
    ("density", "d"),
    ("state", "text"),
    ("law", "label"),
)

_HATE_CRIMES_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("state", "text"),
    ("state_abbrev", "text"),
    ("median_house_inc", "i"),
    ("share_unemp_seas", "d"),
    ("share_pop_metro", "d"),
    ("share_pop_hs", "d"),
    ("share_non_citizen", "d"),
    ("share_white_poverty", "d"),
    ("gini_index", "d"),
    ("share_non_white", "d"),
    ("share_vote_trump", "d"),
    ("hate_crimes_per_100k_splc", "d"),
    ("avg_hatecrimes_per_100k_fbi", "target"),
)

_HELP_STUDY_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("age", "i"),
    ("anysubstatus", "i"),
    ("anysub", "anysub"),
    ("cesd", "i"),
    ("d1", "i"),
    ("daysanysub", "i"),
    ("dayslink", "i"),
    ("drugrisk", "i"),
    ("e2b", "i"),
    ("female", "i"),
    ("sex", "sex"),
    ("g1b", "anysub"),
    ("homeless", "homeless"),
    ("i1", "i"),
    ("i2", "i"),
    ("id", "i"),
    ("indtot", "i"),
    ("linkstatus", "i"),
    ("link", "anysub"),
    ("mcs", "d"),
    ("pcs", "d"),
    ("pss_fr", "i"),
    ("racegrp", "racegrp"),
    ("satreat", "anysub"),
    ("sexrisk", "i"),
    ("substance", "label"),
    ("treat", "anysub"),
    ("avg_drinks", "i"),
    ("max_drinks", "i"),
    ("hospitalizations", "i"),
)

_HIGH_SCHOOL_AND_BEYOND_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("id", "i"),
    ("gender", "gender"),
    ("race", "race"),
    ("ses", "ses"),
    ("schtyp", "schtyp"),
    ("prog", "label"),
    ("read", "i"),
    ("write", "i"),
    ("math", "i"),
    ("science", "i"),
    ("socst", "i"),
)

_HISTORIC_CO2_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("year", "i"),
    ("co2", "d"),
    ("source", "label"),
)

_HPC_JOBS_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("protocol", "protocol"),
    ("compounds", "i"),
    ("input_fields", "i"),
    ("iterations", "i"),
    ("num_pending", "i"),
    ("hour", "d"),
    ("day", "day"),
    ("class", "label"),
)

_INFANT_MORTALITY_FIELDS: tuple[tuple[str, str], ...] = (
    ("name", "text"),
    ("income", "i"),
    ("infant", "target"),
    ("region", "region"),
    ("oil", "oil"),
)

_INFERTILITY_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("education", "education"),
    ("age", "i"),
    ("parity", "i"),
    ("induced", "i"),
    ("case", "label"),
    ("spontaneous", "i"),
    ("stratum", "i"),
    ("pooled_stratum", "i"),
)

_INSECT_SPRAYS_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("count", "i"),
    ("spray", "label"),
)

_ITALIAN_OLIVE_OILS_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("region", "label"),
    ("area", "area"),
    ("palmitic", "d"),
    ("palmitoleic", "d"),
    ("stearic", "d"),
    ("oleic", "d"),
    ("linoleic", "d"),
    ("linolenic", "d"),
    ("arachidic", "d"),
    ("eicosenoic", "d"),
)

_LECTURE_RATINGS_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("s", "i"),
    ("d", "i"),
    ("studage", "i"),
    ("lectage", "i"),
    ("service", "i"),
    ("dept", "i"),
    ("y", "target"),
)

_LENDING_CLUB_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("funded_amnt", "i"),
    ("term", "term"),
    ("int_rate", "d"),
    ("sub_grade", "text"),
    ("addr_state", "text"),
    ("verification_status", "verification_status"),
    ("annual_inc", "d"),
    ("emp_length", "emp_length"),
    ("delinq_2yrs", "i"),
    ("inq_last_6mths", "i"),
    ("revol_util", "d"),
    ("acc_now_delinq", "i"),
    ("open_il_6m", "i"),
    ("open_il_12m", "i"),
    ("open_il_24m", "i"),
    ("total_bal_il", "i"),
    ("all_util", "i"),
    ("inq_fi", "i"),
    ("inq_last_12m", "i"),
    ("delinq_amnt", "i"),
    ("num_il_tl", "i"),
    ("total_il_high_credit_limit", "i"),
    ("class", "label"),
)

_LEPTOGRAPSUS_CRABS_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("sp", "label"),
    ("sex", "sex"),
    ("index_", "i"),
    ("fl", "d"),
    ("rw", "d"),
    ("cl", "d"),
    ("cw", "d"),
    ("bd", "d"),
)

_LIFE_CYCLE_SAVINGS_FIELDS: tuple[tuple[str, str], ...] = (
    ("name", "text"),
    ("sr", "target"),
    ("pop15", "d"),
    ("pop75", "d"),
    ("dpi", "d"),
    ("ddpi", "d"),
)

_LIVER_TRANSPLANT_LIST_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("age", "i"),
    ("sex", "sex"),
    ("abo", "abo"),
    ("year", "i"),
    ("futime", "i"),
    ("event", "label"),
)

_LOBLOLLY_PINES_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("height", "target"),
    ("age", "i"),
    ("seed", "i"),
)

_LOW_BIRTH_WEIGHT_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("low", "i"),
    ("age", "i"),
    ("lwt", "i"),
    ("race", "i"),
    ("smoke", "i"),
    ("ptl", "i"),
    ("ht", "i"),
    ("ui", "i"),
    ("ftv", "i"),
    ("bwt", "target"),
)

_LUNG_CANCER_SURVIVAL_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("inst", "i"),
    ("time", "target"),
    ("status", "i"),
    ("age", "i"),
    ("sex", "i"),
    ("ph_ecog", "i"),
    ("ph_karno", "i"),
    ("pat_karno", "i"),
    ("meal_cal", "i"),
    ("wt_loss", "i"),
)

_MAMMAL_SLEEP_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("name", "text"),
    ("genus", "text"),
    ("vore", "vore"),
    ("order", "order"),
    ("conservation", "conservation"),
    ("sleep_total", "target"),
    ("sleep_rem", "d"),
    ("sleep_cycle", "d"),
    ("awake", "d"),
    ("brainwt", "d"),
    ("bodywt", "d"),
)

_MARIJUANA_ARRESTS_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("released", "label"),
    ("colour", "colour"),
    ("year", "i"),
    ("age", "i"),
    ("sex", "sex"),
    ("employed", "employed"),
    ("citizen", "employed"),
    ("checks", "i"),
)

_MARRIAGE_LICENCES_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("bookpageid", "text"),
    ("appdate", "date"),
    ("ceremonydate", "date"),
    ("delay", "i"),
    ("officialtitle", "officialtitle"),
    ("person", "label"),
    ("dob", "date"),
    ("age", "d"),
    ("race", "race"),
    ("prevcount", "i"),
    ("prevconc", "prevconc"),
    ("hs", "i"),
    ("college", "i"),
    ("dayofbirth", "i"),
    ("sign", "sign"),
)

_MATH_ACHIEVEMENT_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("school", "i"),
    ("minority", "minority"),
    ("sex", "sex"),
    ("ses", "d"),
    ("mathach", "target"),
    ("meanses", "d"),
)

_MEDICAL_CARE_DEMAND_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("visits", "target"),
    ("nvisits", "i"),
    ("ovisits", "i"),
    ("novisits", "i"),
    ("emergency", "i"),
    ("hospital", "i"),
    ("health", "health"),
    ("chronic", "i"),
    ("adl", "adl"),
    ("region", "region"),
    ("age", "d"),
    ("afam", "afam"),
    ("gender", "gender"),
    ("married", "afam"),
    ("school", "i"),
    ("income", "d"),
    ("employed", "afam"),
    ("insurance", "afam"),
    ("medicaid", "afam"),
)

_MID_ATLANTIC_WAGES_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("year", "i"),
    ("age", "i"),
    ("maritl", "maritl"),
    ("race", "race"),
    ("education", "education"),
    ("region", "region"),
    ("jobclass", "jobclass"),
    ("health", "health"),
    ("health_ins", "health_ins"),
    ("logwage", "d"),
    ("wage", "target"),
)

_MIDWEST_COUNTIES_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("pid", "i"),
    ("county", "text"),
    ("state", "state"),
    ("area", "d"),
    ("poptotal", "i"),
    ("popdensity", "d"),
    ("popwhite", "i"),
    ("popblack", "i"),
    ("popamerindian", "i"),
    ("popasian", "i"),
    ("popother", "i"),
    ("percwhite", "d"),
    ("percblack", "d"),
    ("percamerindan", "d"),
    ("percasian", "d"),
    ("percother", "d"),
    ("popadults", "i"),
    ("perchsd", "d"),
    ("percollege", "d"),
    ("percprof", "d"),
    ("poppovertyknown", "i"),
    ("percpovertyknown", "d"),
    ("percbelowpoverty", "target"),
    ("percchildbelowpovert", "d"),
    ("percadultpoverty", "d"),
    ("percelderlypoverty", "d"),
    ("inmetro", "i"),
    ("category", "category"),
)

_MONOCLONAL_GAMMOPATHY_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("id", "i"),
    ("age", "i"),
    ("sex", "sex"),
    ("dxyr", "i"),
    ("hgb", "d"),
    ("creat", "d"),
    ("mspike", "d"),
    ("ptime", "i"),
    ("pstat", "i"),
    ("futime", "target"),
    ("death", "i"),
)

_MORTGAGE_DENIAL_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("deny", "label"),
    ("pirat", "d"),
    ("hirat", "d"),
    ("lvrat", "d"),
    ("chist", "i"),
    ("mhist", "i"),
    ("phist", "phist"),
    ("unemp", "d"),
    ("selfemp", "phist"),
    ("insurance", "phist"),
    ("condomin", "phist"),
    ("afam", "phist"),
    ("single", "phist"),
    ("hschool", "phist"),
)

_MOTOR_TREND_CARS_FIELDS: tuple[tuple[str, str], ...] = (
    ("name", "text"),
    ("mpg", "target"),
    ("cyl", "i"),
    ("disp", "d"),
    ("hp", "i"),
    ("drat", "d"),
    ("wt", "d"),
    ("qsec", "d"),
    ("vs", "i"),
    ("am", "i"),
    ("gear", "i"),
    ("carb", "i"),
)

_MOVIELENS_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("movieid", "i"),
    ("title", "text"),
    ("year", "i"),
    ("genres", "text"),
    ("userid", "i"),
    ("rating", "target"),
    ("timestamp", "i"),
)

_NEW_YORK_AIR_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("ozone", "target"),
    ("solar_r", "i"),
    ("wind", "d"),
    ("temp", "i"),
    ("month", "i"),
    ("day", "i"),
)

_NYC_FLIGHTS_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("year", "i"),
    ("month", "i"),
    ("day", "i"),
    ("dep_time", "i"),
    ("sched_dep_time", "i"),
    ("dep_delay", "i"),
    ("arr_time", "i"),
    ("sched_arr_time", "i"),
    ("arr_delay", "target"),
    ("carrier", "carrier"),
    ("flight", "i"),
    ("tailnum", "text"),
    ("origin", "origin"),
    ("dest", "text"),
    ("air_time", "i"),
    ("distance", "i"),
    ("hour", "i"),
    ("minute", "i"),
    ("time_hour", "time"),
)

_NYC_WEATHER_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("origin", "origin"),
    ("year", "i"),
    ("month", "i"),
    ("day", "i"),
    ("hour", "i"),
    ("temp", "target"),
    ("dewp", "d"),
    ("humid", "d"),
    ("wind_dir", "i"),
    ("wind_speed", "d"),
    ("wind_gust", "d"),
    ("precip", "d"),
    ("pressure", "d"),
    ("visib", "d"),
    ("time_hour", "time"),
)

_OCCUPATIONAL_PRESTIGE_FIELDS: tuple[tuple[str, str], ...] = (
    ("name", "text"),
    ("education", "d"),
    ("income", "i"),
    ("women", "d"),
    ("prestige", "target"),
    ("census", "i"),
    ("type", "type"),
)

_OESOPHAGEAL_CANCER_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("agegp", "agegp"),
    ("alcgp", "alcgp"),
    ("tobgp", "tobgp"),
    ("ncases", "target"),
    ("ncontrols", "i"),
)

_OLD_FAITHFUL_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("eruptions", "d"),
    ("waiting", "target"),
)

_ORANGE_JUICE_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("purchase", "label"),
    ("weekofpurchase", "i"),
    ("storeid", "i"),
    ("pricech", "d"),
    ("pricemm", "d"),
    ("discch", "d"),
    ("discmm", "d"),
    ("specialch", "i"),
    ("specialmm", "i"),
    ("loyalch", "d"),
    ("salepricemm", "d"),
    ("salepricech", "d"),
    ("pricediff", "d"),
    ("store7", "store7"),
    ("pctdiscmm", "d"),
    ("pctdiscch", "d"),
    ("listpricediff", "d"),
    ("store", "i"),
)

_ORANGE_TREES_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("tree", "i"),
    ("age", "i"),
    ("circumference", "target"),
)

_ORCHARD_SPRAYS_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("decrease", "i"),
    ("rowpos", "i"),
    ("colpos", "i"),
    ("treatment", "label"),
)

_ORTHODONTIC_GROWTH_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("distance", "target"),
    ("age", "i"),
    ("subject", "text"),
    ("sex", "sex"),
)

_OXFORD_BOYS_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("subject", "i"),
    ("age", "d"),
    ("height", "target"),
    ("occasion", "i"),
)

_PENICILLIN_TESTING_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("diameter", "target"),
    ("plate", "plate"),
    ("sample", "sample"),
)

_PETROLEUM_ROCK_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("area", "i"),
    ("peri", "d"),
    ("shape", "d"),
    ("perm", "target"),
)

_PHENOBARBITAL_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("subject", "i"),
    ("wt", "d"),
    ("apgar", "i"),
    ("apgarind", "apgarind"),
    ("time", "d"),
    ("dose", "d"),
    ("conc", "target"),
)

_PIMA_DIABETES_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("npreg", "i"),
    ("glu", "i"),
    ("bp", "i"),
    ("skin", "i"),
    ("bmi", "d"),
    ("ped", "d"),
    ("age", "i"),
    ("type", "label"),
)

_PROFESSOR_SALARIES_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("rank", "rank"),
    ("discipline", "discipline"),
    ("yrs_since_phd", "i"),
    ("yrs_service", "i"),
    ("sex", "sex"),
    ("salary", "target"),
)

_PSID_LABOUR_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("participation", "label"),
    ("hours", "i"),
    ("youngkids", "i"),
    ("oldkids", "i"),
    ("age", "i"),
    ("education", "i"),
    ("wage", "d"),
    ("repwage", "d"),
    ("hhours", "i"),
    ("hage", "i"),
    ("heducation", "i"),
    ("hwage", "d"),
    ("fincome", "i"),
    ("tax", "d"),
    ("meducation", "i"),
    ("feducation", "i"),
    ("unemp", "d"),
    ("city", "city"),
    ("experience", "i"),
    ("college", "city"),
    ("hcollege", "city"),
)

_REPORTED_WEIGHT_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("sex", "label"),
    ("weight", "i"),
    ("height", "i"),
    ("repwt", "i"),
    ("repht", "i"),
)

_RESUME_CALLBACKS_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("job_ad_id", "i"),
    ("job_city", "job_city"),
    ("job_industry", "job_industry"),
    ("job_type", "job_type"),
    ("job_fed_contractor", "i"),
    ("job_equal_opp_employer", "i"),
    ("job_ownership", "job_ownership"),
    ("job_req_any", "i"),
    ("job_req_communication", "i"),
    ("job_req_education", "i"),
    ("job_req_min_experience", "job_req_min_experience"),
    ("job_req_computer", "i"),
    ("job_req_organization", "i"),
    ("job_req_school", "job_req_school"),
    ("received_callback", "label"),
    ("firstname", "text"),
    ("race", "race"),
    ("gender", "gender"),
    ("years_college", "i"),
    ("college_degree", "i"),
    ("honors", "i"),
    ("worked_during_school", "i"),
    ("years_experience", "i"),
    ("computer_skills", "i"),
    ("special_skills", "i"),
    ("volunteer", "i"),
    ("military", "i"),
    ("employment_holes", "i"),
    ("has_email_address", "i"),
    ("resume_quality", "resume_quality"),
)

_RETINOPATHY_LASER_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("id", "i"),
    ("laser", "label"),
    ("eye", "eye"),
    ("age", "i"),
    ("type", "type"),
    ("trt", "i"),
    ("futime", "d"),
    ("status", "i"),
    ("risk", "i"),
)

_SAT_AND_GPA_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("sex", "i"),
    ("sat_v", "i"),
    ("sat_m", "i"),
    ("sat_sum", "i"),
    ("hs_gpa", "d"),
    ("fy_gpa", "target"),
)

_SCHOOL_ABSENCES_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("eth", "eth"),
    ("sex", "sex"),
    ("age", "age"),
    ("lrn", "lrn"),
    ("days", "target"),
)

_SEAT_BELT_LAWS_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("state", "text"),
    ("year", "i"),
    ("miles", "i"),
    ("fatalities", "d"),
    ("seatbelt", "d"),
    ("speed65", "speed65"),
    ("speed70", "speed65"),
    ("drinkage", "speed65"),
    ("alcohol", "speed65"),
    ("income", "i"),
    ("age", "d"),
    ("enforce", "label"),
)

_SEATTLE_PETS_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("license_issue_date", "date"),
    ("license_number", "text"),
    ("animal_name", "text"),
    ("species", "label"),
    ("primary_breed", "text"),
    ("secondary_breed", "text"),
    ("zip_code", "text"),
)

_SLEEP_DEPRIVATION_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("reaction", "target"),
    ("days", "i"),
    ("subject", "i"),
)

_SLID_WAGES_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("wages", "target"),
    ("education", "d"),
    ("age", "i"),
    ("sex", "sex"),
    ("language", "language"),
)

_SNAIL_MORTALITY_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("species", "species"),
    ("exposure", "i"),
    ("rel_hum", "d"),
    ("temp", "i"),
    ("deaths", "target"),
    ("n", "i"),
)

_SOYBEAN_GROWTH_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("plot", "text"),
    ("variety", "variety"),
    ("year", "i"),
    ("time", "i"),
    ("weight", "target"),
)

_SP500_DAILY_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("year", "i"),
    ("lag1", "d"),
    ("lag2", "d"),
    ("lag3", "d"),
    ("lag4", "d"),
    ("lag5", "d"),
    ("volume", "d"),
    ("today", "d"),
    ("direction", "label"),
)

_SP500_WEEKLY_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("year", "i"),
    ("lag1", "d"),
    ("lag2", "d"),
    ("lag3", "d"),
    ("lag4", "d"),
    ("lag5", "d"),
    ("volume", "d"),
    ("today", "d"),
    ("direction", "label"),
)

_SPRUCE_GROWTH_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("tree", "text"),
    ("days", "i"),
    ("logsize", "target"),
    ("plot", "i"),
)

_STANFORD_HEART_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("start", "d"),
    ("stop", "target"),
    ("event", "i"),
    ("age", "d"),
    ("year", "d"),
    ("surgery", "i"),
    ("transplant", "i"),
    ("id", "i"),
)

_STAR_PROPERTIES_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("star", "text"),
    ("magnitude", "d"),
    ("temp", "target"),
    ("type", "type"),
)

_STATE_SAT_SCORES_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("state", "text"),
    ("expend", "target"),
    ("ratio", "d"),
    ("salary", "d"),
    ("frac", "i"),
    ("verbal", "i"),
    ("math", "i"),
    ("sat", "i"),
)

_STEAK_PREFERENCES_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("respondent_id", "d"),
    ("lottery_a", "lottery_a"),
    ("smoke", "lottery_a"),
    ("alcohol", "lottery_a"),
    ("gamble", "lottery_a"),
    ("skydiving", "lottery_a"),
    ("speed", "lottery_a"),
    ("cheated", "lottery_a"),
    ("steak", "lottery_a"),
    ("steak_prep", "label"),
    ("female", "lottery_a"),
    ("age", "age"),
    ("hhold_income", "hhold_income"),
    ("educ", "educ"),
    ("region", "region"),
)

_STUDENT_SURVEY_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("sex", "sex"),
    ("wr_hnd", "d"),
    ("nw_hnd", "d"),
    ("w_hnd", "label"),
    ("fold", "fold"),
    ("pulse", "i"),
    ("clap", "clap"),
    ("exer", "exer"),
    ("smoke", "smoke"),
    ("height", "d"),
    ("m_i", "m_i"),
    ("age", "d"),
)

_SWISS_FERTILITY_FIELDS: tuple[tuple[str, str], ...] = (
    ("name", "text"),
    ("fertility", "target"),
    ("agriculture", "d"),
    ("examination", "i"),
    ("education", "i"),
    ("catholic", "d"),
    ("infant_mortality", "d"),
)

_SWISS_LABOUR_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("participation", "label"),
    ("income", "d"),
    ("age", "d"),
    ("education", "i"),
    ("youngkids", "i"),
    ("oldkids", "i"),
    ("foreign", "foreign"),
)

_TARANTINO_SCRIPTS_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("movie", "label"),
    ("profane", "profane"),
    ("word", "text"),
    ("minutes_in", "d"),
)

_TEACHING_EVALUATIONS_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("course_id", "i"),
    ("prof_id", "i"),
    ("score", "target"),
    ("rank", "rank"),
    ("ethnicity", "ethnicity"),
    ("gender", "gender"),
    ("language", "language"),
    ("age", "i"),
    ("cls_perc_eval", "d"),
    ("cls_did_eval", "i"),
    ("cls_students", "i"),
    ("cls_level", "cls_level"),
    ("cls_profs", "cls_profs"),
    ("cls_credits", "cls_credits"),
    ("bty_f1lower", "i"),
    ("bty_f1upper", "i"),
    ("bty_f2upper", "i"),
    ("bty_m1lower", "i"),
    ("bty_m1upper", "i"),
    ("bty_m2upper", "i"),
    ("bty_avg", "d"),
    ("pic_outfit", "pic_outfit"),
    ("pic_color", "pic_color"),
)

_TELECOM_CHURN_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("state", "text"),
    ("account_length", "i"),
    ("area_code", "area_code"),
    ("international_plan", "international_plan"),
    ("voice_mail_plan", "international_plan"),
    ("number_vmail_messages", "i"),
    ("total_day_minutes", "d"),
    ("total_day_calls", "i"),
    ("total_day_charge", "d"),
    ("total_eve_minutes", "d"),
    ("total_eve_calls", "i"),
    ("total_eve_charge", "d"),
    ("total_night_minutes", "d"),
    ("total_night_calls", "i"),
    ("total_night_charge", "d"),
    ("total_intl_minutes", "d"),
    ("total_intl_calls", "i"),
    ("total_intl_charge", "d"),
    ("number_customer_service_calls", "i"),
    ("churn", "label"),
)

_TELECOM_CONTRACTS_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("churn", "label"),
    ("female", "i"),
    ("senior_citizen", "i"),
    ("partner", "i"),
    ("dependents", "i"),
    ("tenure", "i"),
    ("phone_service", "i"),
    ("multiple_lines", "multiple_lines"),
    ("internet_service", "internet_service"),
    ("online_security", "online_security"),
    ("online_backup", "online_security"),
    ("device_protection", "online_security"),
    ("tech_support", "online_security"),
    ("streaming_tv", "online_security"),
    ("streaming_movies", "online_security"),
    ("contract", "contract"),
    ("paperless_billing", "i"),
    ("payment_method", "payment_method"),
    ("monthly_charges", "d"),
    ("total_charges", "d"),
)

_TEMPERATURE_AND_CARBON_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("year", "i"),
    ("temp_anomaly", "target"),
    ("land_anomaly", "d"),
    ("ocean_anomaly", "d"),
    ("carbon_emissions", "i"),
)

_TEN_MILE_RACE_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("state", "text"),
    ("time", "i"),
    ("net", "target"),
    ("age", "i"),
    ("sex", "sex"),
)

_TEXAS_HOUSING_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("city", "text"),
    ("year", "i"),
    ("month", "i"),
    ("sales", "i"),
    ("volume", "d"),
    ("median", "target"),
    ("listings", "i"),
    ("inventory", "d"),
    ("date", "d"),
)

_THEOPHYLLINE_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("subject", "i"),
    ("wt", "d"),
    ("dose", "d"),
    ("time", "d"),
    ("conc", "target"),
)

_TITANIC_FIELDS: tuple[tuple[str, str], ...] = (
    ("name", "text"),
    ("survived", "label"),
    ("sex", "sex"),
    ("age", "d"),
    ("passengerclass", "passengerclass"),
)

_TOOTH_GROWTH_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("len", "d"),
    ("supp", "label"),
    ("dose", "d"),
)

_TRAVEL_MODE_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("individual", "i"),
    ("mode", "label"),
    ("choice", "choice"),
    ("wait", "i"),
    ("vcost", "i"),
    ("travel", "i"),
    ("gcost", "i"),
    ("income", "i"),
    ("size", "i"),
)

_UK_SMOKING_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("gender", "gender"),
    ("age", "i"),
    ("marital_status", "marital_status"),
    ("highest_qualification", "highest_qualification"),
    ("nationality", "nationality"),
    ("ethnicity", "ethnicity"),
    ("gross_income", "gross_income"),
    ("region", "region"),
    ("smoke", "label"),
    ("amt_weekends", "i"),
    ("amt_weekdays", "i"),
    ("type", "type"),
)

_UN_NATIONAL_STATISTICS_FIELDS: tuple[tuple[str, str], ...] = (
    ("name", "text"),
    ("region", "region"),
    ("group", "group"),
    ("fertility", "d"),
    ("ppgdp", "d"),
    ("lifeexpf", "target"),
    ("pcturban", "i"),
    ("infantmortality", "d"),
)

_US_AIRCRAFT_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("tailnum", "text"),
    ("year", "i"),
    ("type", "type"),
    ("manufacturer", "text"),
    ("model", "text"),
    ("engines", "i"),
    ("seats", "target"),
    ("speed", "i"),
    ("engine", "engine"),
)

_US_AIRPORTS_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("faa", "text"),
    ("name", "text"),
    ("lat", "d"),
    ("lon", "d"),
    ("alt", "target"),
    ("tz", "i"),
    ("dst", "dst"),
    ("tzone", "tzone"),
)

_US_ARRESTS_FIELDS: tuple[tuple[str, str], ...] = (
    ("name", "text"),
    ("murder", "target"),
    ("assault", "i"),
    ("urbanpop", "i"),
    ("rape", "d"),
)

_US_BIRTHS_1978_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("date", "date"),
    ("births", "target"),
    ("wday", "wday"),
    ("year", "i"),
    ("month", "i"),
    ("day_of_year", "i"),
    ("day_of_month", "i"),
    ("day_of_week", "i"),
)

_US_BIRTHS_2014_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("fage", "i"),
    ("mage", "i"),
    ("mature", "mature"),
    ("weeks", "i"),
    ("premie", "premie"),
    ("visits", "i"),
    ("gained", "i"),
    ("weight", "target"),
    ("lowbirthweight", "lowbirthweight"),
    ("sex", "sex"),
    ("habit", "habit"),
    ("marital", "marital"),
    ("whitemom", "whitemom"),
)

_US_CEREALS_FIELDS: tuple[tuple[str, str], ...] = (
    ("name", "text"),
    ("mfr", "label"),
    ("calories", "d"),
    ("protein", "d"),
    ("fat", "d"),
    ("sodium", "d"),
    ("fibre", "d"),
    ("carbo", "d"),
    ("sugars", "d"),
    ("shelf", "i"),
    ("potassium", "d"),
    ("vitamins", "vitamins"),
)

_US_COLLEGES_FIELDS: tuple[tuple[str, str], ...] = (
    ("name", "text"),
    ("private", "label"),
    ("apps", "i"),
    ("accept", "i"),
    ("enroll", "i"),
    ("top10perc", "i"),
    ("top25perc", "i"),
    ("f_undergrad", "i"),
    ("p_undergrad", "i"),
    ("outstate", "i"),
    ("room_board", "i"),
    ("books", "i"),
    ("personal", "i"),
    ("phd", "i"),
    ("terminal", "i"),
    ("s_f_ratio", "d"),
    ("perc_alumni", "i"),
    ("expend", "i"),
    ("grad_rate", "i"),
)

_US_ECONOMICS_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("date", "date"),
    ("pce", "d"),
    ("pop", "d"),
    ("psavert", "d"),
    ("uempmed", "d"),
    ("unemploy", "target"),
)

_US_GUN_MURDERS_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("state", "text"),
    ("abb", "text"),
    ("region", "region"),
    ("population", "i"),
    ("total", "target"),
)

_US_STATE_EDUCATION_FIELDS: tuple[tuple[str, str], ...] = (
    ("name", "text"),
    ("region", "region"),
    ("pop", "i"),
    ("satv", "target"),
    ("satm", "target"),
    ("percent", "i"),
    ("dollars", "d"),
    ("pay", "i"),
)

_USED_CAR_PRICES_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("price", "target"),
    ("mileage", "i"),
    ("cylinder", "i"),
    ("doors", "i"),
    ("cruise", "i"),
    ("sound", "i"),
    ("leather", "i"),
    ("buick", "i"),
    ("cadillac", "i"),
    ("chevy", "i"),
    ("pontiac", "i"),
    ("saab", "i"),
    ("saturn", "i"),
    ("convertible", "i"),
    ("coupe", "i"),
    ("hatchback", "i"),
    ("sedan", "i"),
    ("wagon", "i"),
)

_UTILITY_BILLS_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("month", "i"),
    ("day", "i"),
    ("year", "i"),
    ("temp", "i"),
    ("kwh", "i"),
    ("ccf", "i"),
    ("thermsperday", "d"),
    ("billingdays", "i"),
    ("totalbill", "target"),
    ("gasbill", "d"),
    ("elecbill", "d"),
    ("notes", "text"),
)

_VERBAL_AGGRESSION_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("anger", "i"),
    ("gender", "gender"),
    ("item", "item"),
    ("resp", "label"),
    ("id", "i"),
    ("btype", "btype"),
    ("situ", "situ"),
    ("mode", "mode"),
    ("r2", "r2"),
)

_VOCABULARY_TEST_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("year", "i"),
    ("sex", "sex"),
    ("education", "i"),
    ("vocabulary", "target"),
)

_VOLUNTEERING_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("neuroticism", "i"),
    ("extraversion", "i"),
    ("sex", "sex"),
    ("volunteer", "label"),
)

_WARP_BREAKS_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("breaks", "target"),
    ("wool", "wool"),
    ("tension", "tension"),
)

_WHEAT_YIELD_TRIALS_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("block", "i"),
    ("variety", "text"),
    ("yield", "target"),
    ("latitude", "d"),
    ("longitude", "d"),
)

_WHICKHAM_SMOKING_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("outcome", "label"),
    ("smoker", "smoker"),
    ("age", "i"),
)

_WINDSOR_HOUSE_PRICES_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("price", "target"),
    ("lotsize", "i"),
    ("bedrooms", "i"),
    ("bathrooms", "i"),
    ("stories", "i"),
    ("driveway", "driveway"),
    ("recreation", "driveway"),
    ("fullbase", "driveway"),
    ("gasheat", "driveway"),
    ("aircon", "driveway"),
    ("garage", "i"),
    ("prefer", "driveway"),
)

_WOMENS_LABOUR_1975_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("lfp", "label"),
    ("k5", "i"),
    ("k618", "i"),
    ("age", "i"),
    ("wc", "wc"),
    ("hc", "wc"),
    ("lwg", "d"),
    ("inc", "d"),
)

_WORKPLACE_SMOKING_BAN_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("smoker", "label"),
    ("ban", "ban"),
    ("age", "i"),
    ("education", "education"),
    ("afam", "ban"),
    ("hispanic", "ban"),
    ("gender", "gender"),
)

_WORLD_VALUES_SURVEY_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("poverty", "label"),
    ("religion", "religion"),
    ("degree", "religion"),
    ("country", "country"),
    ("age", "i"),
    ("gender", "gender"),
)

_YOUTH_RISK_BEHAVIOUR_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("age", "i"),
    ("gender", "gender"),
    ("grade", "grade"),
    ("hispanic", "hispanic"),
    ("race", "text"),
    ("height", "d"),
    ("weight", "target"),
    ("helmet_12m", "helmet_12m"),
    ("text_while_driving_30d", "text_while_driving_30d"),
    ("physically_active_7d", "i"),
    ("hours_tv_per_school_day", "hours_tv_per_school_day"),
    ("strength_training_7d", "i"),
    ("school_night_hours_sleep", "school_night_hours_sleep"),
)

#: The sets below are the country-year tables Our World in Data charts, read
#: from the CSV each chart offers: a row for one place in one year, naming the
#: place, its three-letter code where it has one - a continent or an income
#: group has none - the year, and what was measured. The measured column is the
#: one to predict. Our World in Data publishes these under CC BY 4.0, and the
#: chart page named as the source says whose counting stands behind each.
_GDP_PER_CAPITA_WORLDBANK_FIELDS: tuple[tuple[str, str], ...] = (
    ("entity", "text"),
    ("code", "text"),
    ("year", "i"),
    ("gdp_per_capita", "target"),
    ("region", "text"),
)

_ELECTRICITY_ACCESS_FIELDS: tuple[tuple[str, str], ...] = (
    ("entity", "text"),
    ("code", "text"),
    ("year", "i"),
    ("share_with_electricity", "target"),
)

_INTERNET_USE_FIELDS: tuple[tuple[str, str], ...] = (
    ("entity", "text"),
    ("code", "text"),
    ("year", "i"),
    ("share_using_internet", "target"),
)

_CONSUMER_PRICE_INFLATION_FIELDS: tuple[tuple[str, str], ...] = (
    ("entity", "text"),
    ("code", "text"),
    ("year", "i"),
    ("inflation_percent", "target"),
)

_UNEMPLOYMENT_RATE_FIELDS: tuple[tuple[str, str], ...] = (
    ("entity", "text"),
    ("code", "text"),
    ("year", "i"),
    ("unemployment_percent", "target"),
)

_FRESHWATER_WITHDRAWALS_FIELDS: tuple[tuple[str, str], ...] = (
    ("entity", "text"),
    ("code", "text"),
    ("year", "i"),
    ("withdrawals_km3", "target"),
)

_AIR_PASSENGERS_FIELDS: tuple[tuple[str, str], ...] = (
    ("entity", "text"),
    ("code", "text"),
    ("year", "i"),
    ("passengers", "target"),
    ("region", "text"),
)

_MOBILE_SUBSCRIPTIONS_FIELDS: tuple[tuple[str, str], ...] = (
    ("entity", "text"),
    ("code", "text"),
    ("year", "i"),
    ("subscriptions_per_100", "target"),
)

_INTERNET_USERS_FIELDS: tuple[tuple[str, str], ...] = (
    ("entity", "text"),
    ("code", "text"),
    ("year", "i"),
    ("users", "target"),
)

_CO2_EMISSIONS_FIELDS: tuple[tuple[str, str], ...] = (
    ("entity", "text"),
    ("code", "text"),
    ("year", "i"),
    ("emissions_tonnes", "target"),
)

_CO2_EMISSIONS_PER_PERSON_FIELDS: tuple[tuple[str, str], ...] = (
    ("entity", "text"),
    ("code", "text"),
    ("year", "i"),
    ("tonnes_per_person", "target"),
)

_CUMULATIVE_CO2_EMISSIONS_FIELDS: tuple[tuple[str, str], ...] = (
    ("entity", "text"),
    ("code", "text"),
    ("year", "i"),
    ("emissions_tonnes", "target"),
)

_CO2_PER_DOLLAR_FIELDS: tuple[tuple[str, str], ...] = (
    ("entity", "text"),
    ("code", "text"),
    ("year", "i"),
    ("kg_per_dollar", "target"),
)

_RENEWABLE_ELECTRICITY_FIELDS: tuple[tuple[str, str], ...] = (
    ("entity", "text"),
    ("code", "text"),
    ("year", "i"),
    ("renewable_percent", "target"),
)

_ELECTRICITY_GENERATION_FIELDS: tuple[tuple[str, str], ...] = (
    ("entity", "text"),
    ("code", "text"),
    ("year", "i"),
    ("generation_twh", "target"),
)

_RENEWABLE_ENERGY_FIELDS: tuple[tuple[str, str], ...] = (
    ("entity", "text"),
    ("code", "text"),
    ("year", "i"),
    ("renewable_percent", "target"),
)

_ENERGY_USE_PER_PERSON_FIELDS: tuple[tuple[str, str], ...] = (
    ("entity", "text"),
    ("code", "text"),
    ("year", "i"),
    ("kwh_per_person", "target"),
)

_PRIMARY_ENERGY_FIELDS: tuple[tuple[str, str], ...] = (
    ("entity", "text"),
    ("code", "text"),
    ("year", "i"),
    ("energy_twh", "target"),
)

_CEREAL_YIELDS_FIELDS: tuple[tuple[str, str], ...] = (
    ("entity", "text"),
    ("code", "text"),
    ("year", "i"),
    ("tonnes_per_hectare", "target"),
)

_CALORIE_SUPPLY_FIELDS: tuple[tuple[str, str], ...] = (
    ("entity", "text"),
    ("code", "text"),
    ("year", "i"),
    ("kcal_per_day", "target"),
)

_GDP_PER_CAPITA_MADDISON_FIELDS: tuple[tuple[str, str], ...] = (
    ("entity", "text"),
    ("code", "text"),
    ("year", "i"),
    ("gdp_per_capita", "target"),
    ("note", "text"),
)

_POPULATION_DENSITY_FIELDS: tuple[tuple[str, str], ...] = (
    ("entity", "text"),
    ("code", "text"),
    ("year", "i"),
    ("people_per_km2", "target"),
)

_URBAN_POPULATION_FIELDS: tuple[tuple[str, str], ...] = (
    ("entity", "text"),
    ("code", "text"),
    ("year", "i"),
    ("urban_percent", "target"),
)

_LIFE_EXPECTANCY_AT_BIRTH_FIELDS: tuple[tuple[str, str], ...] = (
    ("entity", "text"),
    ("code", "text"),
    ("year", "i"),
    ("years", "target"),
)

_CHILD_MORTALITY_FIELDS: tuple[tuple[str, str], ...] = (
    ("entity", "text"),
    ("code", "text"),
    ("year", "i"),
    ("deaths_per_100_births", "target"),
)

_CHILD_MORTALITY_IGME_FIELDS: tuple[tuple[str, str], ...] = (
    ("entity", "text"),
    ("code", "text"),
    ("year", "i"),
    ("deaths_per_100_births", "target"),
)

_ADULT_LITERACY_FIELDS: tuple[tuple[str, str], ...] = (
    ("entity", "text"),
    ("code", "text"),
    ("year", "i"),
    ("literate_percent", "target"),
)

_HUMAN_DEVELOPMENT_INDEX_FIELDS: tuple[tuple[str, str], ...] = (
    ("entity", "text"),
    ("code", "text"),
    ("year", "i"),
    ("hdi", "target"),
    ("region", "text"),
)

#: The readings are quarterly and the file says so in its own terms - ``1880-Q2``
#: - which is not a date any format string parses, so the quarter is kept as the
#: text it is rather than invented into a day.
_SEA_LEVEL_FIELDS: tuple[tuple[str, str], ...] = (
    ("entity", "text"),
    ("code", "text"),
    ("quarter", "text"),
    ("church_and_white", "d"),
    ("uhslc", "d"),
    ("sea_level_mm", "target"),
)

#: A second shelf of the same Rdatasets kind, read the same way: the tables the
#: history of statistics was written from, the trials medicine is taught with,
#: what archaeologists dig up and measure, the sets built to show that a summary
#: hides the shape of the thing, and the series forecasting is practised on.
_ABORTION_AND_CRIME_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("fip", "i"),
    ("age", "i"),
    ("race", "i"),
    ("year", "i"),
    ("sex", "i"),
    ("totpop", "i"),
    ("ir", "d"),
    ("crack", "d"),
    ("alcohol", "d"),
    ("income", "i"),
    ("ur", "d"),
    ("poverty", "d"),
    ("repeal", "i"),
    ("acc", "d"),
    ("wht", "i"),
    ("male", "i"),
    ("lnr", "target"),
    ("t", "i"),
    ("younger", "i"),
    ("fa", "i"),
    ("pi", "i"),
    ("bf15", "i"),
)

_ADULT_SERVICES_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("id", "i"),
    ("session", "i"),
    ("age", "i"),
    ("age_cl", "d"),
    ("appearance_cl", "i"),
    ("bmi", "d"),
    ("schooling", "i"),
    ("asq_cl", "d"),
    ("provider_second", "i"),
    ("asian_cl", "i"),
    ("black_cl", "i"),
    ("hispanic_cl", "i"),
    ("othrace_cl", "i"),
    ("reg", "i"),
    ("hot", "i"),
    ("massage_cl", "i"),
    ("lnw", "target"),
    ("llength", "d"),
    ("unsafe", "i"),
    ("asian", "i"),
    ("black", "i"),
    ("hispanic", "i"),
    ("other", "i"),
    ("white", "i"),
    ("asq", "i"),
    ("cohab", "i"),
    ("married", "i"),
    ("divorced", "i"),
    ("separated", "i"),
    ("nevermarried", "i"),
    ("widowed", "i"),
)

_AFFAIR_COUNTS_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("naffairs", "target"),
    ("kids", "i"),
    ("vryunhap", "i"),
    ("unhap", "i"),
    ("avgmarr", "i"),
    ("hapavg", "i"),
    ("vryhap", "i"),
    ("antirel", "i"),
    ("notrel", "i"),
    ("slghtrel", "i"),
    ("smerel", "i"),
    ("vryrel", "i"),
    ("yrsmarr1", "i"),
    ("yrsmarr2", "i"),
    ("yrsmarr3", "i"),
    ("yrsmarr4", "i"),
    ("yrsmarr5", "i"),
    ("yrsmarr6", "i"),
)

_ALONE_EPISODES_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("version", "version"),
    ("season", "i"),
    ("episode_number_overall", "i"),
    ("episode", "i"),
    ("title", "text"),
    ("air_date", "date"),
    ("viewers", "d"),
    ("quote", "text"),
    ("author", "text"),
    ("imdb_rating", "target"),
    ("n_ratings", "i"),
    ("n_remaining", "i"),
    ("day_start", "i"),
    ("description", "text"),
)

_ALONE_LOADOUTS_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("version", "version"),
    ("season", "i"),
    ("id", "text"),
    ("name", "text"),
    ("item_number", "target"),
    ("item_detailed", "text"),
    ("item", "text"),
)

_ALONE_SEASONS_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("version", "version"),
    ("season", "i"),
    ("subtitle", "subtitle"),
    ("location", "location"),
    ("region", "region"),
    ("country", "country"),
    ("n_survivors", "target"),
    ("lat", "d"),
    ("date_drop_off", "date"),
)

_ALONE_SURVIVALISTS_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("version", "version"),
    ("season", "i"),
    ("id", "text"),
    ("name", "text"),
    ("first_name", "text"),
    ("last_name", "text"),
    ("age", "i"),
    ("gender", "gender"),
    ("city", "text"),
    ("state", "text"),
    ("country", "country"),
    ("result", "i"),
    ("days_lasted", "target"),
    ("medically_evacuated", "medically_evacuated"),
    ("reason_tapped_out", "text"),
    ("reason_category", "reason_category"),
    ("episode_tapped", "i"),
    ("team", "team"),
    ("day_linked_up", "i"),
    ("profession", "text"),
)

_ANCIENT_SHIPWRECKS_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("name", "text"),
    ("sea", "sea"),
    ("country", "country"),
    ("region", "text"),
    ("depth_min", "target"),
    ("depth_max", "i"),
    ("depth", "text"),
    ("period", "text"),
    ("dating", "text"),
    ("date_early", "i"),
    ("date_late", "i"),
    ("origin", "text"),
    ("destination", "text"),
)

_ANIMAL_ATTRIBUTES_FIELDS: tuple[tuple[str, str], ...] = (
    ("name", "text"),
    ("war", "target"),
    ("fly", "i"),
    ("ver", "i"),
    ("end", "i"),
    ("gro", "i"),
    ("hai", "i"),
)

_ANSCOMBE_QUARTET_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("dataset", "label"),
    ("x", "i"),
    ("y", "d"),
)

_ANSETT_PASSENGERS_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("week", "text"),
    ("airports", "airports"),
    ("class", "class"),
    ("passengers", "target"),
)

_ARBUTHNOT_CHRISTENINGS_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("year", "i"),
    ("males", "i"),
    ("females", "i"),
    ("plague", "i"),
    ("mortality", "i"),
    ("ratio", "target"),
    ("total", "d"),
)

_ARCTIC_PIT_HOUSES_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("hearths", "hearths"),
    ("depth", "depth"),
    ("size", "label"),
    ("form", "form"),
    ("orient", "orient"),
    ("entrance", "entrance"),
)

_ARIZONA_CARDIAC_STAYS_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("los", "target"),
    ("procedure", "i"),
    ("sex", "i"),
    ("age75", "i"),
    ("admit", "i"),
    ("hospital", "d"),
)

_ARTHRITIS_TREATMENT_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("id", "i"),
    ("treatment", "treatment"),
    ("sex", "sex"),
    ("age", "i"),
    ("improved", "label"),
)

_ASHKENAZI_BREAST_CANCER_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("famid", "i"),
    ("brcancer", "i"),
    ("age", "i"),
    ("mutant", "label"),
)

_ATMOSPHERIC_RADIOCARBON_FIELDS: tuple[tuple[str, str], ...] = (
    ("name", "text"),
    ("site", "label"),
    ("start", "date"),
    ("end", "date"),
    ("delta", "d"),
    ("sigma", "i"),
)

_AUSTRALIAN_CAR_POLICIES_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("veh_value", "d"),
    ("exposure", "d"),
    ("clm", "i"),
    ("numclaims", "i"),
    ("claimcst0", "target"),
    ("veh_body", "veh_body"),
    ("veh_age", "i"),
    ("gender", "gender"),
    ("area", "area"),
    ("agecat", "i"),
    ("x_obstat", "x_obstat"),
)

_AUSTRALIAN_LIVESTOCK_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("month", "text"),
    ("animal", "animal"),
    ("state", "state"),
    ("count", "target"),
)

_AUSTRALIAN_PRODUCTION_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("quarter", "text"),
    ("beer", "target"),
    ("tobacco", "i"),
    ("bricks", "i"),
    ("cement", "i"),
    ("electricity", "i"),
    ("gas", "i"),
)

_AUSTRALIAN_RETAIL_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("state", "state"),
    ("industry", "text"),
    ("series_id", "text"),
    ("month", "text"),
    ("turnover", "target"),
)

_AUTOMOBILE_CLAIMS_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("state", "state"),
    ("class", "class"),
    ("gender", "gender"),
    ("age", "i"),
    ("paid", "target"),
)

_BAD_HEALTH_VISITS_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("numvisit", "target"),
    ("badh", "i"),
    ("age", "i"),
)

_BAKEOFF_BAKERS_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("series", "i"),
    ("baker", "text"),
    ("star_baker", "i"),
    ("technical_winner", "i"),
    ("technical_top3", "i"),
    ("technical_bottom", "i"),
    ("technical_highest", "i"),
    ("technical_lowest", "i"),
    ("technical_median", "d"),
    ("series_winner", "i"),
    ("series_runner_up", "i"),
    ("total_episodes_appeared", "target"),
    ("first_date_appeared", "date"),
    ("last_date_appeared", "date"),
    ("first_date_us", "i"),
    ("last_date_us", "i"),
    ("percent_episodes_appeared", "d"),
    ("percent_technical_top3", "d"),
    ("baker_full", "text"),
    ("age", "i"),
    ("occupation", "text"),
    ("hometown", "text"),
    ("baker_last", "text"),
    ("baker_first", "text"),
)

_BAKEOFF_CHALLENGES_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("series", "i"),
    ("episode", "i"),
    ("baker", "text"),
    ("result", "label"),
    ("signature", "text"),
    ("technical", "i"),
    ("showstopper", "text"),
)

_BAKEOFF_EPISODES_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("series", "i"),
    ("episode", "i"),
    ("bakers_appeared", "i"),
    ("bakers_out", "i"),
    ("bakers_remaining", "target"),
    ("star_bakers", "i"),
    ("technical_winners", "i"),
    ("sb_name", "text"),
    ("winner_name", "winner_name"),
    ("eliminated", "text"),
)

_BAKEOFF_RATINGS_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("series", "i"),
    ("episode", "i"),
    ("uk_airdate", "date"),
    ("viewers_7day", "target"),
    ("viewers_28day", "d"),
    ("network_rank", "i"),
    ("channels_rank", "i"),
    ("bbc_iplayer_requests", "i"),
    ("episode_count", "i"),
    ("us_season", "i"),
    ("us_airdate", "text"),
)

_BARLEY_YIELDS_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("yield", "target"),
    ("variety", "variety"),
    ("year", "i"),
    ("site", "site"),
)

_BENTHIC_OXYGEN_STACK_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("age", "d"),
    ("delta", "target"),
    ("error", "d"),
)

_BIG_TECH_SHARES_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("symbol", "symbol"),
    ("date", "date"),
    ("open", "d"),
    ("high", "d"),
    ("low", "d"),
    ("close", "target"),
    ("adj_close", "d"),
    ("volume", "i"),
)

_BLOOD_STORAGE_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("rbc_age_group", "i"),
    ("median_rbc_age", "i"),
    ("age", "d"),
    ("aa", "i"),
    ("famhx", "i"),
    ("pvol", "d"),
    ("tvol", "i"),
    ("t_stage", "i"),
    ("bgs", "i"),
    ("bn", "i"),
    ("organconfined", "i"),
    ("preoppsa", "d"),
    ("preoptherapy", "i"),
    ("units", "i"),
    ("sgs", "i"),
    ("anyadjtherapy", "i"),
    ("adjradtherapy", "i"),
    ("recurrence", "label"),
    ("censor", "i"),
    ("timetorecurrence", "d"),
)

_BODILY_INJURY_CLAIMS_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("casenum", "i"),
    ("attorney", "i"),
    ("clmsex", "i"),
    ("marital", "i"),
    ("clminsur", "i"),
    ("seatbelt", "i"),
    ("clmage", "i"),
    ("loss", "target"),
)

_BONE_MARROW_LEUKAEMIA_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("group", "i"),
    ("t1", "target"),
    ("t2", "i"),
    ("d1", "i"),
    ("d2", "i"),
    ("d3", "i"),
    ("ta", "i"),
    ("da", "i"),
    ("tc", "i"),
    ("dc", "i"),
    ("tp", "i"),
    ("dp", "i"),
    ("z1", "i"),
    ("z2", "i"),
    ("z3", "i"),
    ("z4", "i"),
    ("z5", "i"),
    ("z6", "i"),
    ("z7", "i"),
    ("z8", "i"),
    ("z9", "i"),
    ("z10", "i"),
)

_BORNHOLM_BROOCHES_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("number", "i"),
    ("site", "text"),
    ("period", "label"),
    ("n2c", "i"),
    ("r3d", "i"),
    ("n2a", "i"),
    ("q3b", "i"),
    ("r3c", "i"),
    ("n1", "i"),
    ("q3c", "i"),
    ("o1", "i"),
    ("o2", "i"),
    ("n2e", "i"),
    ("i3", "i"),
    ("r3b", "i"),
    ("k1a", "i"),
    ("q3a", "i"),
    ("i2", "i"),
    ("k1c", "i"),
    ("k1b", "i"),
    ("h", "i"),
    ("q3d", "i"),
    ("j1d", "i"),
    ("s1", "i"),
    ("d", "i"),
    ("q2", "i"),
    ("s3", "i"),
    ("p2", "i"),
    ("p4", "i"),
    ("g3", "i"),
    ("e2a", "i"),
    ("p3", "i"),
    ("r3a", "i"),
    ("r1", "i"),
    ("e2b", "i"),
    ("g2", "i"),
    ("i1b", "i"),
    ("g1", "i"),
    ("f", "i"),
    ("p1", "i"),
    ("i1a", "i"),
    ("a2e", "i"),
)

_BOWLEY_WAGES_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("year", "i"),
    ("value", "target"),
)

_BREAST_FEEDING_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("duration", "target"),
    ("delta", "i"),
    ("race", "i"),
    ("poverty", "i"),
    ("smoke", "i"),
    ("alcohol", "i"),
    ("agemth", "i"),
    ("ybirth", "i"),
    ("yschool", "i"),
    ("pc3mth", "i"),
)

_BRESLAU_LIFE_TABLE_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("age", "i"),
    ("year1687", "i"),
    ("year1688", "i"),
    ("year1689", "i"),
    ("year1690", "i"),
    ("year1691", "i"),
    ("total", "i"),
    ("average", "target"),
)

_BRONZE_AGE_CUPS_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("rd", "d"),
    ("nd", "d"),
    ("sd", "d"),
    ("h", "d"),
    ("nh", "d"),
    ("phase", "label"),
)

_BUNDESLIGA_MATCHES_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("hometeam", "text"),
    ("awayteam", "text"),
    ("homegoals", "target"),
    ("awaygoals", "i"),
    ("round", "i"),
    ("year", "i"),
    ("date", "time"),
)

_BUNDESTAG_2005_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("bundesland", "bundesland"),
    ("fraktion", "fraktion"),
    ("freq", "target"),
)

_BURN_WOUND_INFECTION_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("obs", "i"),
    ("z1", "i"),
    ("z2", "i"),
    ("z3", "i"),
    ("z4", "i"),
    ("z5", "i"),
    ("z6", "i"),
    ("z7", "i"),
    ("z8", "i"),
    ("z9", "i"),
    ("z10", "i"),
    ("z11", "i"),
    ("t1", "target"),
    ("d1", "i"),
    ("t2", "i"),
    ("d2", "i"),
    ("t3", "i"),
    ("d3", "i"),
)

_CARE_HOME_INCIDENTS_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("carehomefail", "label"),
    ("weightloss", "i"),
    ("medication", "i"),
    ("falls", "i"),
    ("choking", "i"),
    ("unexpecteddeaths", "i"),
    ("bruising", "i"),
    ("absconsion", "i"),
    ("residentabusebyresident", "i"),
    ("residentabusebystaff", "i"),
    ("residentabuseonstaff", "i"),
    ("wounds", "i"),
)

_CAVENDISH_DENSITY_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("density", "target"),
    ("density2", "d"),
    ("density3", "d"),
)

_CHINESE_BRONZES_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("reference", "i"),
    ("chronology", "i"),
    ("dynasty", "label"),
    ("cu", "i"),
    ("sn", "i"),
    ("pb", "i"),
    ("zn", "d"),
    ("au", "d"),
    ("ag", "i"),
    ("as", "i"),
    ("sb", "d"),
)

_CHOLERA_DEATHS_1849_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("month", "month"),
    ("cause_of_death", "label"),
    ("day_of_month", "i"),
    ("deaths", "i"),
    ("date", "date"),
    ("day_of_week", "day_of_week"),
)

_CHORAL_SINGERS_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("height", "i"),
    ("voice_part", "label"),
)

_COAL_MINERS_BREATHING_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("breathlessness", "breathlessness"),
    ("wheeze", "wheeze"),
    ("age", "age"),
    ("freq", "target"),
)

_COLLEGE_PROXIMITY_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("nearc4", "i"),
    ("educ", "i"),
    ("black", "i"),
    ("smsa", "i"),
    ("south", "i"),
    ("married", "i"),
    ("exper", "i"),
    ("lwage", "target"),
)

_COLLEGE_SCORECARD_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("id", "i"),
    ("name", "text"),
    ("city", "text"),
    ("state", "text"),
    ("zip", "text"),
    ("latitude", "d"),
    ("longitude", "d"),
    ("url", "text"),
    ("deg_predominant", "deg_predominant"),
    ("deg_highest", "deg_predominant"),
    ("control", "label"),
    ("locale_type", "locale_type"),
    ("locale_size", "locale_size"),
    ("adm_req_test", "adm_req_test"),
    ("is_hbcu", "is_hbcu"),
    ("is_pbi", "is_hbcu"),
    ("is_annhi", "is_hbcu"),
    ("is_tribal", "is_hbcu"),
    ("is_aanapii", "is_hbcu"),
    ("is_hsi", "is_hbcu"),
    ("is_nanti", "is_hbcu"),
    ("is_only_men", "is_hbcu"),
    ("is_only_women", "is_hbcu"),
    ("is_only_distance", "is_hbcu"),
    ("religious_affiliation", "text"),
)

_CORPORAL_PUNISHMENT_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("freq", "target"),
    ("attitude", "attitude"),
    ("memory", "memory"),
    ("education", "education"),
    ("age", "age"),
)

_COVID_TESTING_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("subject_id", "i"),
    ("fake_first_name", "text"),
    ("fake_last_name", "text"),
    ("gender", "gender"),
    ("pan_day", "i"),
    ("test_id", "test_id"),
    ("clinic_name", "text"),
    ("result", "label"),
    ("demo_group", "demo_group"),
    ("age", "d"),
    ("drive_thru_ind", "i"),
    ("ct_result", "d"),
    ("orderset", "i"),
    ("payor_group", "payor_group"),
    ("patient_class", "patient_class"),
    ("col_rec_tat", "d"),
    ("rec_ver_tat", "d"),
)

_CSGO_MATCHES_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("map", "map"),
    ("day", "i"),
    ("month", "i"),
    ("year", "i"),
    ("date", "text"),
    ("wait_time_s", "i"),
    ("match_time_s", "i"),
    ("team_a_rounds", "i"),
    ("team_b_rounds", "i"),
    ("ping", "i"),
    ("kills", "i"),
    ("assists", "i"),
    ("deaths", "i"),
    ("mvps", "i"),
    ("hs_percent", "i"),
    ("points", "i"),
    ("result", "label"),
)

_CYTOMEGALOVIRUS_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("id", "i"),
    ("age", "i"),
    ("sex", "i"),
    ("race", "i"),
    ("diagnosis", "diagnosis"),
    ("diagnosis_type", "i"),
    ("time_to_transplant", "d"),
    ("prior_radiation", "i"),
    ("prior_chemo", "i"),
    ("prior_transplant", "i"),
    ("recipient_cmv", "i"),
    ("donor_cmv", "i"),
    ("donor_sex", "i"),
    ("tnc_dose", "d"),
    ("cd34_dose", "d"),
    ("cd3_dose", "d"),
    ("cd8_dose", "d"),
    ("tbi_dose", "i"),
    ("c1_c2", "i"),
    ("akirs", "i"),
    ("cmv", "label"),
    ("time_to_cmv", "d"),
    ("agvhd", "i"),
    ("time_to_agvhd", "d"),
    ("cgvhd", "i"),
    ("time_to_cgvhd", "d"),
)

_DANISH_WELFARE_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("freq", "target"),
    ("alcohol", "alcohol"),
    ("income", "income"),
    ("status", "status"),
    ("urban", "urban"),
)

_DART_POINTS_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("name", "label"),
    ("catalog", "text"),
    ("tarl", "text"),
    ("quad", "text"),
    ("length", "d"),
    ("width", "d"),
    ("thickness", "d"),
    ("b_width", "d"),
    ("j_width", "d"),
    ("h_length", "d"),
    ("weight", "d"),
    ("blade_sh", "blade_sh"),
    ("base_sh", "blade_sh"),
    ("should_sh", "should_sh"),
    ("should_or", "should_or"),
    ("haft_sh", "haft_sh"),
    ("haft_or", "haft_or"),
)

_DATASAURUS_DOZEN_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("dataset", "label"),
    ("x", "d"),
    ("y", "d"),
)

_DEEP_SEA_FISH_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("site", "i"),
    ("totabund", "target"),
    ("density", "d"),
    ("meandepth", "i"),
    ("year", "i"),
    ("period", "period"),
    ("sweptarea", "d"),
)

_DRAG_RACE_APPEARANCES_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("season", "season"),
    ("rank", "target"),
    ("missc", "i"),
    ("contestant", "text"),
    ("episode", "i"),
    ("outcome", "text"),
    ("eliminated", "i"),
    ("participant", "i"),
    ("minichalw", "i"),
    ("finale", "i"),
    ("penultimate", "i"),
)

_DRAG_RACE_CONTESTANTS_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("season", "season"),
    ("contestant", "text"),
    ("age", "target"),
    ("dob", "date"),
    ("hometown", "text"),
)

_DRAG_RACE_EPISODES_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("season", "season"),
    ("episode", "i"),
    ("airdate", "date"),
    ("special", "i"),
    ("finale", "i"),
    ("nickname", "text"),
    ("runwaytheme", "text"),
    ("numqueens", "target"),
    ("minic", "text"),
    ("minicw1", "text"),
    ("minicw2", "text"),
    ("minicw3", "minicw3"),
    ("minicw4", "minicw4"),
    ("bottom1", "text"),
    ("bottom2", "text"),
    ("bottom3", "bottom3"),
    ("bottom4", "bottom4"),
    ("bottom5", "bottom5"),
    ("bottom6", "bottom6"),
    ("bottom7", "minicw4"),
    ("lipsyncartist", "text"),
    ("lipsyncsong", "text"),
    ("eliminated1", "text"),
    ("eliminated2", "eliminated2"),
)

_DRINKS_AND_WAGES_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("class", "class"),
    ("trade", "text"),
    ("sober", "i"),
    ("drinks", "i"),
    ("wage", "target"),
    ("n", "i"),
)

_END_SCRAPERS_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("width", "width"),
    ("sides", "sides"),
    ("curvature", "curvature"),
    ("retouched", "retouched"),
    ("site", "site"),
    ("freq", "target"),
)

_EPICA_CARBON_DIOXIDE_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("age", "i"),
    ("co2", "target"),
)

_ERNEST_WITTE_BURIALS_FIELDS: tuple[tuple[str, str], ...] = (
    ("name", "text"),
    ("group", "i"),
    ("north", "d"),
    ("west", "d"),
    ("age", "age"),
    ("sex", "sex"),
    ("direction", "i"),
    ("looking", "i"),
    ("goods", "label"),
)

_ESOPHAGEAL_CANCER_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("agegp", "agegp"),
    ("alcgp", "alcgp"),
    ("tobgp", "tobgp"),
    ("ncases", "target"),
    ("ncontrols", "i"),
)

_ETHANOL_ENGINE_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("nox", "target"),
    ("c", "d"),
    ("e", "d"),
)

_FAMILIAL_POLYPOSIS_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("participant_id", "i"),
    ("sex", "sex"),
    ("age", "i"),
    ("baseline", "i"),
    ("treatment", "treatment"),
    ("number3m", "i"),
    ("number12m", "target"),
)

_FINGERPRINT_PATTERNS_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("whorls", "i"),
    ("loops", "i"),
    ("count", "target"),
)

_FISH_ADULT_GROWTH_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("fish_code", "text"),
    ("period", "i"),
    ("position", "d"),
    ("distance", "target"),
)

_FISH_JUVENILE_CATCHES_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("fish_code", "text"),
    ("fish", "i"),
    ("otolith_code", "i"),
    ("site", "label"),
    ("day", "i"),
    ("month", "month"),
    ("catch_date", "date"),
)

_FISH_JUVENILE_GROWTH_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("fish_code", "text"),
    ("standard_length", "d"),
    ("body_depth", "d"),
    ("age", "i"),
    ("birthdate", "date"),
    ("growth_rate", "target"),
    ("early_growth", "d"),
    ("late_growth", "d"),
)

_FUNNEL_BEAKER_POTTERY_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("form", "label"),
    ("ax", "d"),
    ("ay", "d"),
    ("bx", "d"),
    ("by", "d"),
    ("cx", "d"),
    ("cy", "d"),
    ("dx", "d"),
    ("dy", "d"),
    ("ex", "d"),
    ("ey", "d"),
    ("fx", "d"),
    ("fy", "d"),
    ("gx", "d"),
    ("gy", "d"),
    ("hx", "d"),
    ("hy", "d"),
)

_FURZE_PLATT_HANDAXES_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("catalog", "text"),
    ("l", "target"),
    ("l1", "i"),
    ("b", "i"),
    ("b1", "i"),
    ("b2", "i"),
    ("t", "i"),
    ("t1", "i"),
)

_GALTON_FAMILIES_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("family", "text"),
    ("father", "d"),
    ("mother", "d"),
    ("midparentheight", "d"),
    ("children", "i"),
    ("childnum", "i"),
    ("gender", "gender"),
    ("childheight", "target"),
)

_GALTON_PARENT_CHILD_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("parent", "d"),
    ("child", "target"),
)

_GEOLOGIC_TIME_SCALE_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("type", "label"),
    ("name", "text"),
    ("age", "d"),
    ("error", "d"),
    ("parent", "text"),
)

_GERMAN_HEALTH_1984_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("docvis", "target"),
    ("hospvis", "i"),
    ("edlevel", "i"),
    ("age", "i"),
    ("outwork", "i"),
    ("female", "i"),
    ("married", "i"),
    ("kids", "i"),
    ("hhninc", "d"),
    ("educ", "d"),
    ("self_", "i"),
    ("edlevel1", "i"),
    ("edlevel2", "i"),
    ("edlevel3", "i"),
    ("edlevel4", "i"),
)

_GERMAN_HEALTH_REFORM_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("numvisit", "target"),
    ("reform", "i"),
    ("badh", "i"),
    ("age", "i"),
    ("educ", "i"),
    ("educ1", "i"),
    ("educ2", "i"),
    ("educ3", "i"),
    ("agegrp", "i"),
    ("age1", "i"),
    ("age2", "i"),
    ("age3", "i"),
    ("loginc", "d"),
)

_GERMAN_SUICIDES_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("freq", "target"),
    ("sex", "sex"),
    ("method", "method"),
    ("age", "i"),
    ("age_group", "age_group"),
    ("method2", "method2"),
)

_GLOBAL_ECONOMY_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("country", "text"),
    ("code", "text"),
    ("year", "i"),
    ("gdp", "target"),
    ("growth", "d"),
    ("cpi", "d"),
    ("imports", "d"),
    ("exports", "d"),
    ("population", "d"),
)

_GOSSET_YEAST_CELLS_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("sample", "sample"),
    ("count", "i"),
    ("freq", "target"),
)

_GOVERNMENT_TRANSFERS_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("income_centered", "d"),
    ("education", "d"),
    ("age", "d"),
    ("participation", "i"),
    ("support", "target"),
)

_GUERRY_MORAL_STATISTICS_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("dept", "i"),
    ("region", "region"),
    ("department", "text"),
    ("crime_pers", "target"),
    ("crime_prop", "i"),
    ("literacy", "i"),
    ("donations", "i"),
    ("infants", "i"),
    ("suicides", "i"),
    ("maincity", "maincity"),
    ("wealth", "i"),
    ("commerce", "i"),
    ("clergy", "i"),
    ("crime_parents", "i"),
    ("infanticide", "i"),
    ("donation_clergy", "i"),
    ("lottery", "i"),
    ("desertion", "i"),
    ("instruction", "i"),
    ("prostitutes", "i"),
    ("distance", "d"),
    ("area", "i"),
    ("pop1831", "d"),
)

_HARE_AND_LYNX_PELTS_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("year", "i"),
    ("hare", "d"),
    ("lynx", "target"),
)

_HEPATOCELLULAR_CARCINOMA_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("number", "i"),
    ("age", "i"),
    ("gender", "i"),
    ("hbsag", "i"),
    ("cirrhosis", "i"),
    ("alt", "i"),
    ("ast", "i"),
    ("afp", "i"),
    ("tumorsize", "i"),
    ("tumordifferentiation", "i"),
    ("vascularinvasion", "i"),
    ("tumormultiplicity", "i"),
    ("capsulation", "i"),
    ("tnm", "i"),
    ("bclc", "i"),
    ("os", "target"),
    ("death", "i"),
    ("rfs", "i"),
    ("recurrence", "i"),
    ("cxcl17t", "d"),
    ("cxcl17p", "d"),
    ("cxcl17n", "d"),
    ("cd4t", "d"),
    ("cd4n", "d"),
    ("cd8t", "d"),
    ("cd8n", "d"),
    ("cd20t", "d"),
    ("cd20n", "d"),
    ("cd57t", "d"),
    ("cd57n", "d"),
    ("cd15t", "d"),
    ("cd15n", "d"),
    ("cd68t", "d"),
    ("cd68n", "d"),
    ("cd4nr", "d"),
    ("cd8nr", "d"),
    ("cd20nr", "d"),
    ("cd57nr", "d"),
    ("cd15nr", "d"),
    ("cd68nr", "d"),
    ("cd4tr", "d"),
    ("cd8tr", "d"),
    ("cd20tr", "d"),
    ("cd57tr", "d"),
    ("cd15tr", "d"),
    ("cd68tr", "d"),
    ("ki67", "d"),
    ("cd34", "d"),
)

_HIV_TEST_RESULTS_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("villnum", "i"),
    ("got", "label"),
    ("distvct", "d"),
    ("tinc", "d"),
    ("any", "i"),
    ("age", "i"),
    ("hiv2004", "i"),
)

_HOUSEHOLD_BUDGETS_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("country", "country"),
    ("year", "i"),
    ("debt", "d"),
    ("di", "d"),
    ("expenditure", "d"),
    ("savings", "target"),
    ("wealth", "d"),
    ("unemployment", "d"),
)

_INDOMETHACIN_TRIAL_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("id", "i"),
    ("site", "site"),
    ("age", "i"),
    ("risk", "d"),
    ("gender", "gender"),
    ("outcome", "label"),
    ("sod", "sod"),
    ("pep", "sod"),
    ("recpanc", "sod"),
    ("psphinc", "sod"),
    ("precut", "sod"),
    ("difcan", "sod"),
    ("pneudil", "sod"),
    ("amp", "sod"),
    ("paninj", "sod"),
    ("acinar", "sod"),
    ("brush", "sod"),
    ("asa81", "asa81"),
    ("asa325", "asa81"),
    ("asa", "asa81"),
    ("prophystent", "sod"),
    ("therastent", "sod"),
    ("pdstent", "sod"),
    ("sodsom", "sod"),
    ("bsphinc", "sod"),
    ("bstent", "sod"),
    ("chole", "sod"),
    ("pbmal", "sod"),
    ("train", "sod"),
    ("status", "status"),
    ("type", "type"),
    ("rx", "rx"),
    ("bleed", "i"),
)

_INFANT_PNEUMONIA_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("chldage", "target"),
    ("hospital", "i"),
    ("mthage", "i"),
    ("urban", "i"),
    ("alcohol", "i"),
    ("smoke", "i"),
    ("region", "i"),
    ("poverty", "i"),
    ("bweight", "i"),
    ("race", "i"),
    ("education", "i"),
    ("nsibs", "i"),
    ("wmonth", "i"),
    ("sfmonth", "i"),
    ("agepn", "i"),
)

_INTCAL20_CURVE_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("calbp", "i"),
    ("age", "target"),
    ("error", "i"),
    ("delta", "d"),
    ("sigma", "d"),
)

_INTERACTION_TRIPTYCH_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("dataset", "dataset"),
    ("moderator", "moderator"),
    ("x", "d"),
    ("y", "target"),
)

_IRON_AGE_FIBULAE_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("grave", "text"),
    ("mno", "text"),
    ("fl", "i"),
    ("bh", "i"),
    ("bfa", "i"),
    ("fa", "i"),
    ("cd", "i"),
    ("bra", "i"),
    ("ed", "i"),
    ("fel", "i"),
    ("c", "i"),
    ("bw", "d"),
    ("bt", "d"),
    ("few", "d"),
    ("coils", "i"),
    ("length", "target"),
)

_IRON_AGE_GRAVES_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("type", "target"),
    ("g100", "i"),
    ("g200b", "i"),
    ("g200c", "i"),
    ("g201", "i"),
    ("g229", "i"),
    ("g500n", "i"),
    ("g532", "i"),
    ("g542", "i"),
    ("g552", "i"),
    ("g562", "i"),
    ("g600", "i"),
    ("g800", "i"),
    ("g900b", "i"),
    ("g900l", "i"),
    ("g900s", "i"),
    ("g900u", "i"),
)

_JEVONS_GUESSES_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("actual", "i"),
    ("estimated", "i"),
    ("frequency", "i"),
    ("error", "target"),
)

_KIDNEY_TRANSPLANT_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("obs", "i"),
    ("time", "target"),
    ("delta", "i"),
    ("gender", "i"),
    ("race", "i"),
    ("age", "i"),
)

_KOMMOS_POTTERY_FIELDS: tuple[tuple[str, str], ...] = (
    ("name", "text"),
    ("type", "label"),
    ("date", "date_code"),
    ("sm", "d"),
    ("lu", "d"),
    ("u", "d"),
    ("yb", "d"),
    ("as", "d"),
    ("sb", "d"),
    ("ca", "d"),
    ("na", "d"),
    ("la", "d"),
    ("ce", "d"),
    ("th", "d"),
    ("cr", "d"),
    ("hf", "d"),
    ("cs", "d"),
    ("sc", "d"),
    ("rb", "i"),
    ("fe", "d"),
    ("ta", "d"),
    ("co", "d"),
    ("eu", "d"),
)

_LARYNGOSCOPE_TRIAL_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("age", "i"),
    ("gender", "i"),
    ("asa", "i"),
    ("bmi", "d"),
    ("mallampati", "i"),
    ("randomization", "i"),
    ("attempt1_time", "d"),
    ("attempt1_s_f", "i"),
    ("attempt2_time", "i"),
    ("attempt2_assigned_method", "i"),
    ("attempt2_s_f", "i"),
    ("attempt3_time", "i"),
    ("attempt3_assigned_method", "i"),
    ("attempt3_s_f", "i"),
    ("attempts", "i"),
    ("failures", "i"),
    ("total_intubation_time", "target"),
    ("intubation_overall_s_f", "i"),
    ("bleeding", "i"),
    ("ease", "i"),
    ("sore_throat", "i"),
    ("view", "i"),
)

_LARYNX_CANCER_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("stage", "i"),
    ("time", "target"),
    ("age", "i"),
    ("diagyr", "i"),
    ("delta", "i"),
)

_LAW_DOME_GASES_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("year", "i"),
    ("noaa04", "d"),
    ("ch4_spl", "d"),
    ("ch4_grw", "d"),
    ("co2_spl", "target"),
    ("co2_grw", "d"),
    ("n2o_spl", "d"),
    ("n2o_grw", "d"),
)

_LETTERS_TO_POLITICIANS_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("leg_black", "i"),
    ("treat_out", "i"),
    ("responded", "label"),
    ("totalpop", "d"),
    ("medianhhincom", "d"),
    ("black_medianhh", "d"),
    ("white_medianhh", "d"),
    ("blackpercent", "d"),
    ("statessquireindex", "d"),
    ("nonblacknonwhite", "i"),
    ("urbanpercent", "d"),
    ("leg_senator", "i"),
    ("leg_democrat", "i"),
    ("south", "i"),
)

_LICORICE_GARGLE_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("preop_gender", "i"),
    ("preop_asa", "i"),
    ("preop_calcbmi", "d"),
    ("preop_age", "i"),
    ("preop_mallampati", "i"),
    ("preop_smoking", "i"),
    ("preop_pain", "i"),
    ("treat", "i"),
    ("intraop_surgerysize", "i"),
    ("extubation_cough", "i"),
    ("pacu30min_cough", "i"),
    ("pacu30min_throatpain", "target"),
    ("pacu30min_swallowpain", "i"),
    ("pacu90min_cough", "i"),
    ("pacu90min_throatpain", "i"),
    ("postop4hour_cough", "i"),
    ("postop4hour_throatpain", "i"),
    ("pod1am_cough", "i"),
    ("pod1am_throatpain", "i"),
)

_LONDON_CHOLERA_DISTRICTS_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("district", "text"),
    ("cholera_drate", "target"),
    ("cholera_deaths", "i"),
    ("popn", "i"),
    ("elevation", "i"),
    ("region", "region"),
    ("water", "water"),
    ("annual_deaths", "i"),
    ("pop_dens", "i"),
    ("persons_house", "d"),
    ("house_valpp", "d"),
    ("poor_rate", "d"),
    ("area", "i"),
    ("houses", "i"),
    ("house_val", "i"),
)

_LONG_STAY_PATIENTS_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("stranded_label", "label"),
    ("age", "i"),
    ("care_home_referral", "i"),
    ("medicallysafe", "i"),
    ("hcop", "i"),
    ("mental_health_care", "i"),
    ("periods_of_previous_care", "i"),
    ("admit_date", "text"),
    ("frailty_index", "frailty_index"),
)

_MACDONELL_CRIMINALS_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("height", "d"),
    ("finger", "d"),
    ("frequency", "target"),
)

_MEDICARE_STAYS_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("los", "target"),
    ("hmo", "i"),
    ("white", "i"),
    ("died", "i"),
    ("age80", "i"),
    ("type", "i"),
    ("type1", "i"),
    ("type2", "i"),
    ("type3", "i"),
    ("provnum", "i"),
)

_MEDIEVAL_GLASS_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("site", "site"),
    ("sample", "text"),
    ("type", "type"),
    ("age", "age"),
    ("periode", "periode"),
    ("tint", "tint"),
    ("na2o", "d"),
    ("cao", "d"),
    ("k2o", "d"),
    ("mgo", "d"),
    ("p2o5", "d"),
    ("sio2", "target"),
    ("al2o3", "d"),
    ("feo", "d"),
    ("mno", "d"),
    ("cl", "d"),
    ("reference", "text"),
)

_MESOLITHIC_TOOLS_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("microliths", "target"),
    ("scrapers", "i"),
    ("burins", "i"),
    ("axes", "i"),
    ("saws", "i"),
)

_MICHELSBERG_POTTERY_FIELDS: tuple[tuple[str, str], ...] = (
    ("name", "text"),
    ("id", "i"),
    ("site_name", "text"),
    ("catalogue_nr", "i"),
    ("feature_nr", "d"),
    ("to3", "i"),
    ("f4", "i"),
    ("b2", "i"),
    ("to2", "i"),
    ("b3", "i"),
    ("b7", "i"),
    ("kw5", "i"),
    ("vg1", "i"),
    ("vg2", "i"),
    ("t4a", "i"),
    ("kw2", "i"),
    ("kw4", "i"),
    ("b5", "i"),
    ("t3b", "i"),
    ("f3", "i"),
    ("kw3", "i"),
    ("kw1", "i"),
    ("b6", "i"),
    ("to1", "i"),
    ("b1", "i"),
    ("t3a", "i"),
    ("vg4", "i"),
    ("ks2", "d"),
    ("ks1", "d"),
    ("t2b", "i"),
    ("f2", "i"),
    ("bs3", "i"),
    ("t2a", "i"),
    ("bs2", "i"),
    ("b4", "i"),
    ("bs1", "i"),
    ("f1", "i"),
    ("t1b", "i"),
    ("vg3", "i"),
    ("t1a", "i"),
    ("mbk_phase", "label"),
    ("x_utm32n", "i"),
    ("y_utm32n", "i"),
)

_MINARD_TROOPS_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("long", "d"),
    ("lat", "d"),
    ("survivors", "target"),
    ("direction", "direction"),
    ("group", "i"),
)

_MISSISSIPPI_POTTERY_FIELDS: tuple[tuple[str, str], ...] = (
    ("name", "text"),
    ("parkinpunctate", "i"),
    ("bartonkentmpi", "i"),
    ("painted", "target"),
    ("fortunenoded", "i"),
    ("ranchincised", "i"),
    ("wallsengraved", "i"),
    ("wallaceincised", "i"),
    ("rhodesincised", "i"),
    ("vernonpaulapplique", "i"),
    ("hullengraved", "i"),
)

_NGRIP_ICE_CORE_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("age", "i"),
    ("depth", "d"),
    ("delta", "target"),
    ("mce", "i"),
)

_NIGHTINGALE_MORTALITY_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("date", "date"),
    ("month", "month"),
    ("year", "i"),
    ("army", "i"),
    ("disease", "i"),
    ("wounds", "i"),
    ("other", "i"),
    ("disease_rate", "target"),
    ("wounds_rate", "d"),
    ("other_rate", "d"),
)

_OLYMPIC_RUNNING_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("year", "i"),
    ("length", "i"),
    ("sex", "sex"),
    ("time", "target"),
)

_ORGAN_DONATIONS_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("state", "text"),
    ("quarter", "quarter"),
    ("rate", "target"),
    ("quarter_num", "i"),
)

_OXFORD_POTTERY_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("place", "text"),
    ("oxfordpct", "target"),
    ("oxforddst", "i"),
    ("newforestpct", "d"),
    ("newforestdst", "i"),
    ("walledarea", "d"),
    ("watertrans", "i"),
)

_OZONE_AND_WEATHER_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("ozone", "target"),
    ("radiation", "i"),
    ("temperature", "i"),
    ("wind", "d"),
)

_PARIS_REGISTRATIONS_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("year", "i"),
    ("month", "month"),
    ("count", "target"),
    ("mon", "i"),
    ("date", "date"),
)

_PEARSON_LEE_HEIGHTS_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("child", "target"),
    ("parent", "d"),
    ("frequency", "d"),
    ("gp", "gp"),
    ("par", "par"),
    ("chl", "chl"),
)

_PLANT_CARBON_ISOTOPES_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("family", "text"),
    ("species", "text"),
    ("type", "label"),
    ("delta", "d"),
    ("country", "country"),
)

_PLANT_TRAITS_FIELDS: tuple[tuple[str, str], ...] = (
    ("name", "text"),
    ("pdias", "d"),
    ("longindex", "d"),
    ("durflow", "i"),
    ("height", "target"),
    ("begflow", "i"),
    ("mycor", "i"),
    ("vegaer", "i"),
    ("vegsout", "i"),
    ("autopoll", "i"),
    ("insects", "i"),
    ("wind", "i"),
    ("lign", "i"),
    ("piq", "i"),
    ("ros", "i"),
    ("semiros", "i"),
    ("leafy", "i"),
    ("suman", "i"),
    ("winan", "i"),
    ("monocarp", "i"),
    ("polycarp", "i"),
    ("seasaes", "i"),
    ("seashiv", "i"),
    ("seasver", "i"),
    ("everalw", "i"),
    ("everparti", "i"),
    ("elaio", "i"),
    ("endozoo", "i"),
    ("epizoo", "i"),
    ("aquat", "i"),
    ("windgl", "i"),
    ("unsp", "i"),
)

_PLAYFAIR_WHEAT_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("year", "i"),
    ("wheat", "target"),
    ("wages", "d"),
)

_PORTAL_RODENTS_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("record_id", "i"),
    ("month", "i"),
    ("day", "i"),
    ("year", "i"),
    ("plot_id", "i"),
    ("species_id", "text"),
    ("sex", "sex"),
    ("hindfoot_length", "i"),
    ("weight", "i"),
    ("genus", "text"),
    ("species", "text"),
    ("taxa", "label"),
    ("plot_type", "plot_type"),
)

_PORTAL_SPECIES_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("species_id", "text"),
    ("genus", "text"),
    ("species", "text"),
    ("taxa", "label"),
)

_PREDIABETES_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("age", "i"),
    ("sex", "i"),
    ("imd_decile", "i"),
    ("bmi", "d"),
    ("age_prediabetes", "i"),
    ("hba1c", "i"),
    ("time_pre_to_diabetes", "target"),
    ("age_diabetes", "i"),
    ("prediabetes_checks_before_diabetes", "i"),
)

_PROSTATE_SURVIVAL_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("grade", "grade"),
    ("stage", "stage"),
    ("agegroup", "agegroup"),
    ("survtime", "target"),
    ("status", "i"),
)

_PRUSSIAN_HORSE_KICKS_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("deaths", "target"),
    ("year", "i"),
    ("corps", "corps"),
    ("fisher", "fisher"),
)

_RASHOMON_QUARTET_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("split", "split"),
    ("x1", "d"),
    ("x2", "d"),
    ("x3", "d"),
    ("y", "target"),
)

_REPEAT_VICTIMISATION_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("first_victimization", "first_victimization"),
    ("second_victimization", "first_victimization"),
    ("freq", "target"),
)

_REPUBLICAN_VOTE_SHARE_FIELDS: tuple[tuple[str, str], ...] = (
    ("name", "text"),
    ("x1856", "d"),
    ("x1860", "d"),
    ("x1864", "d"),
    ("x1868", "d"),
    ("x1872", "d"),
    ("x1876", "d"),
    ("x1880", "d"),
    ("x1884", "d"),
    ("x1888", "d"),
    ("x1892", "d"),
    ("x1896", "d"),
    ("x1900", "d"),
    ("x1904", "d"),
    ("x1908", "d"),
    ("x1912", "d"),
    ("x1916", "d"),
    ("x1920", "d"),
    ("x1924", "d"),
    ("x1928", "d"),
    ("x1932", "d"),
    ("x1936", "d"),
    ("x1940", "d"),
    ("x1944", "d"),
    ("x1948", "d"),
    ("x1952", "d"),
    ("x1956", "d"),
    ("x1960", "d"),
    ("x1964", "d"),
    ("x1968", "d"),
    ("x1972", "d"),
    ("x1976", "target"),
)

_RESTAURANT_INSPECTIONS_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("business_name", "text"),
    ("inspection_score", "target"),
    ("year", "i"),
    ("numberoflocations", "i"),
    ("weekend", "weekend"),
)

_RICE_FARMER_INSURANCE_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("address", "text"),
    ("village", "text"),
    ("takeup_survey", "label"),
    ("age", "i"),
    ("agpop", "i"),
    ("ricearea_2010", "d"),
    ("disaster_prob", "d"),
    ("male", "i"),
    ("default", "i"),
    ("intensive", "i"),
    ("risk_averse", "d"),
    ("literacy", "i"),
    ("pre_takeup_rate", "d"),
)

_ROCHDALE_WOMEN_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("econactive", "econactive"),
    ("age", "age"),
    ("husbandemployed", "econactive"),
    ("child", "econactive"),
    ("education", "econactive"),
    ("husbandeducation", "econactive"),
    ("asian", "econactive"),
    ("householdworking", "econactive"),
    ("freq", "target"),
)

_ROMAN_STREET_NETWORKS_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("name", "text"),
    ("area", "i"),
    ("population", "i"),
    ("forum_area", "i"),
    ("street_area", "target"),
    ("street_length", "i"),
    ("street_width", "i"),
    ("block_area", "i"),
)

_ROMANO_BRITISH_GLASS_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("site", "label"),
    ("al", "d"),
    ("fe", "d"),
    ("mg", "d"),
    ("ca", "d"),
    ("na", "d"),
    ("k", "d"),
    ("ti", "d"),
    ("p", "d"),
    ("mn", "d"),
    ("sb", "d"),
    ("pb", "d"),
)

_ROMANO_BRITISH_POTTERY_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("id", "text"),
    ("kiln", "label"),
    ("region", "region"),
    ("al2o3", "d"),
    ("fe2o3", "d"),
    ("mgo", "d"),
    ("cao", "d"),
    ("na2o", "d"),
    ("k2o", "d"),
    ("tio2", "d"),
    ("mno", "d"),
    ("bao", "d"),
)

_RUSPINI_POINTS_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("x", "target"),
    ("y", "target"),
)

_SEA_LEVEL_RECONSTRUCTION_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("age_calkabp", "i"),
    ("sealev_shortpc1", "d"),
    ("sealev_shortpc1_err_sig", "d"),
    ("sealev_shortpc1_err_lo", "d"),
    ("sealev_shortpc1_err_up", "d"),
    ("sealev_longpc1", "target"),
    ("sealev_longpc1_err_sig", "d"),
    ("sealev_longpc1_err_lo", "d"),
    ("sealev_longpc1_err_up", "d"),
)

_SHIP_DAMAGE_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("accident", "target"),
    ("op", "i"),
    ("co_65_69", "i"),
    ("co_70_74", "i"),
    ("co_75_79", "i"),
    ("service", "i"),
    ("ship", "i"),
)

_SINGAPORE_CAR_CLAIMS_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("sexinsured", "sexinsured"),
    ("female", "i"),
    ("vehicletype", "vehicletype"),
    ("pc", "i"),
    ("clm_count", "target"),
    ("exp_weights", "d"),
    ("lnweight", "d"),
    ("ncd", "i"),
    ("agecat", "i"),
    ("autoage0", "i"),
    ("autoage1", "i"),
    ("autoage2", "i"),
    ("autoage", "i"),
    ("vagecat", "i"),
    ("vagecat1", "i"),
)

_SMARTPILL_MOTILITY_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("group", "i"),
    ("gender", "i"),
    ("race", "i"),
    ("height", "d"),
    ("weight", "d"),
    ("age", "i"),
    ("ge_time", "target"),
    ("sb_time", "d"),
    ("c_time", "d"),
    ("wg_time", "d"),
    ("s_contractions", "i"),
    ("s_sum_of_amplitudes", "d"),
    ("s_mean_peak_amplitude", "d"),
    ("s_mean_ph", "d"),
    ("sb_contractions", "i"),
    ("sb_sum_of_amplitudes", "d"),
    ("sb_mean_peak_amplitude", "d"),
    ("sb_mean_ph", "d"),
    ("colon_contractions", "i"),
    ("colon_sum_of_amplitudes", "d"),
    ("c_mean_peak_amplitude", "d"),
    ("c_mean_ph", "d"),
)

_SMOKING_CESSATION_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("id", "i"),
    ("ttr", "target"),
    ("relapse", "i"),
    ("grp", "grp"),
    ("age", "i"),
    ("gender", "gender"),
    ("race", "race"),
    ("employment", "employment"),
    ("yearssmoking", "i"),
    ("levelsmoking", "levelsmoking"),
    ("agegroup2", "agegroup2"),
    ("agegroup4", "agegroup4"),
    ("priorattempts", "i"),
    ("longestnosmoke", "i"),
)

_SNODGRASS_HOUSES_FIELDS: tuple[tuple[str, str], ...] = (
    ("name", "text"),
    ("east", "d"),
    ("south", "d"),
    ("length", "d"),
    ("width", "d"),
    ("segment", "i"),
    ("inside", "label"),
    ("area", "d"),
    ("points", "i"),
    ("abraders", "i"),
    ("discs", "i"),
    ("earplugs", "i"),
    ("effigies", "i"),
    ("ceramics", "i"),
    ("total", "i"),
    ("types", "i"),
)

_SNOW_CHOLERA_DEATHS_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("case", "i"),
    ("x", "target"),
    ("y", "target"),
)

_STD_REINFECTION_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("obs", "i"),
    ("race", "race"),
    ("marital", "marital"),
    ("age", "i"),
    ("yschool", "i"),
    ("iinfct", "i"),
    ("npartner", "i"),
    ("os12m", "i"),
    ("os30d", "i"),
    ("rs12m", "i"),
    ("rs30d", "i"),
    ("abdpain", "i"),
    ("discharge", "i"),
    ("dysuria", "i"),
    ("condom", "i"),
    ("itch", "i"),
    ("lesion", "i"),
    ("rash", "i"),
    ("lymph", "i"),
    ("vagina", "i"),
    ("dchexam", "i"),
    ("abnode", "i"),
    ("rinfct", "i"),
    ("time", "target"),
)

_STONE_AGE_SITES_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("ta", "target"),
    ("ba", "i"),
    ("toa", "i"),
    ("aa", "i"),
    ("m", "i"),
    ("fk", "i"),
    ("bk", "i"),
    ("nk", "i"),
    ("cfs", "i"),
    ("bs", "i"),
    ("ds", "i"),
    ("bu", "i"),
    ("ax", "i"),
    ("ch", "i"),
    ("sax", "i"),
    ("pf", "i"),
)

_STREPTOMYCIN_TUBERCULOSIS_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("patient_id", "i"),
    ("arm", "arm"),
    ("dose_strep_g", "i"),
    ("dose_pas_g", "i"),
    ("gender", "gender"),
    ("baseline_condition", "baseline_condition"),
    ("baseline_temp", "baseline_temp"),
    ("baseline_esr", "baseline_esr"),
    ("baseline_cavitation", "baseline_cavitation"),
    ("strep_resistance", "strep_resistance"),
    ("radiologic_6m", "radiologic_6m"),
    ("rad_num", "i"),
    ("improved", "label"),
)

_STROKE_CLASSIFICATION_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("pat_id", "i"),
    ("stroke", "label"),
    ("gender", "gender"),
    ("age", "d"),
    ("hypertension", "i"),
    ("heart_disease", "i"),
    ("work_related_stress", "i"),
    ("urban_residence", "i"),
    ("avg_glucose_level", "d"),
    ("bmi", "d"),
    ("smokes", "i"),
)

_SUPPORTED_WORK_PROGRAMME_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("data_id", "data_id"),
    ("treat", "i"),
    ("age", "i"),
    ("educ", "i"),
    ("black", "i"),
    ("hisp", "i"),
    ("marr", "i"),
    ("nodegree", "i"),
    ("re74", "d"),
    ("re75", "d"),
    ("re78", "target"),
)

_SUPRACLAVICULAR_BLOCK_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("subject", "i"),
    ("group", "i"),
    ("gender", "i"),
    ("bmi", "d"),
    ("age", "i"),
    ("fentanyl", "i"),
    ("alfentanil", "d"),
    ("midazolam", "d"),
    ("onset_sensory", "target"),
    ("onset_first_sensory", "i"),
    ("onset_motor", "i"),
    ("nerve_block_censor", "i"),
    ("med_duration", "d"),
    ("med_censor", "i"),
    ("vps_rest", "i"),
    ("vps_movement", "i"),
    ("opioid_total", "d"),
)

_SWEDISH_MOTORCYCLES_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("agarald", "i"),
    ("kon", "kon"),
    ("zon", "i"),
    ("mcklass", "i"),
    ("fordald", "i"),
    ("bonuskl", "i"),
    ("duration", "d"),
    ("antskad", "i"),
    ("skadkost", "target"),
)

_TEXAS_PRISONS_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("statefip", "i"),
    ("year", "i"),
    ("bmprison", "target"),
    ("wmprison", "d"),
    ("alcohol", "d"),
    ("income", "i"),
    ("ur", "d"),
    ("poverty", "d"),
    ("black", "d"),
    ("perc1519", "d"),
    ("aidscapita", "d"),
    ("state", "text"),
)

_TONGUE_CANCER_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("type", "i"),
    ("time", "target"),
    ("delta", "i"),
)

_TRIAL_OF_THE_PYX_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("bags", "bags"),
    ("group", "group"),
    ("deviation", "deviation"),
    ("count", "target"),
)

_US_REGIONAL_MORTALITY_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("region", "region"),
    ("status", "status"),
    ("sex", "sex"),
    ("cause", "cause"),
    ("rate", "target"),
    ("se", "d"),
)

_VICTORIAN_ELECTRICITY_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("time", "time"),
    ("demand", "target"),
    ("temperature", "d"),
    ("date", "text"),
    ("holiday", "holiday"),
)

_VIRGIL_DACTYLS_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("foot", "i"),
    ("lines", "lines"),
    ("count", "target"),
)

_WOODLAND_BIRDS_FIELDS: tuple[tuple[str, str], ...] = (
    ("name", "text"),
    ("hiddenglen", "target"),
    ("wildwood", "i"),
    ("lonelypines", "i"),
)

_WORKERS_COMPENSATION_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("cl", "i"),
    ("yr", "i"),
    ("pr", "d"),
    ("loss", "target"),
)

_XCLARA_CLUSTERS_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("v1", "target"),
    ("v2", "target"),
)

_YULE_PAUPERISM_FIELDS: tuple[tuple[str, str], ...] = (
    ("row", "i"),
    ("location", "text"),
    ("paup", "target"),
    ("outrelief", "i"),
    ("old", "i"),
    ("pop", "i"),
)

_ZUNI_POTTERY_FIELDS: tuple[tuple[str, str], ...] = (
    ("name", "text"),
    ("lino", "target"),
    ("kiat", "i"),
    ("red", "i"),
    ("gall", "i"),
    ("esc", "i"),
    ("pubw", "i"),
    ("res", "i"),
    ("tula", "i"),
    ("pine", "i"),
    ("pubr", "i"),
    ("wing", "i"),
    ("wipo", "i"),
    ("sj", "i"),
    ("lsj", "i"),
    ("spr", "i"),
    ("piner", "i"),
    ("hesh", "i"),
    ("kwak", "i"),
)

#: A second shelf of Our World in Data charts, read the same country-year way:
#: what the world burns and generates, what it lets into the air, how warm the
#: air and the sea have grown, what the land is put to and what it yields, and
#: how many people live off it. Each chart page named as the source says whose
#: counting stands behind it.
_FOSSIL_ELECTRICITY_SHARE_FIELDS: tuple[tuple[str, str], ...] = (
    ("entity", "text"),
    ("code", "text"),
    ("year", "i"),
    ("fossil_percent", "target"),
)

_NUCLEAR_ELECTRICITY_SHARE_FIELDS: tuple[tuple[str, str], ...] = (
    ("entity", "text"),
    ("code", "text"),
    ("year", "i"),
    ("nuclear_percent", "target"),
)

_WIND_ELECTRICITY_SHARE_FIELDS: tuple[tuple[str, str], ...] = (
    ("entity", "text"),
    ("code", "text"),
    ("year", "i"),
    ("wind_percent", "target"),
)

_SOLAR_ELECTRICITY_SHARE_FIELDS: tuple[tuple[str, str], ...] = (
    ("entity", "text"),
    ("code", "text"),
    ("year", "i"),
    ("solar_percent", "target"),
)

_HYDRO_ELECTRICITY_SHARE_FIELDS: tuple[tuple[str, str], ...] = (
    ("entity", "text"),
    ("code", "text"),
    ("year", "i"),
    ("hydro_percent", "target"),
)

_ELECTRICITY_PER_PERSON_FIELDS: tuple[tuple[str, str], ...] = (
    ("entity", "text"),
    ("code", "text"),
    ("year", "i"),
    ("kwh_per_person", "target"),
)

_ELECTRICITY_DEMAND_FIELDS: tuple[tuple[str, str], ...] = (
    ("entity", "text"),
    ("code", "text"),
    ("year", "i"),
    ("demand_twh", "target"),
)

_FOSSIL_FUEL_ENERGY_FIELDS: tuple[tuple[str, str], ...] = (
    ("entity", "text"),
    ("code", "text"),
    ("year", "i"),
    ("fossil_twh", "target"),
)

_ELECTRICITY_CARBON_INTENSITY_FIELDS: tuple[tuple[str, str], ...] = (
    ("entity", "text"),
    ("code", "text"),
    ("year", "i"),
    ("grams_per_kwh", "target"),
)

_COAL_PRODUCTION_FIELDS: tuple[tuple[str, str], ...] = (
    ("entity", "text"),
    ("code", "text"),
    ("year", "i"),
    ("coal_twh", "target"),
)

_OIL_PRODUCTION_FIELDS: tuple[tuple[str, str], ...] = (
    ("entity", "text"),
    ("code", "text"),
    ("year", "i"),
    ("oil_twh", "target"),
)

_GAS_PRODUCTION_FIELDS: tuple[tuple[str, str], ...] = (
    ("entity", "text"),
    ("code", "text"),
    ("year", "i"),
    ("gas_twh", "target"),
)

_CONSUMPTION_CO2_EMISSIONS_FIELDS: tuple[tuple[str, str], ...] = (
    ("entity", "text"),
    ("code", "text"),
    ("year", "i"),
    ("emissions_tonnes", "target"),
)

_METHANE_EMISSIONS_FIELDS: tuple[tuple[str, str], ...] = (
    ("entity", "text"),
    ("code", "text"),
    ("year", "i"),
    ("tonnes_co2e", "target"),
)

_NITROUS_OXIDE_EMISSIONS_FIELDS: tuple[tuple[str, str], ...] = (
    ("entity", "text"),
    ("code", "text"),
    ("year", "i"),
    ("tonnes_co2e", "target"),
)

_GREENHOUSE_GAS_EMISSIONS_FIELDS: tuple[tuple[str, str], ...] = (
    ("entity", "text"),
    ("code", "text"),
    ("year", "i"),
    ("tonnes_co2e", "target"),
)

_TEMPERATURE_ANOMALY_FIELDS: tuple[tuple[str, str], ...] = (
    ("entity", "text"),
    ("code", "text"),
    ("year", "i"),
    ("anomaly_c", "target"),
    ("anomaly_low", "d"),
    ("anomaly_high", "d"),
)

_SEA_SURFACE_TEMPERATURE_FIELDS: tuple[tuple[str, str], ...] = (
    ("entity", "text"),
    ("code", "text"),
    ("year", "i"),
    ("anomaly_c", "target"),
    ("anomaly_low", "d"),
    ("anomaly_high", "d"),
)

_ICE_SHEET_MASS_FIELDS: tuple[tuple[str, str], ...] = (
    ("entity", "text"),
    ("code", "text"),
    ("day", "date"),
    ("mass_change_gt", "target"),
)

_ANNUAL_PRECIPITATION_FIELDS: tuple[tuple[str, str], ...] = (
    ("entity", "text"),
    ("code", "text"),
    ("year", "i"),
    ("precipitation_mm", "target"),
)

_FOREST_COVER_FIELDS: tuple[tuple[str, str], ...] = (
    ("entity", "text"),
    ("code", "text"),
    ("year", "i"),
    ("forest_percent", "target"),
    ("note", "text"),
)

_AGRICULTURAL_LAND_FIELDS: tuple[tuple[str, str], ...] = (
    ("entity", "text"),
    ("code", "text"),
    ("year", "i"),
    ("farmed_percent", "target"),
)

_FERTILIZER_USE_FIELDS: tuple[tuple[str, str], ...] = (
    ("entity", "text"),
    ("code", "text"),
    ("year", "i"),
    ("kg_per_hectare", "target"),
)

_PESTICIDE_USE_FIELDS: tuple[tuple[str, str], ...] = (
    ("entity", "text"),
    ("code", "text"),
    ("year", "i"),
    ("tonnes", "target"),
)

_WHEAT_YIELDS_FIELDS: tuple[tuple[str, str], ...] = (
    ("entity", "text"),
    ("code", "text"),
    ("year", "i"),
    ("tonnes_per_hectare", "target"),
)

_MAIZE_YIELDS_FIELDS: tuple[tuple[str, str], ...] = (
    ("entity", "text"),
    ("code", "text"),
    ("year", "i"),
    ("tonnes_per_hectare", "target"),
)

_RICE_YIELDS_FIELDS: tuple[tuple[str, str], ...] = (
    ("entity", "text"),
    ("code", "text"),
    ("year", "i"),
    ("tonnes_per_hectare", "target"),
)

_CEREAL_PRODUCTION_FIELDS: tuple[tuple[str, str], ...] = (
    ("entity", "text"),
    ("code", "text"),
    ("year", "i"),
    ("tonnes", "target"),
)

_CATTLE_NUMBERS_FIELDS: tuple[tuple[str, str], ...] = (
    ("entity", "text"),
    ("code", "text"),
    ("year", "i"),
    ("cattle", "target"),
)

_FISH_CONSUMPTION_FIELDS: tuple[tuple[str, str], ...] = (
    ("entity", "text"),
    ("code", "text"),
    ("year", "i"),
    ("kg_per_person", "target"),
)

_GDP_PER_CAPITA_GROWTH_FIELDS: tuple[tuple[str, str], ...] = (
    ("entity", "text"),
    ("code", "text"),
    ("year", "i"),
    ("growth_percent", "target"),
)

_TRADE_SHARE_OF_GDP_FIELDS: tuple[tuple[str, str], ...] = (
    ("entity", "text"),
    ("code", "text"),
    ("year", "i"),
    ("trade_percent", "target"),
)

_FOREIGN_DIRECT_INVESTMENT_FIELDS: tuple[tuple[str, str], ...] = (
    ("entity", "text"),
    ("code", "text"),
    ("year", "i"),
    ("investment_percent", "target"),
)

_LABOUR_FORCE_PARTICIPATION_FIELDS: tuple[tuple[str, str], ...] = (
    ("entity", "text"),
    ("code", "text"),
    ("year", "i"),
    ("participation_percent", "target"),
)

_WORLD_POPULATION_FIELDS: tuple[tuple[str, str], ...] = (
    ("entity", "text"),
    ("code", "text"),
    ("year", "i"),
    ("people", "target"),
)

_BIRTH_RATE_FIELDS: tuple[tuple[str, str], ...] = (
    ("entity", "text"),
    ("code", "text"),
    ("year", "i"),
    ("births_per_1000", "target"),
)

_MATERNAL_MORTALITY_FIELDS: tuple[tuple[str, str], ...] = (
    ("entity", "text"),
    ("code", "text"),
    ("year", "i"),
    ("deaths_per_100k", "target"),
    ("region", "text"),
    ("note", "text"),
)

_INTERNATIONAL_MIGRANTS_FIELDS: tuple[tuple[str, str], ...] = (
    ("entity", "text"),
    ("code", "text"),
    ("year", "i"),
    ("immigrants", "target"),
)

_BROADBAND_SUBSCRIPTIONS_FIELDS: tuple[tuple[str, str], ...] = (
    ("entity", "text"),
    ("code", "text"),
    ("year", "i"),
    ("subscriptions_per_100", "target"),
)

DATASETS: dict[str, Images | CIFAR | Audio | Matrix | Table | Large] = {
    "mnist": Images(
        name="mnist",
        label="MNIST",
        title="70,000 handwritten digits, 28x28 greyscale, 10 classes",
        licence="CC BY-SA 3.0",
        source="https://yann.lecun.com/exdb/mnist/",
        classes=tuple(str(digit) for digit in range(10)),
        modality="image",
        task="handwritten digit classification",
        splits=("train", "test"),
        basket_size=IMAGE_BASKET,
        base=MNIST_MIRROR,
    ),
    "fashion_mnist": Images(
        name="fashion_mnist",
        label="Fashion-MNIST",
        title="70,000 clothing photographs, 28x28 greyscale, 10 classes",
        licence="MIT",
        source="https://github.com/zalandoresearch/fashion-mnist",
        classes=(
            "t_shirt_top",
            "trouser",
            "pullover",
            "dress",
            "coat",
            "sandal",
            "shirt",
            "sneaker",
            "bag",
            "ankle_boot",
        ),
        modality="image",
        task="clothing classification",
        splits=("train", "test"),
        basket_size=IMAGE_BASKET,
        base="https://github.com/zalandoresearch/fashion-mnist/raw/master/data/fashion/",
    ),
    "kmnist": Images(
        name="kmnist",
        label="KMNIST",
        title="70,000 handwritten classical Japanese characters, 28x28 greyscale, 10 classes",
        licence="CC BY-SA 4.0",
        source="https://github.com/rois-codh/kmnist",
        classes=("o", "ki", "su", "tsu", "na", "ha", "ma", "ya", "re", "wo"),
        modality="image",
        task="character classification",
        splits=("train", "test"),
        basket_size=IMAGE_BASKET,
        base="http://codh.rois.ac.jp/kmnist/dataset/kmnist/",
    ),
    "cifar10": CIFAR(
        name="cifar10",
        label="CIFAR-10",
        title="60,000 photographs, 32x32 colour, 10 classes",
        licence="no formal licence; Krizhevsky asks that the tech report be cited",
        source="https://www.cs.toronto.edu/~kriz/cifar.html",
        classes=(
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
        ),
        modality="image",
        task="object classification",
        splits=("train", "test"),
        basket_size=IMAGE_BASKET,
        archive="https://www.cs.toronto.edu/~kriz/cifar-10-binary.tar.gz",
        source_bytes=170_052_171,
        files={
            "train": tuple(f"data_batch_{batch}.bin" for batch in range(1, 6)),
            "test": ("test_batch.bin",),
        },
        meta="batches.meta.txt",
    ),
    "cifar100": CIFAR(
        name="cifar100",
        label="CIFAR-100",
        title="60,000 photographs, 32x32 colour, 100 classes in 20 superclasses",
        licence="no formal licence; Krizhevsky asks that the tech report be cited",
        source="https://www.cs.toronto.edu/~kriz/cifar.html",
        classes=(),
        modality="image",
        task="fine and coarse object classification",
        splits=("train", "test"),
        basket_size=IMAGE_BASKET,
        archive="https://www.cs.toronto.edu/~kriz/cifar-100-binary.tar.gz",
        source_bytes=168_513_733,
        files={"train": ("train.bin",), "test": ("test.bin",)},
        meta="fine_label_names.txt",
        coarse="coarse_label_names.txt",
    ),
    "iris": Table(
        name="iris",
        label="Iris",
        title="150 iris flowers measured four ways, 3 species",
        licence="CC BY 4.0",
        source="https://archive.ics.uci.edu/dataset/53/iris",
        classes=("setosa", "versicolor", "virginica"),
        url="https://archive.ics.uci.edu/static/public/53/iris.zip",
        member="iris.data",
        fields=(
            ("sepal_length", "d"),
            ("sepal_width", "d"),
            ("petal_length", "d"),
            ("petal_width", "d"),
            ("species", "label"),
        ),
        labels={"Iris-setosa": 0, "Iris-versicolor": 1, "Iris-virginica": 2},
    ),
    "penguins": Table(
        name="penguins",
        label="Palmer penguins",
        title="344 penguins measured at Palmer Station, 3 species, with gaps",
        licence="CC0",
        source="https://allisonhorst.github.io/palmerpenguins/",
        classes=("adelie", "chinstrap", "gentoo"),
        url=(
            "https://raw.githubusercontent.com/allisonhorst/palmerpenguins/"
            "main/inst/extdata/penguins.csv"
        ),
        header=True,
        fields=(
            ("species", "label"),
            ("island", "island"),
            ("bill_length_mm", "d"),
            ("bill_depth_mm", "d"),
            ("flipper_length_mm", "d"),
            ("body_mass_g", "d"),
            ("sex", "sex"),
            ("year", "i"),
        ),
        labels={"Adelie": 0, "Chinstrap": 1, "Gentoo": 2},
        codes={
            "island": {"Biscoe": 0, "Dream": 1, "Torgersen": 2},
            "sex": {"female": 0, "male": 1},
        },
    ),
    "covertype": Table(
        name="covertype",
        label="Covertype",
        title="581,012 patches of Colorado forest, 54 features, 7 cover types",
        licence="CC BY 4.0",
        source="https://archive.ics.uci.edu/dataset/31/covertype",
        classes=(
            "spruce_fir",
            "lodgepole_pine",
            "ponderosa_pine",
            "cottonwood_willow",
            "aspen",
            "douglas_fir",
            "krummholz",
        ),
        basket_size=IMAGE_BASKET,
        url="https://archive.ics.uci.edu/static/public/31/covertype.zip",
        member="covtype.data.gz",
        fields=_COVER_FIELDS,
        labels={str(kind): kind - 1 for kind in range(1, 8)},
    ),
    "emnist": Images(
        name="emnist",
        label="EMNIST",
        title="131,600 handwritten characters, 28x28 greyscale, 47 balanced classes",
        licence=(
            "NIST Special Database 19, a work of the US federal government; "
            "the EMNIST paper asks to be cited"
        ),
        source="https://www.nist.gov/itl/products-and-services/emnist-dataset",
        classes=_EMNIST_CLASSES,
        modality="image",
        task="handwritten character classification",
        splits=("train", "test"),
        basket_size=IMAGE_BASKET,
        archive="https://biometrics.nist.gov/cs_links/EMNIST/gzip.zip",
        archive_bytes=561_753_746,
        files=_EMNIST_FILES,
        shade="greyscale, laid out as NIST wrote it with rows and columns swapped",
    ),
    "fsdd": Audio(
        name="fsdd",
        label="Free Spoken Digit Dataset v1.0.8",
        title="3,000 recordings of spoken digits, 8 kHz mono, 6 speakers, 10 classes",
        licence="CC BY-SA 4.0",
        source="https://zenodo.org/records/1342401",
        classes=tuple(str(digit) for digit in range(10)),
        modality="audio",
        task="spoken digit classification",
        creators=(
            "Zohar Jackson",
            "César Souza",
            "Jason Flaks",
            "Yuxin Pan",
            "Hereman Nicolas",
            "Adhish Thite",
        ),
        publisher="Free Spoken Digit Dataset project",
        origin="https://github.com/Jakobovski/free-spoken-digit-dataset/tree/v1.0.8",
        repository="GitHub",
        mirrors=(("Zenodo archived release v1.0.8", "https://zenodo.org/records/1342401"),),
        citation="https://doi.org/10.5281/zenodo.1342401",
        basket_size=IMAGE_BASKET,
        archive=(
            "https://codeload.github.com/Jakobovski/free-spoken-digit-dataset/"
            "zip/refs/tags/v1.0.8"
        ),
        folder="recordings",
        labels={str(digit): digit for digit in range(10)},
        speakers=("george", "jackson", "lucas", "nicolas", "theo", "yweweler"),
        splits=("train", "test"),
        rate=8000,
        samples=20000,
        archive_bytes=7_233_673,
        test_repetitions=5,
    ),
    "adult": Table(
        name="adult",
        label="Adult",
        title="48,842 census records, 14 features, 2 income classes, with gaps",
        licence="CC BY 4.0",
        source="https://archive.ics.uci.edu/dataset/2/adult",
        classes=("under_50k", "over_50k"),
        splits=("train", "test"),
        url="https://archive.ics.uci.edu/static/public/2/adult.zip",
        files={"train": "adult.data", "test": "adult.test"},
        comment="|",
        fields=_ADULT_FIELDS,
        labels={"<=50K": 0, ">50K": 1, "<=50K.": 0, ">50K.": 1},
        codes=_ADULT_CODES,
    ),
    "mushroom": Table(
        name="mushroom",
        label="Mushroom",
        title="8,124 mushrooms described 22 ways, edible or poisonous, with gaps",
        licence="CC BY 4.0",
        source="https://archive.ics.uci.edu/dataset/73/mushroom",
        classes=("edible", "poisonous"),
        url="https://archive.ics.uci.edu/static/public/73/mushroom.zip",
        member="agaricus-lepiota.data",
        fields=(("edibility", "label"), *((name, name) for name, _ in _MUSHROOM)),
        labels={"e": 0, "p": 1},
        codes=dict(_MUSHROOM),
    ),
    "letter": Table(
        name="letter",
        label="Letter Recognition",
        title="20,000 printed capitals measured 16 ways, 26 classes",
        licence="CC BY 4.0",
        source="https://archive.ics.uci.edu/dataset/59/letter+recognition",
        classes=tuple(chr(letter) for letter in range(ord("a"), ord("z") + 1)),
        url="https://archive.ics.uci.edu/static/public/59/letter+recognition.zip",
        member="letter-recognition.data",
        fields=_LETTER_FIELDS,
        labels={chr(ord("A") + at): at for at in range(26)},
    ),
    "digits": Table(
        name="digits",
        label="Optical Digits",
        title="5,620 handwritten digits as 8x8 counts of ink, 10 classes",
        licence="CC BY 4.0",
        source="https://archive.ics.uci.edu/dataset/80/optical+recognition+of+handwritten+digits",
        classes=tuple(str(digit) for digit in range(10)),
        splits=("train", "test"),
        url=(
            "https://archive.ics.uci.edu/static/public/80/"
            "optical+recognition+of+handwritten+digits.zip"
        ),
        files={"train": "optdigits.tra", "test": "optdigits.tes"},
        fields=_DIGIT_FIELDS,
        labels={str(digit): digit for digit in range(10)},
    ),
    "wine": Table(
        name="wine",
        label="Wine",
        title="178 wines analysed 13 ways, 3 cultivars",
        licence="CC BY 4.0",
        source="https://archive.ics.uci.edu/dataset/109/wine",
        classes=("cultivar_1", "cultivar_2", "cultivar_3"),
        url="https://archive.ics.uci.edu/ml/machine-learning-databases/wine/wine.data",
        fields=_WINE_FIELDS,
        labels={"1": 0, "2": 1, "3": 2},
    ),
    "breast_cancer": Table(
        name="breast_cancer",
        label="Breast Cancer Wisconsin",
        title="569 cell-nucleus images measured 30 ways, benign or malignant",
        licence="CC BY 4.0",
        source="https://archive.ics.uci.edu/dataset/17/breast+cancer+wisconsin+diagnostic",
        classes=("benign", "malignant"),
        url="https://archive.ics.uci.edu/static/public/17/breast+cancer+wisconsin+diagnostic.zip",
        member="wdbc.data",
        fields=_WDBC_FIELDS,
        labels={"B": 0, "M": 1},
    ),
    "dry_bean": Table(
        name="dry_bean",
        label="Dry Bean",
        title="13,611 beans measured 16 ways from photographs, 7 varieties",
        licence="CC BY 4.0",
        source="https://archive.ics.uci.edu/dataset/602/dry+bean+dataset",
        classes=("seker", "barbunya", "bombay", "cali", "horoz", "sira", "dermason"),
        url="https://archive.ics.uci.edu/static/public/602/dry+bean+dataset.zip",
        member="DryBeanDataset/Dry_Bean_Dataset.arff",
        arff=True,
        fields=_BEAN_FIELDS,
        labels={
            "SEKER": 0,
            "BARBUNYA": 1,
            "BOMBAY": 2,
            "CALI": 3,
            "HOROZ": 4,
            "SIRA": 5,
            "DERMASON": 6,
        },
    ),
    "miniboone": Matrix(
        name="miniboone",
        label="MiniBooNE",
        title="130,064 particle-identification events measured 50 ways, signal or background",
        licence="CC BY 4.0",
        source="https://archive.ics.uci.edu/dataset/199/miniboone+particle+identification",
        classes=("electron_neutrino", "muon_neutrino"),
        basket_size=IMAGE_BASKET,
        url="https://archive.ics.uci.edu/static/public/199/miniboone+particle+identification.zip",
        files={"all": "MiniBooNE_PID.txt"},
        width=50,
        counts=True,
    ),
    "har": Matrix(
        name="har",
        label="Human Activity Recognition",
        title="10,299 windows of phone accelerometer and gyroscope, 561 features, 6 activities",
        licence="CC BY 4.0",
        source="https://archive.ics.uci.edu/dataset/240/human+activity+recognition+using+smartphones",
        classes=_HAR_ACTIVITIES,
        splits=("train", "test"),
        basket_size=IMAGE_BASKET,
        url=(
            "https://archive.ics.uci.edu/static/public/240/"
            "human+activity+recognition+using+smartphones.zip"
        ),
        inner="UCI HAR Dataset.zip",
        files={split: f"UCI HAR Dataset/{split}/X_{split}.txt" for split in ("train", "test")},
        label_files={
            split: f"UCI HAR Dataset/{split}/y_{split}.txt" for split in ("train", "test")
        },
        labels={str(at + 1): at for at in range(6)},
        beside={
            "subject": {
                split: f"UCI HAR Dataset/{split}/subject_{split}.txt" for split in ("train", "test")
            }
        },
        width=561,
    ),
    "semeion": Matrix(
        name="semeion",
        label="Semeion",
        title="1,593 handwritten digits as 16x16 black and white, 10 classes",
        licence="CC BY 4.0",
        source="https://archive.ics.uci.edu/dataset/235/semeion+handwritten+digit",
        classes=tuple(str(digit) for digit in range(10)),
        basket_size=IMAGE_BASKET,
        url="https://archive.ics.uci.edu/ml/machine-learning-databases/semeion/semeion.data",
        files={"all": ""},
        width=256,
        column="image",
        kind="B",
        onehot=10,
    ),
    "sms_spam": Table(
        name="sms_spam",
        label="SMS Spam Collection",
        title="5,574 text messages, ham or spam",
        licence="CC BY 4.0",
        source="https://archive.ics.uci.edu/dataset/228/sms+spam+collection",
        classes=("ham", "spam"),
        url="https://archive.ics.uci.edu/static/public/228/sms+spam+collection.zip",
        member="SMSSpamCollection",
        delimiter="\t",
        quoted=False,
        text_size=1024,
        fields=(("kind", "label"), ("message", "text")),
        labels={"ham": 0, "spam": 1},
    ),
    "wine_quality": Table(
        name="wine_quality",
        label="Wine Quality",
        title="6,497 Portuguese wines analysed 11 ways and scored out of ten",
        licence="CC BY 4.0",
        source="https://archive.ics.uci.edu/dataset/186/wine+quality",
        classes=tuple(f"quality_{score}" for score in range(3, 10)),
        splits=("red", "white"),
        url="https://archive.ics.uci.edu/static/public/186/wine+quality.zip",
        files={colour: f"winequality-{colour}.csv" for colour in ("red", "white")},
        delimiter=";",
        header=True,
        fields=_WINE_QUALITY_FIELDS,
        labels={str(score): score - 3 for score in range(3, 10)},
    ),
    "spambase": Table(
        name="spambase",
        label="Spambase",
        title="4,601 e-mails counted 57 ways, spam or not",
        licence="CC BY 4.0",
        source="https://archive.ics.uci.edu/dataset/94/spambase",
        classes=("not_spam", "spam"),
        url="https://archive.ics.uci.edu/static/public/94/spambase.zip",
        member="spambase.data",
        fields=_SPAM_FIELDS,
        labels={"0": 0, "1": 1},
    ),
    "ionosphere": Table(
        name="ionosphere",
        label="Ionosphere",
        title="351 radar returns from the ionosphere measured 34 ways, good or bad",
        licence="CC BY 4.0",
        source="https://archive.ics.uci.edu/dataset/52/ionosphere",
        classes=("good", "bad"),
        url="https://archive.ics.uci.edu/static/public/52/ionosphere.zip",
        member="ionosphere.data",
        fields=_IONOSPHERE_FIELDS,
        labels={"g": 0, "b": 1},
    ),
    "glass": Table(
        name="glass",
        label="Glass Identification",
        title="214 fragments of glass measured 9 ways, 6 kinds",
        licence="CC BY 4.0",
        source="https://archive.ics.uci.edu/dataset/42/glass+identification",
        classes=(
            "building_windows_float",
            "building_windows_nonfloat",
            "vehicle_windows_float",
            "containers",
            "tableware",
            "headlamps",
        ),
        url="https://archive.ics.uci.edu/static/public/42/glass+identification.zip",
        member="glass.data",
        fields=_GLASS_FIELDS,
        labels={"1": 0, "2": 1, "3": 2, "5": 3, "6": 4, "7": 5},
    ),
    "abalone": Table(
        name="abalone",
        label="Abalone",
        title="4,177 abalone measured 8 ways and counted for rings, 3 sexes",
        licence="CC BY 4.0",
        source="https://archive.ics.uci.edu/dataset/1/abalone",
        classes=("male", "female", "infant"),
        url="https://archive.ics.uci.edu/static/public/1/abalone.zip",
        member="abalone.data",
        fields=_ABALONE_FIELDS,
        labels={"M": 0, "F": 1, "I": 2},
    ),
    "banknote": Table(
        name="banknote",
        label="Banknote Authentication",
        title="1,372 photographed banknotes measured 4 ways, 2 classes",
        licence="CC BY 4.0",
        source="https://archive.ics.uci.edu/dataset/267/banknote+authentication",
        classes=("class_0", "class_1"),
        url="https://archive.ics.uci.edu/static/public/267/banknote+authentication.zip",
        member="data_banknote_authentication.txt",
        fields=_BANKNOTE_FIELDS,
        labels={"0": 0, "1": 1},
    ),
    "seeds": Table(
        name="seeds",
        label="Seeds",
        title="210 wheat kernels measured 7 ways, 3 varieties",
        licence="CC BY 4.0",
        source="https://archive.ics.uci.edu/dataset/236/seeds",
        classes=("kama", "rosa", "canadian"),
        url="https://archive.ics.uci.edu/static/public/236/seeds.zip",
        member="seeds_dataset.txt",
        delimiter=None,
        fields=_SEED_FIELDS,
        labels={"1": 0, "2": 1, "3": 2},
    ),
    "magic": Table(
        name="magic",
        label="MAGIC Gamma Telescope",
        title="19,020 air showers seen by a Cherenkov telescope, 2 classes",
        licence="CC BY 4.0",
        source="https://archive.ics.uci.edu/dataset/159/magic+gamma+telescope",
        classes=("gamma", "hadron"),
        url="https://archive.ics.uci.edu/static/public/159/magic+gamma+telescope.zip",
        member="magic04.data",
        fields=_MAGIC_FIELDS,
        labels={"g": 0, "h": 1},
    ),
    "htru2": Table(
        name="htru2",
        label="HTRU2",
        title="17,898 pulsar candidates from a radio survey, 2 classes",
        licence="CC BY 4.0",
        source="https://archive.ics.uci.edu/dataset/372/htru2",
        classes=("not_pulsar", "pulsar"),
        url="https://archive.ics.uci.edu/static/public/372/htru2.zip",
        member="HTRU_2.csv",
        fields=_HTRU_FIELDS,
        labels={"0": 0, "1": 1},
    ),
    "auto_mpg": Table(
        name="auto_mpg",
        label="Auto MPG",
        title="398 cars of the 1970s and how far they went on a gallon",
        licence="CC BY 4.0",
        source="https://archive.ics.uci.edu/dataset/9/auto+mpg",
        classes=(),
        url="https://archive.ics.uci.edu/static/public/9/auto+mpg.zip",
        member="auto-mpg.data",
        delimiter=None,
        tail=9,
        text_size=48,
        fields=_MPG_FIELDS,
    ),
    "bike_sharing": Table(
        name="bike_sharing",
        label="Bike Sharing",
        title="17,379 hours of a bicycle hire scheme, and how many were taken out",
        licence="CC BY 4.0",
        source="https://archive.ics.uci.edu/dataset/275/bike+sharing+dataset",
        classes=(),
        url="https://archive.ics.uci.edu/static/public/275/bike+sharing+dataset.zip",
        member="hour.csv",
        header=True,
        fields=_BIKE_FIELDS,
    ),
    "energy_efficiency": Table(
        name="energy_efficiency",
        label="Energy Efficiency",
        title="768 simulated buildings and the heating and cooling they need",
        licence="CC BY 4.0",
        source="https://archive.ics.uci.edu/dataset/242/energy+efficiency",
        classes=(),
        url="https://archive.ics.uci.edu/static/public/242/energy+efficiency.zip",
        member="ENB2012_data.xlsx",
        xlsx=True,
        header=True,
        fields=_ENERGY_FIELDS,
    ),
    "real_estate": Table(
        name="real_estate",
        label="Real Estate Valuation",
        title="414 flats sold in Taipei and what a unit of floor cost",
        licence="CC BY 4.0",
        source="https://archive.ics.uci.edu/dataset/477/real+estate+valuation+data+set",
        classes=(),
        url="https://archive.ics.uci.edu/static/public/477/real+estate+valuation+data+set.zip",
        member="Real estate valuation data set.xlsx",
        xlsx=True,
        header=True,
        fields=_ESTATE_FIELDS,
    ),
    "student": Table(
        name="student",
        label="Student Performance",
        title="1,044 pupils, 32 answers each, and the mark they finished on",
        licence="CC BY 4.0",
        source="https://archive.ics.uci.edu/dataset/320/student+performance",
        classes=(),
        splits=("maths", "portuguese"),
        url="https://archive.ics.uci.edu/static/public/320/student+performance.zip",
        inner="student.zip",
        files={"maths": "student-mat.csv", "portuguese": "student-por.csv"},
        delimiter=";",
        header=True,
        fields=_STUDENT_FIELDS,
        codes=_STUDENT_CODES,
    ),
    "heart_disease": Table(
        name="heart_disease",
        label="Heart Disease",
        title="920 patients from four hospitals, 5 degrees of narrowed arteries",
        licence="CC BY 4.0",
        source="https://archive.ics.uci.edu/dataset/45/heart+disease",
        classes=("none", "stage_1", "stage_2", "stage_3", "stage_4"),
        splits=("cleveland", "hungary", "switzerland", "long_beach"),
        url="https://archive.ics.uci.edu/static/public/45/heart+disease.zip",
        files={
            "cleveland": "processed.cleveland.data",
            "hungary": "processed.hungarian.data",
            "switzerland": "processed.switzerland.data",
            "long_beach": "processed.va.data",
        },
        fields=_HEART_FIELDS,
        labels={str(stage): stage for stage in range(5)},
    ),
    "car_evaluation": Table(
        name="car_evaluation",
        label="Car Evaluation",
        title="1,728 cars described six ways and judged acceptable or not",
        licence="CC BY 4.0",
        source="https://archive.ics.uci.edu/dataset/19/car+evaluation",
        classes=("unacceptable", "acceptable", "good", "very_good"),
        url="https://archive.ics.uci.edu/static/public/19/car+evaluation.zip",
        member="car.data",
        fields=_CAR_FIELDS,
        labels={"unacc": 0, "acc": 1, "good": 2, "vgood": 3},
        codes=_CAR_CODES,
    ),
    "yeast": Table(
        name="yeast",
        label="Yeast",
        title="1,484 yeast proteins measured 8 ways, 10 places in the cell",
        licence="CC BY 4.0",
        source="https://archive.ics.uci.edu/dataset/110/yeast",
        classes=(
            "cytosol",
            "nucleus",
            "mitochondria",
            "membrane_no_signal",
            "membrane_uncleaved_signal",
            "membrane_cleaved_signal",
            "extracellular",
            "vacuole",
            "peroxisome",
            "endoplasmic_reticulum",
        ),
        url="https://archive.ics.uci.edu/static/public/110/yeast.zip",
        member="yeast.data",
        delimiter=None,
        text_size=16,
        fields=_YEAST_FIELDS,
        labels=_YEAST_SITES,
    ),
    "airfoil": Table(
        name="airfoil",
        label="Airfoil Self-Noise",
        title="1,503 wind tunnel runs and how loud the aerofoil was",
        licence="CC BY 4.0",
        source="https://archive.ics.uci.edu/dataset/291/airfoil+self+noise",
        classes=(),
        url="https://archive.ics.uci.edu/static/public/291/airfoil+self+noise.zip",
        member="airfoil_self_noise.dat",
        delimiter=None,
        fields=_AIRFOIL_FIELDS,
    ),
    "automobile": Table(
        name="automobile",
        label="Automobile",
        title="205 cars imported into America in 1985 and what they cost",
        licence="CC BY 4.0",
        source="https://archive.ics.uci.edu/dataset/10/automobile",
        classes=(),
        url="https://archive.ics.uci.edu/static/public/10/automobile.zip",
        member="automobile/imports-85.data",
        text_size=16,
        fields=_AUTOMOBILE_FIELDS,
        codes=_AUTOMOBILE_CODES,
    ),
    "balance_scale": Table(
        name="balance_scale",
        label="Balance Scale",
        title="625 balances and which way each one tips, 3 classes",
        licence="CC BY 4.0",
        source="https://archive.ics.uci.edu/dataset/12/balance+scale",
        classes=("balanced", "left", "right"),
        url="https://archive.ics.uci.edu/static/public/12/balance+scale.zip",
        member="balance-scale.data",
        fields=_BALANCE_FIELDS,
        labels={"B": 0, "L": 1, "R": 2},
    ),
    "bank_marketing": Table(
        name="bank_marketing",
        label="Bank Marketing",
        title="45,211 sales calls and whether the customer took the deposit",
        licence="CC BY 4.0",
        source="https://archive.ics.uci.edu/dataset/222/bank+marketing",
        classes=("no_deposit", "deposit"),
        url="https://archive.ics.uci.edu/static/public/222/bank+marketing.zip",
        inner="bank.zip",
        member="bank-full.csv",
        delimiter=";",
        header=True,
        fields=_BANK_FIELDS,
        labels={"no": 0, "yes": 1},
        codes=_BANK_CODES,
    ),
    "blood_transfusion": Table(
        name="blood_transfusion",
        label="Blood Transfusion",
        title="748 blood donors and whether each gave again, 2 classes",
        licence="CC BY 4.0",
        source="https://archive.ics.uci.edu/dataset/176/blood+transfusion+service+center",
        classes=("did_not_give", "gave"),
        url="https://archive.ics.uci.edu/static/public/176/blood+transfusion+service+center.zip",
        member="transfusion.data",
        header=True,
        fields=_BLOOD_FIELDS,
        labels={"0": 0, "1": 1},
    ),
    "climate_crashes": Table(
        name="climate_crashes",
        label="Climate Model Crashes",
        title="540 runs of an ocean model and whether each one finished",
        licence="CC BY 4.0",
        source="https://archive.ics.uci.edu/dataset/252/climate+model+simulation+crashes",
        classes=("crashed", "finished"),
        url="https://archive.ics.uci.edu/static/public/252/climate+model+simulation+crashes.zip",
        member="pop_failures.dat",
        delimiter=None,
        header=True,
        fields=_CLIMATE_FIELDS,
        labels={"0": 0, "1": 1},
    ),
    "computer_hardware": Table(
        name="computer_hardware",
        label="Computer Hardware",
        title="209 mainframes of the 1980s and how fast each one was",
        licence="CC BY 4.0",
        source="https://archive.ics.uci.edu/dataset/29/computer+hardware",
        classes=(),
        url="https://archive.ics.uci.edu/static/public/29/computer+hardware.zip",
        member="machine.data",
        text_size=24,
        fields=_HARDWARE_FIELDS,
    ),
    "concrete_slump": Table(
        name="concrete_slump",
        label="Concrete Slump Test",
        title="103 concrete mixes and how each one flowed and held",
        licence="CC BY 4.0",
        source="https://archive.ics.uci.edu/dataset/182/concrete+slump+test",
        classes=(),
        url="https://archive.ics.uci.edu/static/public/182/concrete+slump+test.zip",
        member="slump_test.data",
        header=True,
        fields=_SLUMP_FIELDS,
    ),
    "contraceptive": Table(
        name="contraceptive",
        label="Contraceptive Method Choice",
        title="1,473 Indonesian couples and what they used, 3 classes",
        licence="CC BY 4.0",
        source="https://archive.ics.uci.edu/dataset/30/contraceptive+method+choice",
        classes=("none", "long_term", "short_term"),
        url="https://archive.ics.uci.edu/static/public/30/contraceptive+method+choice.zip",
        member="cmc.data",
        fields=_CMC_FIELDS,
        labels={"1": 0, "2": 1, "3": 2},
    ),
    "dermatology": Table(
        name="dermatology",
        label="Dermatology",
        title="366 patients with one of 6 red scaly skin diseases",
        licence="CC BY 4.0",
        source="https://archive.ics.uci.edu/dataset/33/dermatology",
        classes=(
            "psoriasis",
            "seboreic_dermatitis",
            "lichen_planus",
            "pityriasis_rosea",
            "chronic_dermatitis",
            "pityriasis_rubra_pilaris",
        ),
        url="https://archive.ics.uci.edu/static/public/33/dermatology.zip",
        member="dermatology.data",
        fields=_DERM_FIELDS,
        labels={str(which): which - 1 for which in range(1, 7)},
    ),
    "diabetes_risk": Table(
        name="diabetes_risk",
        label="Early Stage Diabetes Risk",
        title="520 patients asked about 14 symptoms, 2 classes",
        licence="CC BY 4.0",
        source="https://archive.ics.uci.edu/dataset/529/early+stage+diabetes+risk+prediction+dataset",
        classes=("negative", "positive"),
        url="https://archive.ics.uci.edu/static/public/529/early+stage+diabetes+risk+prediction+dataset.zip",
        member="diabetes_data_upload.csv",
        header=True,
        fields=_DIABETES_FIELDS,
        labels={"Negative": 0, "Positive": 1},
        codes={"sex": _SEXES, "yesno": _YES_NO_CAPS},
    ),
    "ecoli": Table(
        name="ecoli",
        label="Ecoli",
        title="336 E. coli proteins measured 7 ways, 8 places in the cell",
        licence="CC BY 4.0",
        source="https://archive.ics.uci.edu/dataset/39/ecoli",
        classes=(
            "cytoplasm",
            "inner_membrane_no_signal",
            "periplasm",
            "inner_membrane_uncleaved_signal",
            "outer_membrane",
            "outer_membrane_lipoprotein",
            "inner_membrane_lipoprotein",
            "inner_membrane_cleaved_signal",
        ),
        url="https://archive.ics.uci.edu/static/public/39/ecoli.zip",
        member="ecoli.data",
        delimiter=None,
        text_size=16,
        fields=_ECOLI_FIELDS,
        labels={
            name: at for at, name in enumerate(("cp", "im", "pp", "imU", "om", "omL", "imL", "imS"))
        },
    ),
    "fertility": Table(
        name="fertility",
        label="Fertility",
        title="100 men, 9 questions each, and their semen analysis",
        licence="CC BY 4.0",
        source="https://archive.ics.uci.edu/dataset/244/fertility",
        classes=("normal", "altered"),
        url="https://archive.ics.uci.edu/static/public/244/fertility.zip",
        member="fertility_Diagnosis.txt",
        fields=_FERTILITY_FIELDS,
        labels={"N": 0, "O": 1},
    ),
    "forest_fires": Table(
        name="forest_fires",
        label="Forest Fires",
        title="517 fires in a Portuguese park and how far each one spread",
        licence="CC BY 4.0",
        source="https://archive.ics.uci.edu/dataset/162/forest+fires",
        classes=(),
        url="https://archive.ics.uci.edu/static/public/162/forest+fires.zip",
        member="forestfires.csv",
        header=True,
        fields=_FIRE_FIELDS,
        codes={"month": _MONTHS, "day": _DAYS},
    ),
    "garment_productivity": Table(
        name="garment_productivity",
        label="Garment Worker Productivity",
        title="1,197 team-days in a clothing factory and what each got done",
        licence="CC BY 4.0",
        source="https://archive.ics.uci.edu/dataset/597/productivity+prediction+of+garment+employees",
        classes=(),
        url="https://archive.ics.uci.edu/static/public/597/productivity+prediction+of+garment+employees.zip",
        member="garments_worker_productivity.csv",
        header=True,
        dates="%m/%d/%Y",
        fields=_GARMENT_FIELDS,
        codes={
            "quarter": ("Quarter1", "Quarter2", "Quarter3", "Quarter4", "Quarter5"),
            "department": ("finishing", "sweing"),
            "weekday": _WEEKDAYS,
        },
    ),
    "german_credit": Table(
        name="german_credit",
        label="Statlog German Credit",
        title="1,000 loan applications judged good or bad, 2 classes",
        licence="CC BY 4.0",
        source="https://archive.ics.uci.edu/dataset/144/statlog+german+credit+data",
        classes=("good", "bad"),
        url="https://archive.ics.uci.edu/static/public/144/statlog+german+credit+data.zip",
        member="german.data",
        delimiter=None,
        fields=_GERMAN_FIELDS,
        labels={"1": 0, "2": 1},
        codes=_GERMAN_CODES,
    ),
    "haberman": Table(
        name="haberman",
        label="Haberman's Survival",
        title="306 breast cancer operations and who was alive 5 years on",
        licence="CC BY 4.0",
        source="https://archive.ics.uci.edu/dataset/43/haberman+s+survival",
        classes=("survived", "died"),
        url="https://archive.ics.uci.edu/static/public/43/haberman+s+survival.zip",
        member="haberman.data",
        fields=_HABERMAN_FIELDS,
        labels={"1": 0, "2": 1},
    ),
    "heart_failure": Table(
        name="heart_failure",
        label="Heart Failure Clinical Records",
        title="299 heart failure patients and who survived the follow-up",
        licence="CC BY 4.0",
        source="https://archive.ics.uci.edu/dataset/519/heart+failure+clinical+records",
        classes=("survived", "died"),
        url="https://archive.ics.uci.edu/static/public/519/heart+failure+clinical+records.zip",
        member="heart_failure_clinical_records_dataset.csv",
        header=True,
        fields=_FAILURE_FIELDS,
        labels={"0": 0, "1": 1},
    ),
    "hepatitis": Table(
        name="hepatitis",
        label="Hepatitis",
        title="155 hepatitis patients, 19 findings each, 2 classes",
        licence="CC BY 4.0",
        source="https://archive.ics.uci.edu/dataset/46/hepatitis",
        classes=("died", "lived"),
        url="https://archive.ics.uci.edu/static/public/46/hepatitis.zip",
        member="hepatitis.data",
        fields=_HEPATITIS_FIELDS,
        labels={"1": 0, "2": 1},
    ),
    "image_segmentation": Table(
        name="image_segmentation",
        label="Image Segmentation",
        title="2,310 patches of outdoor photographs, 7 things they are of",
        licence="CC BY 4.0",
        source="https://archive.ics.uci.edu/dataset/50/image+segmentation",
        classes=("brickface", "sky", "foliage", "cement", "window", "path", "grass"),
        splits=("train", "test"),
        url="https://archive.ics.uci.edu/static/public/50/image+segmentation.zip",
        files={"train": "segmentation.data", "test": "segmentation.test"},
        comment=";",
        header=True,
        fields=_SEGMENT_FIELDS,
        labels={
            name.upper(): at
            for at, name in enumerate(
                ("brickface", "sky", "foliage", "cement", "window", "path", "grass")
            )
        },
    ),
    "indian_liver": Table(
        name="indian_liver",
        label="Indian Liver Patient",
        title="583 patients from Andhra Pradesh, 2 classes",
        licence="CC BY 4.0",
        source="https://archive.ics.uci.edu/dataset/225/ilpd+indian+liver+patient+dataset",
        classes=("liver_patient", "not_a_patient"),
        url="https://archive.ics.uci.edu/static/public/225/ilpd+indian+liver+patient+dataset.zip",
        member="Indian Liver Patient Dataset (ILPD).csv",
        fields=_ILPD_FIELDS,
        labels={"1": 0, "2": 1},
        codes={"sex": _SEXES},
    ),
    "liver_disorders": Table(
        name="liver_disorders",
        label="Liver Disorders",
        title="345 blood tests and the drinking to predict from them",
        licence="CC BY 4.0",
        source="https://archive.ics.uci.edu/dataset/60/liver+disorders",
        classes=(),
        url="https://archive.ics.uci.edu/static/public/60/liver+disorders.zip",
        member="bupa.data",
        fields=_BUPA_FIELDS,
    ),
    "lymphography": Table(
        name="lymphography",
        label="Lymphography",
        title="148 lymph node X-rays read 18 ways, 4 classes",
        licence="CC BY 4.0",
        source="https://archive.ics.uci.edu/dataset/63/lymphography",
        classes=("normal", "metastases", "malignant_lymphoma", "fibrosis"),
        url="https://archive.ics.uci.edu/static/public/63/lymphography.zip",
        member="lymphography.data",
        fields=_LYMPH_FIELDS,
        labels={str(which): which - 1 for which in range(1, 5)},
    ),
    "mammographic_mass": Table(
        name="mammographic_mass",
        label="Mammographic Mass",
        title="961 lumps seen on a mammogram, benign or malignant",
        licence="CC BY 4.0",
        source="https://archive.ics.uci.edu/dataset/161/mammographic+mass",
        classes=("benign", "malignant"),
        url="https://archive.ics.uci.edu/static/public/161/mammographic+mass.zip",
        member="mammographic_masses.data",
        fields=_MAMMOGRAM_FIELDS,
        labels={"0": 0, "1": 1},
    ),
    "maternal_health": Table(
        name="maternal_health",
        label="Maternal Health Risk",
        title="1,014 pregnancies seen in rural clinics, 3 degrees of risk",
        licence="CC BY 4.0",
        source="https://archive.ics.uci.edu/dataset/863/maternal+health+risk",
        classes=("low_risk", "mid_risk", "high_risk"),
        url="https://archive.ics.uci.edu/static/public/863/maternal+health+risk.zip",
        member="Maternal Health Risk Data Set.csv",
        header=True,
        fields=_MATERNAL_FIELDS,
        labels={"low risk": 0, "mid risk": 1, "high risk": 2},
    ),
    "nursery": Table(
        name="nursery",
        label="Nursery",
        title="12,960 nursery applications ranked 5 ways",
        licence="CC BY 4.0",
        source="https://archive.ics.uci.edu/dataset/76/nursery",
        classes=("not_recom", "recommend", "very_recom", "priority", "spec_prior"),
        url="https://archive.ics.uci.edu/static/public/76/nursery.zip",
        member="nursery.data",
        fields=_NURSERY_FIELDS,
        labels={
            name: at
            for at, name in enumerate(
                ("not_recom", "recommend", "very_recom", "priority", "spec_prior")
            )
        },
        codes=_NURSERY_CODES,
    ),
    "occupancy": Table(
        name="occupancy",
        label="Occupancy Detection",
        title="20,560 minutes in an office and whether anyone was in it",
        licence="CC BY 4.0",
        source="https://archive.ics.uci.edu/dataset/357/occupancy+detection",
        classes=("empty", "occupied"),
        splits=("train", "test", "second_test"),
        url="https://archive.ics.uci.edu/static/public/357/occupancy+detection.zip",
        files={
            "train": "datatraining.txt",
            "test": "datatest.txt",
            "second_test": "datatest2.txt",
        },
        header=True,
        dates="%Y-%m-%d %H:%M:%S",
        fields=_OCCUPANCY_FIELDS,
        labels={"0": 0, "1": 1},
    ),
    "online_shoppers": Table(
        name="online_shoppers",
        label="Online Shoppers Intention",
        title="12,330 shopping sessions and which of them ended in a sale",
        licence="CC BY 4.0",
        source="https://archive.ics.uci.edu/dataset/468/online+shoppers+purchasing+intention+dataset",
        classes=("no_purchase", "purchase"),
        url="https://archive.ics.uci.edu/static/public/468/online+shoppers+purchasing+intention+dataset.zip",
        member="online_shoppers_intention.csv",
        header=True,
        fields=_SHOPPER_FIELDS,
        labels={"FALSE": 0, "TRUE": 1},
        codes=_SHOPPER_CODES,
    ),
    "parkinsons": Table(
        name="parkinsons",
        label="Parkinsons",
        title="195 voice recordings, 22 measurements each, 2 classes",
        licence="CC BY 4.0",
        source="https://archive.ics.uci.edu/dataset/174/parkinsons",
        classes=("healthy", "parkinsons"),
        url="https://archive.ics.uci.edu/static/public/174/parkinsons.zip",
        member="parkinsons.data",
        header=True,
        text_size=24,
        fields=_PARKINSONS_FIELDS,
        labels={"0": 0, "1": 1},
    ),
    "parkinsons_telemonitoring": Table(
        name="parkinsons_telemonitoring",
        label="Parkinsons Telemonitoring",
        title="5,875 recordings made at home and the two scores to predict",
        licence="CC BY 4.0",
        source="https://archive.ics.uci.edu/dataset/189/parkinsons+telemonitoring",
        classes=(),
        url="https://archive.ics.uci.edu/static/public/189/parkinsons+telemonitoring.zip",
        member="parkinsons_updrs.data",
        header=True,
        fields=_TELEMONITORING_FIELDS,
    ),
    "pendigits": Table(
        name="pendigits",
        label="Pen-Based Digits",
        title="10,992 digits written on a tablet, 8 points each, 10 classes",
        licence="CC BY 4.0",
        source="https://archive.ics.uci.edu/dataset/81/pen+based+recognition+of+handwritten+digits",
        classes=tuple(str(digit) for digit in range(10)),
        splits=("train", "test"),
        url="https://archive.ics.uci.edu/static/public/81/pen+based+recognition+of+handwritten+digits.zip",
        files={"train": "pendigits.tra", "test": "pendigits.tes"},
        fields=_PENDIGIT_FIELDS,
        labels={str(digit): digit for digit in range(10)},
    ),
    "phishing": Table(
        name="phishing",
        label="Phishing Websites",
        title="11,055 websites scored 30 ways, phishing or legitimate",
        licence="CC BY 4.0",
        source="https://archive.ics.uci.edu/dataset/327/phishing+websites",
        classes=("phishing", "legitimate"),
        url="https://archive.ics.uci.edu/static/public/327/phishing+websites.zip",
        member="Training Dataset.arff",
        arff=True,
        fields=_PHISHING_FIELDS,
        labels={"-1": 0, "1": 1},
    ),
    "power_plant": Table(
        name="power_plant",
        label="Combined Cycle Power Plant",
        title="9,568 hours of a gas turbine and the power it put out",
        licence="CC BY 4.0",
        source="https://archive.ics.uci.edu/dataset/294/combined+cycle+power+plant",
        classes=(),
        url="https://archive.ics.uci.edu/static/public/294/combined+cycle+power+plant.zip",
        member="CCPP/Folds5x2_pp.xlsx",
        xlsx=True,
        header=True,
        fields=_POWER_FIELDS,
    ),
    "qsar_aquatic": Table(
        name="qsar_aquatic",
        label="QSAR Aquatic Toxicity",
        title="546 chemicals and the dose that kills half the water fleas",
        licence="CC BY 4.0",
        source="https://archive.ics.uci.edu/dataset/505/qsar+aquatic+toxicity",
        classes=(),
        url="https://archive.ics.uci.edu/static/public/505/qsar+aquatic+toxicity.zip",
        member="qsar_aquatic_toxicity.csv",
        delimiter=";",
        fields=_AQUATIC_FIELDS,
    ),
    "qsar_fish": Table(
        name="qsar_fish",
        label="QSAR Fish Toxicity",
        title="908 chemicals and the dose that kills half the minnows",
        licence="CC BY 4.0",
        source="https://archive.ics.uci.edu/dataset/504/qsar+fish+toxicity",
        classes=(),
        url="https://archive.ics.uci.edu/static/public/504/qsar+fish+toxicity.zip",
        member="qsar_fish_toxicity.csv",
        delimiter=";",
        fields=_FISH_FIELDS,
    ),
    "raisin": Table(
        name="raisin",
        label="Raisin",
        title="900 raisins measured off a photograph, 2 varieties",
        licence="CC BY 4.0",
        source="https://archive.ics.uci.edu/dataset/850/raisin",
        classes=("kecimen", "besni"),
        url="https://archive.ics.uci.edu/static/public/850/raisin.zip",
        inner="Raisin_Dataset.zip",
        member="Raisin_Dataset/Raisin_Dataset.arff",
        arff=True,
        fields=_RAISIN_FIELDS,
        labels={"Kecimen": 0, "Besni": 1},
    ),
    "rice": Table(
        name="rice",
        label="Rice",
        title="3,810 grains of rice measured off a photograph, 2 varieties",
        licence="CC BY 4.0",
        source="https://archive.ics.uci.edu/dataset/545/rice+cammeo+and+osmancik",
        classes=("cammeo", "osmancik"),
        url="https://archive.ics.uci.edu/static/public/545/rice+cammeo+and+osmancik.zip",
        member="Rice_Cammeo_Osmancik.arff",
        arff=True,
        fields=_RICE_FIELDS,
        labels={"Cammeo": 0, "Osmancik": 1},
    ),
    "satellite": Table(
        name="satellite",
        label="Statlog Landsat Satellite",
        title="6,435 Landsat neighbourhoods of 9 pixels, 6 kinds of ground",
        licence="CC BY 4.0",
        source="https://archive.ics.uci.edu/dataset/146/statlog+landsat+satellite",
        classes=(
            "red_soil",
            "cotton_crop",
            "grey_soil",
            "damp_grey_soil",
            "vegetation_stubble",
            "very_damp_grey_soil",
        ),
        splits=("train", "test"),
        url="https://archive.ics.uci.edu/static/public/146/statlog+landsat+satellite.zip",
        files={"train": "sat.trn", "test": "sat.tst"},
        delimiter=None,
        fields=_SATELLITE_FIELDS,
        labels={"1": 0, "2": 1, "3": 2, "4": 3, "5": 4, "7": 5},
    ),
    "seismic": Table(
        name="seismic",
        label="Seismic Bumps",
        title="2,584 shifts in a Polish coal mine, 2 classes",
        licence="CC BY 4.0",
        source="https://archive.ics.uci.edu/dataset/266/seismic+bumps",
        classes=("no_bump", "bump"),
        url="https://archive.ics.uci.edu/static/public/266/seismic+bumps.zip",
        member="seismic-bumps.arff",
        arff=True,
        fields=_SEISMIC_FIELDS,
        labels={"0": 0, "1": 1},
        codes={"hazard": ("a", "b", "c", "d"), "shift": ("W", "N")},
    ),
    "servo": Table(
        name="servo",
        label="Servo",
        title="167 servomechanisms and how long each took to settle",
        licence="CC BY 4.0",
        source="https://archive.ics.uci.edu/dataset/87/servo",
        classes=(),
        url="https://archive.ics.uci.edu/static/public/87/servo.zip",
        member="servo.data",
        fields=_SERVO_FIELDS,
        codes={"part": ("A", "B", "C", "D", "E")},
    ),
    "solar_flare": Table(
        name="solar_flare",
        label="Solar Flare",
        title="1,066 active regions on the sun, 7 modified Zurich classes",
        licence="CC BY 4.0",
        source="https://archive.ics.uci.edu/dataset/89/solar+flare",
        classes=tuple(f"class_{letter}" for letter in "abcdefh"),
        url="https://archive.ics.uci.edu/static/public/89/solar+flare.zip",
        member="flare.data2",
        delimiter=None,
        comment="*",
        fields=_FLARE_FIELDS,
        labels={letter.upper(): at for at, letter in enumerate("abcdefh")},
        codes={"spots": ("X", "R", "S", "A", "H", "K"), "spread": ("X", "O", "I", "C")},
    ),
    "sonar": Table(
        name="sonar",
        label="Sonar",
        title="208 sonar returns off a rock or a mine, 60 bands each",
        licence="CC BY 4.0",
        source="https://archive.ics.uci.edu/dataset/151/connectionist+bench+sonar+mines+vs+rocks",
        classes=("rock", "mine"),
        url="https://archive.ics.uci.edu/static/public/151/connectionist+bench+sonar+mines+vs+rocks.zip",
        member="sonar.all-data",
        fields=_SONAR_FIELDS,
        labels={"R": 0, "M": 1},
    ),
    "soybean": Table(
        name="soybean",
        label="Soybean (Large)",
        title="683 diseased soybean plants, 19 diseases",
        licence="CC BY 4.0",
        source="https://archive.ics.uci.edu/dataset/90/soybean+large",
        classes=tuple(
            name.replace("&", "and").replace("2-4-d", "two_four_d").replace("-", "_")
            for name in _SOYBEAN_DISEASES
        ),
        splits=("train", "test"),
        url="https://archive.ics.uci.edu/static/public/90/soybean+large.zip",
        files={"train": "soybean-large.data", "test": "soybean-large.test"},
        fields=_SOYBEAN_FIELDS,
        labels={name: at for at, name in enumerate(_SOYBEAN_DISEASES)},
    ),
    "statlog_heart": Table(
        name="statlog_heart",
        label="Statlog Heart",
        title="270 patients from the Cleveland heart study, 2 classes",
        licence="CC BY 4.0",
        source="https://archive.ics.uci.edu/dataset/145/statlog+heart",
        classes=("absent", "present"),
        url="https://archive.ics.uci.edu/static/public/145/statlog+heart.zip",
        member="heart.dat",
        delimiter=None,
        fields=_STATLOG_HEART_FIELDS,
        labels={"1": 0, "2": 1},
    ),
    "steel_industry": Table(
        name="steel_industry",
        label="Steel Industry Energy",
        title="35,040 quarter hours in a steel plant, 3 kinds of load",
        licence="CC BY 4.0",
        source="https://archive.ics.uci.edu/dataset/851/steel+industry+energy+consumption",
        classes=("light_load", "medium_load", "maximum_load"),
        url="https://archive.ics.uci.edu/static/public/851/steel+industry+energy+consumption.zip",
        member="Steel_industry_data.csv",
        header=True,
        dates="%d/%m/%Y %H:%M",
        fields=_STEEL_FIELDS,
        labels={"Light_Load": 0, "Medium_Load": 1, "Maximum_Load": 2},
        codes={"week": ("Weekday", "Weekend"), "weekday": _WEEKDAYS},
    ),
    "tic_tac_toe": Table(
        name="tic_tac_toe",
        label="Tic-Tac-Toe Endgame",
        title="958 finished games and whether x had won, 2 classes",
        licence="CC BY 4.0",
        source="https://archive.ics.uci.edu/dataset/101/tic+tac+toe+endgame",
        classes=("x_lost", "x_won"),
        url="https://archive.ics.uci.edu/static/public/101/tic+tac+toe+endgame.zip",
        member="tic-tac-toe.data",
        fields=_TICTACTOE_FIELDS,
        labels={"negative": 0, "positive": 1},
        codes={"square": {"x": 1, "o": -1, "b": 0}},
    ),
    "vertebral_column": Table(
        name="vertebral_column",
        label="Vertebral Column",
        title="310 lower spines measured 6 ways, 3 classes",
        licence="CC BY 4.0",
        source="https://archive.ics.uci.edu/dataset/212/vertebral+column",
        classes=("disc_hernia", "spondylolisthesis", "normal"),
        url="https://archive.ics.uci.edu/static/public/212/vertebral+column.zip",
        member="column_3C.dat",
        delimiter=None,
        fields=_VERTEBRAL_FIELDS,
        labels={"DH": 0, "SL": 1, "NO": 2},
    ),
    "wifi_localisation": Table(
        name="wifi_localisation",
        label="Wireless Indoor Localisation",
        title="2,000 readings of 7 wifi points, taken in 4 rooms",
        licence="CC BY 4.0",
        source="https://archive.ics.uci.edu/dataset/422/wireless+indoor+localization",
        classes=tuple(f"room_{at}" for at in range(1, 5)),
        url="https://archive.ics.uci.edu/static/public/422/wireless+indoor+localization.zip",
        member="wifi_localization.txt",
        delimiter=None,
        fields=_WIFI_FIELDS,
        labels={str(at): at - 1 for at in range(1, 5)},
    ),
    "yacht": Table(
        name="yacht",
        label="Yacht Hydrodynamics",
        title="308 towing tank runs and the resistance each hull made",
        licence="CC BY 4.0",
        source="https://archive.ics.uci.edu/dataset/243/yacht+hydrodynamics",
        classes=(),
        url="https://archive.ics.uci.edu/static/public/243/yacht+hydrodynamics.zip",
        member="yacht_hydrodynamics.data",
        delimiter=None,
        fields=_YACHT_FIELDS,
    ),
    "zoo": Table(
        name="zoo",
        label="Zoo",
        title="101 animals described 16 ways, 7 kinds of animal",
        licence="CC BY 4.0",
        source="https://archive.ics.uci.edu/dataset/111/zoo",
        classes=(
            "mammal",
            "bird",
            "reptile",
            "fish",
            "amphibian",
            "insect",
            "invertebrate",
        ),
        url="https://archive.ics.uci.edu/static/public/111/zoo.zip",
        member="zoo.data",
        text_size=16,
        fields=_ZOO_FIELDS,
        labels={str(which): which - 1 for which in range(1, 8)},
    ),
    "absenteeism_at_work": Table(
        name="absenteeism_at_work",
        label="Absenteeism at work",
        title="740 absences at a Brazilian courier firm and the hours each one lost",
        licence="CC BY 4.0",
        source="https://archive.ics.uci.edu/dataset/445/absenteeism+at+work",
        classes=(),
        url="https://archive.ics.uci.edu/static/public/445/data.csv",
        header=True,
        fields=_ABSENTEEISM_AT_WORK_FIELDS,
    ),
    "acute_inflammations": Table(
        name="acute_inflammations",
        label="Acute Inflammations",
        title="120 patients with urinary symptoms, judged for inflammation of the bladder",
        licence="CC BY 4.0",
        source="https://archive.ics.uci.edu/dataset/184/acute+inflammations",
        classes=("no", "yes"),
        url="https://archive.ics.uci.edu/static/public/184/data.csv",
        header=True,
        fields=_ACUTE_INFLAMMATIONS_FIELDS,
        labels={"no": 0, "yes": 1},
        codes={
            "nausea": ("no", "yes"),
        },
    ),
    "aids_clinical_trials": Table(
        name="aids_clinical_trials",
        label="AIDS Clinical Trials Group Study 175",
        title="2,139 people on HIV treatment and whose illness went on progressing",
        licence="CC BY 4.0",
        source="https://archive.ics.uci.edu/dataset/890/aids+clinical+trials+group+study+175",
        classes=("no_event", "progressed"),
        url="https://archive.ics.uci.edu/static/public/890/data.csv",
        header=True,
        fields=_AIDS_CLINICAL_TRIALS_FIELDS,
        labels={"0": 0, "1": 1},
    ),
    "android_permissions": Table(
        name="android_permissions",
        label="NATICUSdroid (Android Permissions)",
        title="29,332 Android apps described by the permissions they ask for, benign or malware",
        licence="CC BY 4.0",
        source="https://archive.ics.uci.edu/dataset/722/naticusdroid+android+permissions+dataset",
        classes=("benign", "malware"),
        url="https://archive.ics.uci.edu/static/public/722/data.csv",
        header=True,
        fields=_ANDROID_PERMISSIONS_FIELDS,
        labels={"0": 0, "1": 1},
    ),
    "annealing": Table(
        name="annealing",
        label="Annealing",
        title="898 steel coils, their chemistry, and the annealing class each took",
        licence="CC BY 4.0",
        source="https://archive.ics.uci.edu/dataset/3/annealing",
        classes=("class_1", "class_2", "class_3", "class_5", "class_u"),
        url="https://archive.ics.uci.edu/static/public/3/data.csv",
        header=True,
        fields=_ANNEALING_FIELDS,
        labels={"1": 0, "2": 1, "3": 2, "5": 3, "U": 4},
        codes={
            "famiily": ("TN", "ZS"),
            "product_type": ("C",),
            "steel": ("A", "K", "M", "R", "S", "V", "W"),
            "temper_rolling": ("T",),
            "condition": ("A", "S"),
            "non_ageing": ("N",),
            "surface_finish": ("P",),
            "surface_quality": ("D", "E", "F", "G"),
            "bc": ("Y",),
            "bw_me": ("B", "M"),
            "blue_bright_varn_clean": ("B", "C", "V"),
            "shape": ("COIL", "SHEET"),
            "oil": ("N", "Y"),
        },
    ),
    "appliances_energy": Table(
        name="appliances_energy",
        label="Appliances Energy Prediction",
        title="19,735 ten-minute readings of a low-energy house and the watt-hours it drew",
        licence="CC BY 4.0",
        source="https://archive.ics.uci.edu/dataset/374/appliances+energy+prediction",
        classes=(),
        url="https://archive.ics.uci.edu/static/public/374/data.csv",
        header=True,
        text_size=18,
        fields=_APPLIANCES_ENERGY_FIELDS,
    ),
    "auction_verification": Table(
        name="auction_verification",
        label="Auction Verification",
        title="2,043 runs of a model checker over simulated spectrum auctions, verified or not",
        licence="CC BY 4.0",
        source="https://archive.ics.uci.edu/dataset/713/auction+verification",
        classes=("false", "true"),
        url="https://archive.ics.uci.edu/static/public/713/data.csv",
        header=True,
        fields=_AUCTION_VERIFICATION_FIELDS,
        labels={"False": 0, "True": 1},
    ),
    "audiology": Table(
        name="audiology",
        label="Audiology (Standardized)",
        title="200 hearing tests and the diagnosis each led to, 24 classes",
        licence="CC BY 4.0",
        source="https://archive.ics.uci.edu/dataset/8/audiology+standardized",
        classes=(
            "acoustic_neuroma",
            "bells_palsy",
            "cochlear_age",
            "cochlear_age_and_noise",
            "cochlear_age_plus_poss_menieres",
            "cochlear_noise_and_heredity",
            "cochlear_poss_noise",
            "cochlear_unknown",
            "conductive_discontinuity",
            "conductive_fixation",
            "mixed_cochlear_age_fixation",
            "mixed_cochlear_age_otitis_media",
            "mixed_cochlear_age_s_om",
            "mixed_cochlear_unk_discontinuity",
            "mixed_cochlear_unk_fixation",
            "mixed_cochlear_unk_ser_om",
            "mixed_poss_central_om",
            "mixed_poss_noise_om",
            "normal_ear",
            "otitis_media",
            "poss_central",
            "possible_brainstem_disorder",
            "possible_menieres",
            "retrocochlear_unknown",
        ),
        url="https://archive.ics.uci.edu/static/public/8/data.csv",
        header=True,
        text_size=4,
        fields=_AUDIOLOGY_FIELDS,
        labels={
            "acoustic_neuroma": 0,
            "bells_palsy": 1,
            "cochlear_age": 2,
            "cochlear_age_and_noise": 3,
            "cochlear_age_plus_poss_menieres": 4,
            "cochlear_noise_and_heredity": 5,
            "cochlear_poss_noise": 6,
            "cochlear_unknown": 7,
            "conductive_discontinuity": 8,
            "conductive_fixation": 9,
            "mixed_cochlear_age_fixation": 10,
            "mixed_cochlear_age_otitis_media": 11,
            "mixed_cochlear_age_s_om": 12,
            "mixed_cochlear_unk_discontinuity": 13,
            "mixed_cochlear_unk_fixation": 14,
            "mixed_cochlear_unk_ser_om": 15,
            "mixed_poss_central_om": 16,
            "mixed_poss_noise_om": 17,
            "normal_ear": 18,
            "otitis_media": 19,
            "poss_central": 20,
            "possible_brainstem_disorder": 21,
            "possible_menieres": 22,
            "retrocochlear_unknown": 23,
        },
        codes={
            "age_gt_60": ("f", "t"),
            "air": ("mild", "moderate", "normal", "profound", "severe"),
            "ar_c": ("absent", "elevated", "normal"),
            "bone": ("mild", "moderate", "normal", "unmeasured"),
            "bser": ("degraded", "normal"),
            "m_cond_lt_1k": ("f",),
            "speech": ("good", "normal", "poor", "unmeasured", "very_good", "very_poor"),
            "tymp": ("a", "ad", "as", "b", "c"),
        },
    ),
    "autism_screening_adult": Table(
        name="autism_screening_adult",
        label="Autism Screening Adult",
        title="704 adults put through the autism screening questionnaire, 2 classes",
        licence="CC BY 4.0",
        source="https://archive.ics.uci.edu/dataset/426/autism+screening+adult",
        classes=("no", "yes"),
        url="https://archive.ics.uci.edu/static/public/426/data.csv",
        header=True,
        text_size=22,
        fields=_AUTISM_SCREENING_ADULT_FIELDS,
        labels={"NO": 0, "YES": 1},
        codes={
            "gender": ("f", "m"),
            "ethnicity": (
                "'Middle Eastern '",
                "'South Asian'",
                "Asian",
                "Black",
                "Hispanic",
                "Latino",
                "Others",
                "Pasifika",
                "Turkish",
                "White-European",
                "others",
            ),
            "jaundice": ("no", "yes"),
            "age_desc": ("'18 and more'",),
            "relation": ("'Health care professional'", "Others", "Parent", "Relative", "Self"),
        },
    ),
    "autism_screening_child": Table(
        name="autism_screening_child",
        label="Autistic Spectrum Disorder Screening Data for Children",
        title="292 children put through the autism screening questionnaire, 2 classes",
        licence="CC BY 4.0",
        source="https://archive.ics.uci.edu/dataset/419/autistic+spectrum+disorder+screening+data+for+children",
        classes=("no", "yes"),
        url="https://archive.ics.uci.edu/static/public/419/data.csv",
        header=True,
        text_size=23,
        fields=_AUTISM_SCREENING_CHILD_FIELDS,
        labels={"NO": 0, "YES": 1},
        codes={
            "gender": ("f", "m"),
            "ethnicity": (
                "'Middle Eastern '",
                "'South Asian'",
                "Asian",
                "Black",
                "Hispanic",
                "Latino",
                "Others",
                "Pasifika",
                "Turkish",
                "White-European",
            ),
            "jaundice": ("no", "yes"),
            "age_desc": ("'4-11 years'",),
            "relation": ("'Health care professional'", "Parent", "Relative", "Self", "self"),
        },
    ),
    "beijing_pm25": Table(
        name="beijing_pm25",
        label="Beijing PM2.5",
        title="43,824 hours of Beijing weather and the fine particles in the air",
        licence="CC BY 4.0",
        source="https://archive.ics.uci.edu/dataset/381/beijing+pm2+5+data",
        classes=(),
        url="https://archive.ics.uci.edu/static/public/381/data.csv",
        header=True,
        fields=_BEIJING_PM25_FIELDS,
        codes={
            "cbwd": ("NE", "NW", "SE", "cv"),
        },
    ),
    "bone_marrow_transplant": Table(
        name="bone_marrow_transplant",
        label="Bone marrow transplant: children",
        title="187 children given a bone marrow transplant, and who survived it",
        licence="CC BY 4.0",
        source="https://archive.ics.uci.edu/dataset/565/bone+marrow+transplant+children",
        classes=("alive", "died"),
        url="https://archive.ics.uci.edu/static/public/565/data.csv",
        header=True,
        fields=_BONE_MARROW_TRANSPLANT_FIELDS,
        labels={"0": 0, "1": 1},
        codes={
            "disease": ("ALL", "AML", "chronic", "lymphoma", "nonmalignant"),
        },
    ),
    "breast_cancer_coimbra": Table(
        name="breast_cancer_coimbra",
        label="Breast Cancer Coimbra",
        title="116 blood samples from Coimbra, healthy women and cancer patients",
        licence="CC BY 4.0",
        source="https://archive.ics.uci.edu/dataset/451/breast+cancer+coimbra",
        classes=("healthy", "patient"),
        url="https://archive.ics.uci.edu/static/public/451/data.csv",
        header=True,
        fields=_BREAST_CANCER_COIMBRA_FIELDS,
        labels={"1": 0, "2": 1},
    ),
    "breast_cancer_original": Table(
        name="breast_cancer_original",
        label="Breast Cancer Wisconsin (Original)",
        title="699 breast tissue samples scored nine ways, benign or malignant",
        licence="CC BY 4.0",
        source="https://archive.ics.uci.edu/dataset/15/breast+cancer+wisconsin+original",
        classes=("benign", "malignant"),
        url="https://archive.ics.uci.edu/static/public/15/data.csv",
        header=True,
        fields=_BREAST_CANCER_ORIGINAL_FIELDS,
        labels={"2": 0, "4": 1},
    ),
    "breast_cancer_prognostic": Table(
        name="breast_cancer_prognostic",
        label="Breast Cancer Wisconsin (Prognostic)",
        title="198 breast cancer operations and whether the disease came back",
        licence="CC BY 4.0",
        source="https://archive.ics.uci.edu/dataset/16/breast+cancer+wisconsin+prognostic",
        classes=("no_recurrence", "recurrence"),
        url="https://archive.ics.uci.edu/static/public/16/data.csv",
        header=True,
        fields=_BREAST_CANCER_PROGNOSTIC_FIELDS,
        labels={"N": 0, "R": 1},
    ),
    "breast_cancer_recurrence": Table(
        name="breast_cancer_recurrence",
        label="Breast Cancer",
        title="286 breast cancer cases from Ljubljana and which of them came back",
        licence="CC BY 4.0",
        source="https://archive.ics.uci.edu/dataset/14/breast+cancer",
        classes=("no_recurrence_events", "recurrence_events"),
        url="https://archive.ics.uci.edu/static/public/14/data.csv",
        header=True,
        fields=_BREAST_CANCER_RECURRENCE_FIELDS,
        labels={"no-recurrence-events": 0, "recurrence-events": 1},
        codes={
            "age": ("20-29", "30-39", "40-49", "50-59", "60-69", "70-79"),
            "menopause": ("ge40", "lt40", "premeno"),
            "tumor_size": (
                "0-4",
                "14-Oct",
                "15-19",
                "20-24",
                "25-29",
                "30-34",
                "35-39",
                "40-44",
                "45-49",
                "50-54",
                "9-May",
            ),
            "inv_nodes": ("0-2", "11-Sep", "14-Dec", "15-17", "24-26", "5-Mar", "8-Jun"),
            "node_caps": ("no", "yes"),
            "breast": ("left", "right"),
            "breast_quad": ("central", "left_low", "left_up", "right_low", "right_up"),
        },
    ),
    "cardiotocography": Table(
        name="cardiotocography",
        label="Cardiotocography",
        title="2,126 foetal heart traces read by three obstetricians, 3 classes",
        licence="CC BY 4.0",
        source="https://archive.ics.uci.edu/dataset/193/cardiotocography",
        classes=("normal", "suspect", "pathologic"),
        url="https://archive.ics.uci.edu/static/public/193/data.csv",
        header=True,
        fields=_CARDIOTOCOGRAPHY_FIELDS,
        labels={"1": 0, "2": 1, "3": 2},
    ),
    "cdc_diabetes": Table(
        name="cdc_diabetes",
        label="CDC Diabetes Health Indicators",
        title="253,680 answers to a CDC health survey, and who among them had diabetes",
        licence="CC BY 4.0",
        source="https://archive.ics.uci.edu/dataset/891/cdc+diabetes+health+indicators",
        classes=("no_diabetes", "diabetes"),
        url="https://archive.ics.uci.edu/static/public/891/data.csv",
        header=True,
        fields=_CDC_DIABETES_FIELDS,
        labels={"0": 0, "1": 1},
    ),
    "census_income_kdd": Table(
        name="census_income_kdd",
        label="Census-Income (KDD)",
        title="199,523 census records from the 1990s and who earned over $50,000",
        licence="CC BY 4.0",
        source="https://archive.ics.uci.edu/dataset/117/census+income+kdd",
        classes=("under_50k", "over_50k"),
        url="https://archive.ics.uci.edu/static/public/117/data.csv",
        header=True,
        text_size=47,
        fields=_CENSUS_INCOME_KDD_FIELDS,
        labels={"-50000": 0, "50000+.": 1},
        codes={
            "aclswkr": (
                "Federal government",
                "Local government",
                "Never worked",
                "Not in universe",
                "Private",
                "Self-employed-incorporated",
                "Self-employed-not incorporated",
                "State government",
                "Without pay",
            ),
            "ahscol": ("College or university", "High school", "Not in universe"),
            "amaritl": (
                "Divorced",
                "Married-A F spouse present",
                "Married-civilian spouse present",
                "Married-spouse absent",
                "Never married",
                "Separated",
                "Widowed",
            ),
            "arace": (
                "Amer Indian Aleut or Eskimo",
                "Asian or Pacific Islander",
                "Black",
                "Other",
                "White",
            ),
            "areorgn": (
                "All other",
                "Central or South American",
                "Chicano",
                "Cuban",
                "Do not know",
                "Mexican (Mexicano)",
                "Mexican-American",
                "Other Spanish",
                "Puerto Rican",
            ),
            "asex": ("Female", "Male"),
            "aunmem": ("No", "Not in universe", "Yes"),
            "auntype": (
                "Job leaver",
                "Job loser - on layoff",
                "New entrant",
                "Not in universe",
                "Other job loser",
                "Re-entrant",
            ),
            "filestat": (
                "Head of household",
                "Joint both 65+",
                "Joint both under 65",
                "Joint one under 65 & one 65+",
                "Nonfiler",
                "Single",
            ),
            "grinreg": ("Abroad", "Midwest", "Northeast", "Not in universe", "South", "West"),
            "migmtr1": (
                "Abroad to MSA",
                "Abroad to nonMSA",
                "MSA to MSA",
                "MSA to nonMSA",
                "NonMSA to MSA",
                "NonMSA to nonMSA",
                "Nonmover",
                "Not identifiable",
                "Not in universe",
            ),
            "migmtr3": (
                "Abroad",
                "Different county same state",
                "Different division same region",
                "Different region",
                "Different state same division",
                "Nonmover",
                "Not in universe",
                "Same county",
            ),
            "migmtr4": (
                "Abroad",
                "Different county same state",
                "Different state in Midwest",
                "Different state in Northeast",
                "Different state in South",
                "Different state in West",
                "Nonmover",
                "Not in universe",
                "Same county",
            ),
            "migsame": ("No", "Not in universe under 1 year old", "Yes"),
            "parent": (
                "Both parents present",
                "Father only present",
                "Mother only present",
                "Neither parent present",
                "Not in universe",
            ),
        },
    ),
    "cervical_cancer_behaviour": Table(
        name="cervical_cancer_behaviour",
        label="Cervical Cancer Behavior Risk",
        title="72 women answering a behaviour questionnaire, with and without cervical cancer",
        licence="CC BY 4.0",
        source="https://archive.ics.uci.edu/dataset/537/cervical+cancer+behavior+risk",
        classes=("no_cancer", "cancer"),
        url="https://archive.ics.uci.edu/static/public/537/data.csv",
        header=True,
        fields=_CERVICAL_CANCER_BEHAVIOUR_FIELDS,
        labels={"0": 0, "1": 1},
    ),
    "cervical_cancer_risk": Table(
        name="cervical_cancer_risk",
        label="Cervical Cancer (Risk Factors)",
        title="858 cervical cancer screenings and what the biopsy found",
        licence="CC BY 4.0",
        source="https://archive.ics.uci.edu/dataset/383/cervical+cancer+risk+factors",
        classes=("negative", "positive"),
        url="https://archive.ics.uci.edu/static/public/383/data.csv",
        header=True,
        fields=_CERVICAL_CANCER_RISK_FIELDS,
        labels={"0": 0, "1": 1},
    ),
    "challenger_o_rings": Table(
        name="challenger_o_rings",
        label="Challenger USA Space Shuttle O-Ring",
        title="23 shuttle launches, the temperature at each, and the O-rings that failed",
        licence="CC BY 4.0",
        source="https://archive.ics.uci.edu/dataset/92/challenger+usa+space+shuttle+o+ring",
        classes=(),
        url="https://archive.ics.uci.edu/static/public/92/data.csv",
        header=True,
        fields=_CHALLENGER_O_RINGS_FIELDS,
    ),
    "chess_endgame": Table(
        name="chess_endgame",
        label="Chess (King-Rook vs. King)",
        title="28,056 king-and-rook against king-and-king positions, by the moves white needs",
        licence="CC BY 4.0",
        source="https://archive.ics.uci.edu/dataset/23/chess+king+rook+vs+king",
        classes=(
            "draw",
            "eight",
            "eleven",
            "fifteen",
            "five",
            "four",
            "fourteen",
            "nine",
            "one",
            "seven",
            "six",
            "sixteen",
            "ten",
            "thirteen",
            "three",
            "twelve",
            "two",
            "zero",
        ),
        url="https://archive.ics.uci.edu/static/public/23/data.csv",
        header=True,
        fields=_CHESS_ENDGAME_FIELDS,
        labels={
            "draw": 0,
            "eight": 1,
            "eleven": 2,
            "fifteen": 3,
            "five": 4,
            "four": 5,
            "fourteen": 6,
            "nine": 7,
            "one": 8,
            "seven": 9,
            "six": 10,
            "sixteen": 11,
            "ten": 12,
            "thirteen": 13,
            "three": 14,
            "twelve": 15,
            "two": 16,
            "zero": 17,
        },
        codes={
            "white_king_file": ("a", "b", "c", "d"),
            "white_rook_file": ("a", "b", "c", "d", "e", "f", "g", "h"),
        },
    ),
    "chronic_kidney_disease": Table(
        name="chronic_kidney_disease",
        label="Chronic Kidney Disease",
        title="400 patients tested for chronic kidney disease, 2 classes",
        licence="CC BY 4.0",
        source="https://archive.ics.uci.edu/dataset/336/chronic+kidney+disease",
        classes=("ckd", "notckd"),
        url="https://archive.ics.uci.edu/static/public/336/data.csv",
        header=True,
        fields=_CHRONIC_KIDNEY_DISEASE_FIELDS,
        labels={"ckd": 0, "notckd": 1},
        codes={
            "rbc": ("abnormal", "normal"),
            "pcc": ("notpresent", "present"),
            "htn": ("no", "yes"),
            "appet": ("good", "poor"),
        },
    ),
    "communities_crime": Table(
        name="communities_crime",
        label="Communities and Crime",
        title="1,994 American communities described by the census, and the violent crime in each",
        licence="CC BY 4.0",
        source="https://archive.ics.uci.edu/dataset/183/communities+and+crime",
        classes=(),
        url="https://archive.ics.uci.edu/static/public/183/data.csv",
        header=True,
        text_size=28,
        fields=_COMMUNITIES_CRIME_FIELDS,
    ),
    "concrete_strength": Table(
        name="concrete_strength",
        label="Concrete Compressive Strength",
        title="1,030 concrete mixes, how long each cured, and the strength it reached",
        licence="CC BY 4.0",
        source="https://archive.ics.uci.edu/dataset/165/concrete+compressive+strength",
        classes=(),
        url="https://archive.ics.uci.edu/static/public/165/data.csv",
        header=True,
        fields=_CONCRETE_STRENGTH_FIELDS,
    ),
    "congressional_voting": Table(
        name="congressional_voting",
        label="Congressional Voting Records",
        title="435 congressmen and the sixteen votes that give away the party",
        licence="CC BY 4.0",
        source="https://archive.ics.uci.edu/dataset/105/congressional+voting+records",
        classes=("democrat", "republican"),
        url="https://archive.ics.uci.edu/static/public/105/data.csv",
        header=True,
        fields=_CONGRESSIONAL_VOTING_FIELDS,
        labels={"democrat": 0, "republican": 1},
        codes={
            "handicapped_infants": ("n", "y"),
        },
    ),
    "connect_four": Table(
        name="connect_four",
        label="Connect-4",
        title="67,557 Connect Four positions eight moves in, won, lost or drawn",
        licence="CC BY 4.0",
        source="https://archive.ics.uci.edu/dataset/26/connect+4",
        classes=("draw", "loss", "win"),
        url="https://archive.ics.uci.edu/static/public/26/data.csv",
        header=True,
        fields=_CONNECT_FOUR_FIELDS,
        labels={"draw": 0, "loss": 1, "win": 2},
        codes={
            "a1": ("b", "o", "x"),
        },
    ),
    "credit_card_default": Table(
        name="credit_card_default",
        label="Default of Credit Card Clients",
        title="30,000 Taiwanese credit cards and which of them defaulted the next month",
        licence="CC BY 4.0",
        source="https://archive.ics.uci.edu/dataset/350/default+of+credit+card+clients",
        classes=("paid", "defaulted"),
        url="https://archive.ics.uci.edu/static/public/350/data.csv",
        header=True,
        fields=_CREDIT_CARD_DEFAULT_FIELDS,
        labels={"0": 0, "1": 1},
    ),
    "credit_screening": Table(
        name="credit_screening",
        label="Japanese Credit Screening",
        title="690 credit card applications with every field anonymised, approved or not",
        licence="CC BY 4.0",
        source="https://archive.ics.uci.edu/dataset/28/japanese+credit+screening",
        classes=("approved", "declined"),
        url="https://archive.ics.uci.edu/static/public/28/data.csv",
        header=True,
        fields=_CREDIT_SCREENING_FIELDS,
        labels={"+": 0, "-": 1},
        codes={
            "a1": ("a", "b"),
            "a4": ("l", "u", "y"),
            "a5": ("g", "gg", "p"),
            "a6": ("aa", "c", "cc", "d", "e", "ff", "i", "j", "k", "m", "q", "r", "w", "x"),
            "a7": ("bb", "dd", "ff", "h", "j", "n", "o", "v", "z"),
            "a9": ("f", "t"),
            "a13": ("g", "p", "s"),
        },
    ),
    "daily_demand": Table(
        name="daily_demand",
        label="Daily Demand Forecasting Orders",
        title="60 days at a Brazilian logistics firm and the orders each brought in",
        licence="CC BY 4.0",
        source="https://archive.ics.uci.edu/dataset/409/daily+demand+forecasting+orders",
        classes=(),
        url="https://archive.ics.uci.edu/static/public/409/data.csv",
        header=True,
        fields=_DAILY_DEMAND_FIELDS,
    ),
    "darwin": Table(
        name="darwin",
        label="DARWIN",
        title="174 people writing on a graphics tablet, with and without Alzheimer's",
        licence="CC BY 4.0",
        source="https://archive.ics.uci.edu/dataset/732/darwin",
        classes=("healthy", "patient"),
        url="https://archive.ics.uci.edu/static/public/732/data.csv",
        header=True,
        text_size=6,
        fields=_DARWIN_FIELDS,
        labels={"H": 0, "P": 1},
    ),
    "diabetes_hospitals": Table(
        name="diabetes_hospitals",
        label="Diabetes 130-US Hospitals for Years 1999-2008",
        title="101,766 hospital stays for diabetes, and who came back",
        licence="CC BY 4.0",
        source="https://archive.ics.uci.edu/dataset/296/diabetes+130-us+hospitals+for+years+1999-2008",
        classes=("readmitted_within_30_days", "readmitted_later", "not_readmitted"),
        url="https://archive.ics.uci.edu/static/public/296/data.csv",
        header=True,
        text_size=36,
        fields=_DIABETES_HOSPITALS_FIELDS,
        labels={"<30": 0, ">30": 1, "NO": 2},
        codes={
            "race": ("AfricanAmerican", "Asian", "Caucasian", "Hispanic", "Other"),
            "gender": ("Female", "Male", "Unknown/Invalid"),
            "age": (
                "[0-10)",
                "[10-20)",
                "[20-30)",
                "[30-40)",
                "[40-50)",
                "[50-60)",
                "[60-70)",
                "[70-80)",
                "[80-90)",
                "[90-100)",
            ),
            "weight": (
                ">200",
                "[0-25)",
                "[100-125)",
                "[125-150)",
                "[150-175)",
                "[175-200)",
                "[25-50)",
                "[50-75)",
                "[75-100)",
            ),
            "payer_code": (
                "BC",
                "CH",
                "CM",
                "CP",
                "DM",
                "FR",
                "HM",
                "MC",
                "MD",
                "MP",
                "OG",
                "OT",
                "PO",
                "SI",
                "SP",
                "UN",
                "WC",
            ),
            "max_glu_serum": (">200", ">300", "None", "Norm"),
            "a1cresult": (">7", ">8", "None", "Norm"),
            "metformin": ("Down", "No", "Steady", "Up"),
            "acetohexamide": ("No", "Steady"),
            "tolazamide": ("No", "Steady", "Up"),
            "examide": ("No",),
            "change": ("Ch", "No"),
            "diabetesmed": ("No", "Yes"),
        },
    ),
    "diabetic_retinopathy": Table(
        name="diabetic_retinopathy",
        label="Diabetic Retinopathy Debrecen",
        title="1,151 eye images scored by a lesion detector, with and without retinopathy",
        licence="CC BY 4.0",
        source="https://archive.ics.uci.edu/dataset/329/diabetic+retinopathy+debrecen",
        classes=("no_retinopathy", "retinopathy"),
        url="https://archive.ics.uci.edu/static/public/329/data.csv",
        header=True,
        fields=_DIABETIC_RETINOPATHY_FIELDS,
        labels={"0": 0, "1": 1},
    ),
    "dota2_games": Table(
        name="dota2_games",
        label="Dota2 Games Results",
        title="102,944 games of Dota 2, the heroes each side picked, and who won",
        licence="CC BY 4.0",
        source="https://archive.ics.uci.edu/dataset/367/dota2+games+results",
        classes=("radiant_lost", "radiant_won"),
        url="https://archive.ics.uci.edu/static/public/367/data.csv",
        header=True,
        fields=_DOTA2_GAMES_FIELDS,
        labels={"-1": 0, "1": 1},
    ),
    "drug_consumption": Table(
        name="drug_consumption",
        label="Drug Consumption (Quantified)",
        title="1,885 personality profiles, and how recently each person last used cannabis",
        licence="CC BY 4.0",
        source="https://archive.ics.uci.edu/dataset/373/drug+consumption+quantified",
        classes=("cl0", "cl1", "cl2", "cl3", "cl4", "cl5", "cl6"),
        url="https://archive.ics.uci.edu/static/public/373/data.csv",
        header=True,
        fields=_DRUG_CONSUMPTION_FIELDS,
        labels={"CL0": 0, "CL1": 1, "CL2": 2, "CL3": 3, "CL4": 4, "CL5": 5, "CL6": 6},
        codes={
            "alcohol": ("CL0", "CL1", "CL2", "CL3", "CL4", "CL5", "CL6"),
            "semer": ("CL0", "CL1", "CL2", "CL3", "CL4"),
        },
    ),
    "eeg_eye_state": Table(
        name="eeg_eye_state",
        label="EEG Eye State",
        title="14,980 moments of EEG from fourteen electrodes, eyes open or shut",
        licence="CC BY 4.0",
        source="https://archive.ics.uci.edu/dataset/264/eeg+eye+state",
        classes=("eye_open", "eye_closed"),
        url="https://archive.ics.uci.edu/static/public/264/data.csv",
        header=True,
        fields=_EEG_EYE_STATE_FIELDS,
        labels={"0": 0, "1": 1},
    ),
    "el_nino": Table(
        name="el_nino",
        label="El Nino",
        title="178,080 readings from the Pacific buoy array and the air temperature at each",
        licence="CC BY 4.0",
        source="https://archive.ics.uci.edu/dataset/122/el+nino",
        classes=(),
        url="https://archive.ics.uci.edu/static/public/122/data.csv",
        header=True,
        fields=_EL_NINO_FIELDS,
    ),
    "entrance_exam": Table(
        name="entrance_exam",
        label="Student Performance on an Entrance Examination",
        title="666 students sitting an engineering entrance exam, and how well each did",
        licence="CC BY 4.0",
        source="https://archive.ics.uci.edu/dataset/582/student+performance+on+an+entrance+examination",
        classes=("excellent", "very_good", "good", "average"),
        url="https://archive.ics.uci.edu/static/public/582/data.csv",
        header=True,
        fields=_ENTRANCE_EXAM_FIELDS,
        labels={"Excellent": 0, "Vg": 1, "Good": 2, "Average": 3},
        codes={
            "gender": ("female", "male"),
            "caste": ("General", "OBC", "SC", "ST"),
            "coaching": ("NO", "OA", "WA"),
            "time_code": ("FIVE", "FOUR", "ONE", "SEVEN", "THREE", "TWO"),
            "class_ten_education": ("CBSE", "OTHERS", "SEBA"),
            "twelve_education": ("AHSEC", "CBSE", "OTHERS"),
            "medium": ("ASSAMESE", "ENGLISH", "OTHERS"),
            "class_x_percentage": ("Average", "Excellent", "Good", "Vg"),
            "father_occupation": (
                "BANK_OFFICIAL",
                "BUSINESS",
                "COLLEGE_TEACHER",
                "CULTIVATOR",
                "DOCTOR",
                "ENGINEER",
                "OTHERS",
                "SCHOOL_TEACHER",
            ),
            "mother_occupation": (
                "BANK_OFFICIAL",
                "BUSINESS",
                "COLLEGE_TEACHER",
                "CULTIVATOR",
                "DOCTOR",
                "ENGINEER",
                "HOUSE_WIFE",
                "OTHERS",
                "SCHOOL_TEACHER",
            ),
        },
    ),
    "facebook_live_sellers": Table(
        name="facebook_live_sellers",
        label="Facebook Live Sellers in Thailand",
        title="7,050 posts by Thai fashion sellers, by what kind of post each one was",
        licence="CC BY 4.0",
        source="https://archive.ics.uci.edu/dataset/488/facebook+live+sellers+in+thailand",
        classes=("link", "photo", "status", "video"),
        url="https://archive.ics.uci.edu/static/public/488/data.csv",
        header=True,
        dates="%m/%d/%Y %H:%M",
        fields=_FACEBOOK_LIVE_SELLERS_FIELDS,
        labels={"link": 0, "photo": 1, "status": 2, "video": 3},
    ),
    "facebook_metrics": Table(
        name="facebook_metrics",
        label="Facebook Metrics",
        title="500 posts by a cosmetics brand and the reaction each of them drew",
        licence="CC BY 4.0",
        source="https://archive.ics.uci.edu/dataset/368/facebook+metrics",
        classes=(),
        url="https://archive.ics.uci.edu/static/public/368/data.csv",
        header=True,
        fields=_FACEBOOK_METRICS_FIELDS,
        codes={
            "type": ("Link", "Photo", "Status", "Video"),
        },
    ),
    "flags": Table(
        name="flags",
        label="Flags",
        title="194 national flags, their colours and shapes, and the country's religion",
        licence="CC BY 4.0",
        source="https://archive.ics.uci.edu/dataset/40/flags",
        classes=(
            "catholic",
            "other_christian",
            "muslim",
            "buddhist",
            "hindu",
            "ethnic",
            "marxist",
            "other_religion",
        ),
        url="https://archive.ics.uci.edu/static/public/40/data.csv",
        header=True,
        text_size=24,
        fields=_FLAGS_FIELDS,
        labels={"0": 0, "1": 1, "2": 2, "3": 3, "4": 4, "5": 5, "6": 6, "7": 7},
        codes={
            "mainhue": ("black", "blue", "brown", "gold", "green", "orange", "red", "white"),
            "topleft": ("black", "blue", "gold", "green", "orange", "red", "white"),
        },
    ),
    "gas_turbine_emissions": Table(
        name="gas_turbine_emissions",
        label="Gas Turbine CO and NOx Emission Data Set",
        title="36,733 hours of a gas turbine and the carbon monoxide and nitrogen oxides it made",
        licence="CC BY 4.0",
        source="https://archive.ics.uci.edu/dataset/551/gas+turbine+co+and+nox+emission+data+set",
        classes=(),
        url="https://archive.ics.uci.edu/static/public/551/data.csv",
        header=True,
        fields=_GAS_TURBINE_EMISSIONS_FIELDS,
    ),
    "gender_by_name": Table(
        name="gender_by_name",
        label="Gender by Name",
        title="147,269 first names and the sex of the babies given them",
        licence="CC BY 4.0",
        source="https://archive.ics.uci.edu/dataset/591/gender+by+name",
        classes=("female", "male"),
        url="https://archive.ics.uci.edu/static/public/591/data.csv",
        header=True,
        text_size=25,
        fields=_GENDER_BY_NAME_FIELDS,
        labels={"F": 0, "M": 1},
    ),
    "glioma_grading": Table(
        name="glioma_grading",
        label="Glioma Grading Clinical and Mutation Features",
        title="839 glioma patients, the genes mutated in each, and the grade of the tumour",
        licence="CC BY 4.0",
        source="https://archive.ics.uci.edu/dataset/759/glioma+grading+clinical+and+mutation+features+dataset",
        classes=("lower_grade_glioma", "glioblastoma"),
        url="https://archive.ics.uci.edu/static/public/759/data.csv",
        header=True,
        text_size=12,
        fields=_GLIOMA_GRADING_FIELDS,
        labels={"0": 0, "1": 1},
        codes={
            "race": (
                "american indian or alaska native",
                "asian",
                "black or african american",
                "white",
            ),
        },
    ),
    "grid_stability": Table(
        name="grid_stability",
        label="Electrical Grid Stability Simulated Data",
        title="10,000 simulated four-node power grids, stable or not",
        licence="CC BY 4.0",
        source="https://archive.ics.uci.edu/dataset/471/electrical+grid+stability+simulated+data",
        classes=("stable", "unstable"),
        url="https://archive.ics.uci.edu/static/public/471/data.csv",
        header=True,
        fields=_GRID_STABILITY_FIELDS,
        labels={"stable": 0, "unstable": 1},
    ),
    "hcv_blood_donors": Table(
        name="hcv_blood_donors",
        label="HCV data",
        title="615 blood samples from donors and hepatitis C patients, 5 classes",
        licence="CC BY 4.0",
        source="https://archive.ics.uci.edu/dataset/571/hcv+data",
        classes=("blood_donor", "suspect_blood_donor", "hepatitis", "fibrosis", "cirrhosis"),
        url="https://archive.ics.uci.edu/static/public/571/data.csv",
        header=True,
        fields=_HCV_BLOOD_DONORS_FIELDS,
        labels={
            "0=Blood Donor": 0,
            "0s=suspect Blood Donor": 1,
            "1=Hepatitis": 2,
            "2=Fibrosis": 3,
            "3=Cirrhosis": 4,
        },
        codes={
            "sex": ("f", "m"),
        },
    ),
    "healthy_aging_poll": Table(
        name="healthy_aging_poll",
        label="National Poll on Healthy Aging (NPHA)",
        title="714 older Americans polled on their health, by how many doctors each sees",
        licence="CC BY 4.0",
        source="https://archive.ics.uci.edu/dataset/936/national+poll+on+healthy+aging+(npha)",
        classes=("doctors_0_1", "doctors_2_3", "doctors_4_or_more"),
        url="https://archive.ics.uci.edu/static/public/936/data.csv",
        header=True,
        fields=_HEALTHY_AGING_POLL_FIELDS,
        labels={"1": 0, "2": 1, "3": 2},
    ),
    "hepatitis_c_egypt": Table(
        name="hepatitis_c_egypt",
        label="Hepatitis C Virus (HCV) for Egyptian patients",
        title="1,385 Egyptian hepatitis C patients and how far the fibrosis had gone",
        licence="CC BY 4.0",
        source="https://archive.ics.uci.edu/dataset/503/hepatitis+c+virus+hcv+for+egyptian+patients",
        classes=("portal_fibrosis", "few_septa", "many_septa", "cirrhosis"),
        url="https://archive.ics.uci.edu/static/public/503/data.csv",
        header=True,
        fields=_HEPATITIS_C_EGYPT_FIELDS,
        labels={"1": 0, "2": 1, "3": 2, "4": 3},
    ),
    "higher_education_students": Table(
        name="higher_education_students",
        label="Higher Education Students Performance Evaluation",
        title="145 students answering how they live and study, and the grade each got",
        licence="CC BY 4.0",
        source="https://archive.ics.uci.edu/dataset/856/higher+education+students+performance+evaluation",
        classes=("fail", "dd", "dc", "cc", "cb", "bb", "ba", "aa"),
        url="https://archive.ics.uci.edu/static/public/856/data.csv",
        header=True,
        text_size=10,
        fields=_HIGHER_EDUCATION_STUDENTS_FIELDS,
        labels={"0": 0, "1": 1, "2": 2, "3": 3, "4": 4, "5": 5, "6": 6, "7": 7},
    ),
    "horse_colic": Table(
        name="horse_colic",
        label="Horse Colic",
        title="368 horses with colic and whether the lesion turned out to need surgery",
        licence="CC BY 4.0",
        source="https://archive.ics.uci.edu/dataset/47/horse+colic",
        classes=("surgical", "not_surgical"),
        url="https://archive.ics.uci.edu/static/public/47/data.csv",
        header=True,
        fields=_HORSE_COLIC_FIELDS,
        labels={"1": 0, "2": 1},
    ),
    "in_vehicle_coupon": Table(
        name="in_vehicle_coupon",
        label="In-Vehicle Coupon Recommendation",
        title="12,684 drivers offered a coupon at the wheel, and who took one",
        licence="CC BY 4.0",
        source="https://archive.ics.uci.edu/dataset/603/in+vehicle+coupon+recommendation",
        classes=("declined", "accepted"),
        url="https://archive.ics.uci.edu/static/public/603/data.csv",
        header=True,
        text_size=41,
        fields=_IN_VEHICLE_COUPON_FIELDS,
        labels={"0": 0, "1": 1},
        codes={
            "destination": ("Home", "No Urgent Place", "Work"),
            "passenger": ("Alone", "Friend(s)", "Kid(s)", "Partner"),
            "weather": ("Rainy", "Snowy", "Sunny"),
            "time_code": ("10AM", "10PM", "2PM", "6PM", "7AM"),
            "coupon": (
                "Bar",
                "Carry out & Take away",
                "Coffee House",
                "Restaurant(20-50)",
                "Restaurant(<20)",
            ),
            "expiration": ("1d", "2h"),
            "gender": ("Female", "Male"),
            "age": ("21", "26", "31", "36", "41", "46", "50plus", "below21"),
            "maritalstatus": (
                "Divorced",
                "Married partner",
                "Single",
                "Unmarried partner",
                "Widowed",
            ),
            "income": (
                "$100000 or More",
                "$12500 - $24999",
                "$25000 - $37499",
                "$37500 - $49999",
                "$50000 - $62499",
                "$62500 - $74999",
                "$75000 - $87499",
                "$87500 - $99999",
                "Less than $12500",
            ),
            "bar": ("1~3", "4~8", "gt8", "less1", "never"),
        },
    ),
    "infrared_thermography": Table(
        name="infrared_thermography",
        label="Infrared Thermography Temperature",
        title="1,020 thermal images of a face and the oral temperature measured after each",
        licence="CC BY 4.0",
        source="https://archive.ics.uci.edu/dataset/925/infrared+thermography+temperature+dataset",
        classes=(),
        url="https://archive.ics.uci.edu/static/public/925/data.csv",
        header=True,
        text_size=33,
        fields=_INFRARED_THERMOGRAPHY_FIELDS,
        codes={
            "gender": ("Female", "Male"),
            "age": ("18-20", "21-25", "21-30", "26-30", "31-40", "41-50", "51-60", ">60"),
        },
    ),
    "iot_intrusion": Table(
        name="iot_intrusion",
        label="RT-IoT2022",
        title="123,117 network flows from an IoT testbed, ordinary traffic and twelve attacks",
        licence="CC BY 4.0",
        source="https://archive.ics.uci.edu/dataset/942/rt-iot2022",
        classes=(
            "arp_poisioning",
            "ddos_slowloris",
            "dos_syn_hping",
            "mqtt_publish",
            "metasploit_brute_force_ssh",
            "nmap_fin_scan",
            "nmap_os_detection",
            "nmap_tcp_scan",
            "nmap_udp_scan",
            "nmap_xmas_tree_scan",
            "thing_speak",
            "wipro_bulb",
        ),
        url="https://archive.ics.uci.edu/static/public/942/data.csv",
        header=True,
        fields=_IOT_INTRUSION_FIELDS,
        labels={
            "ARP_poisioning": 0,
            "DDOS_Slowloris": 1,
            "DOS_SYN_Hping": 2,
            "MQTT_Publish": 3,
            "Metasploit_Brute_Force_SSH": 4,
            "NMAP_FIN_SCAN": 5,
            "NMAP_OS_DETECTION": 6,
            "NMAP_TCP_scan": 7,
            "NMAP_UDP_SCAN": 8,
            "NMAP_XMAS_TREE_SCAN": 9,
            "Thing_Speak": 10,
            "Wipro_bulb": 11,
        },
        codes={
            "proto": ("icmp", "tcp", "udp"),
            "service": ("-", "dhcp", "dns", "http", "irc", "mqtt", "ntp", "radius", "ssh", "ssl"),
        },
    ),
    "iranian_churn": Table(
        name="iranian_churn",
        label="Iranian Churn",
        title="3,150 customers of an Iranian telecom and which of them left",
        licence="CC BY 4.0",
        source="https://archive.ics.uci.edu/dataset/563/iranian+churn+dataset",
        classes=("stayed", "left"),
        url="https://archive.ics.uci.edu/static/public/563/data.csv",
        header=True,
        fields=_IRANIAN_CHURN_FIELDS,
        labels={"0": 0, "1": 1},
    ),
    "isolet": Table(
        name="isolet",
        label="ISOLET",
        title="7,797 spoken letters described 617 ways, 26 classes",
        licence="CC BY 4.0",
        source="https://archive.ics.uci.edu/dataset/54/isolet",
        classes=(
            "a",
            "b",
            "c",
            "d",
            "e",
            "f",
            "g",
            "h",
            "i",
            "j",
            "k",
            "l",
            "m",
            "n",
            "o",
            "p",
            "q",
            "r",
            "s",
            "t",
            "u",
            "v",
            "w",
            "x",
            "y",
            "z",
        ),
        url="https://archive.ics.uci.edu/static/public/54/data.csv",
        header=True,
        fields=_ISOLET_FIELDS,
        labels={
            "1.": 0,
            "2.": 1,
            "3.": 2,
            "4.": 3,
            "5.": 4,
            "6.": 5,
            "7.": 6,
            "8.": 7,
            "9.": 8,
            "10.": 9,
            "11.": 10,
            "12.": 11,
            "13.": 12,
            "14.": 13,
            "15.": 14,
            "16.": 15,
            "17.": 16,
            "18.": 17,
            "19.": 18,
            "20.": 19,
            "21.": 20,
            "22.": 21,
            "23.": 22,
            "24.": 23,
            "25.": 24,
            "26.": 25,
        },
    ),
    "istanbul_exchange": Table(
        name="istanbul_exchange",
        label="ISTANBUL STOCK EXCHANGE",
        title="536 trading days of the Istanbul exchange beside eight other indices",
        licence="CC BY 4.0",
        source="https://archive.ics.uci.edu/dataset/247/istanbul+stock+exchange",
        classes=(),
        url="https://archive.ics.uci.edu/static/public/247/data.csv",
        header=True,
        dates="%d-%b-%y",
        text_size=9,
        fields=_ISTANBUL_EXCHANGE_FIELDS,
    ),
    "kidney_risk_factors": Table(
        name="kidney_risk_factors",
        label="Risk Factor Prediction of Chronic Kidney Disease",
        title="200 villagers in India screened for chronic kidney disease",
        licence="CC BY 4.0",
        source="https://archive.ics.uci.edu/dataset/857/risk+factor+prediction+of+chronic+kidney+disease",
        classes=("ckd", "notckd"),
        url="https://archive.ics.uci.edu/static/public/857/data.csv",
        header=True,
        fields=_KIDNEY_RISK_FACTORS_FIELDS,
        labels={"ckd": 0, "notckd": 1},
        codes={
            "sg": ("1.009 - 1.011", "1.015 - 1.017", "1.019 - 1.021", "< 1.007", "≥ 1.023"),
            "al": ("1-Jan", "2-Feb", "3-Mar", "< 0", "≥ 4"),
            "su": ("2-Feb", "2-Jan", "4-Apr", "4-Mar", "< 0", "≥ 4"),
            "bgr": (
                "112 - 154",
                "154 - 196",
                "196 - 238",
                "238 - 280",
                "280 - 322",
                "322 - 364",
                "364 - 406",
                "406 - 448",
                "< 112",
                "≥ 448",
            ),
            "bu": (
                "124.3 - 162.4",
                "162.4 - 200.5",
                "200.5 - 238.6",
                "238.6 - 276.7",
                "48.1 - 86.2",
                "86.2 - 124.3",
                "< 48.1",
                "≥ 352.9",
            ),
            "sod": (
                "118 - 123",
                "123 - 128",
                "128 - 133",
                "133 - 138",
                "138 - 143",
                "143 - 148",
                "148 - 153",
                "< 118",
                "≥ 158",
            ),
            "sc": (
                "13.1 - 16.25",
                "16.25 - 19.4",
                "3.65 - 6.8",
                "6.8 - 9.95",
                "9.95 - 13.1",
                "< 3.65",
                "≥ 28.85",
            ),
            "pot": ("38.18 - 42.59", "7.31 - 11.72", "< 7.31", "≥ 42.59"),
            "hemo": (
                "10 - 11.3",
                "11.3 - 12.6",
                "12.6 - 13.9",
                "13.9 - 15.2",
                "15.2 - 16.5",
                "6.1 - 7.4",
                "7.4 - 8.7",
                "8.7 - 10",
                "< 6.1",
                "≥ 16.5",
            ),
            "pcv": (
                "17.9 - 21.8",
                "21.8 - 25.7",
                "25.7 - 29.6",
                "29.6 - 33.5",
                "33.5 - 37.4",
                "37.4 - 41.3",
                "41.3 - 45.2",
                "45.2 - 49.1",
                "< 17.9",
                "≥ 49.1",
            ),
            "rbcc": (
                "2.69 - 3.28",
                "3.28 - 3.87",
                "3.87 - 4.46",
                "4.46 - 5.05",
                "5.05 - 5.64",
                "5.64 - 6.23",
                "6.23 - 6.82",
                "< 2.69",
                "≥ 7.41",
            ),
            "wbcc": (
                "12120 - 14500",
                "14500 - 16880",
                "16880 - 19260",
                "19260 - 21640",
                "4980 - 7360",
                "7360 - 9740",
                "9740 - 12120",
                "< 4980",
                "≥ 24020",
            ),
            "grf": (
                "102.115 - 127.281",
                "127.281 - 152.446",
                "152.446 - 177.612",
                "177.612 - 202.778",
                "202.778 - 227.944",
                "26.6175 - 51.7832",
                "51.7832 - 76.949",
                "76.949 - 102.115",
                "< 26.6175",
                "p",
                "≥ 227.944",
            ),
            "stage": ("s1", "s2", "s3", "s4", "s5"),
            "age": (
                "20 - 27",
                "20-Dec",
                "27 - 35",
                "35 - 43",
                "43 - 51",
                "51 - 59",
                "59 - 66",
                "66 - 74",
                "< 12",
                "≥ 74",
            ),
        },
    ),
    "land_mines": Table(
        name="land_mines",
        label="Land Mines",
        title="338 passes of a metal detector and the kind of mine underneath",
        licence="CC BY 4.0",
        source="https://archive.ics.uci.edu/dataset/763/land+mines-1",
        classes=(
            "no_mine",
            "anti_personnel",
            "anti_tank",
            "booby_trapped_anti_personnel",
            "m14_anti_personnel",
        ),
        url="https://archive.ics.uci.edu/static/public/763/data.csv",
        header=True,
        fields=_LAND_MINES_FIELDS,
        labels={"1": 0, "2": 1, "3": 2, "4": 3, "5": 4},
    ),
    "metro_traffic": Table(
        name="metro_traffic",
        label="Metro Interstate Traffic Volume",
        title="48,204 hours on an interstate near Minneapolis and the cars that went by",
        licence="CC BY 4.0",
        source="https://archive.ics.uci.edu/dataset/492/metro+interstate+traffic+volume",
        classes=(),
        url="https://archive.ics.uci.edu/static/public/492/data.csv",
        header=True,
        dates="%Y-%m-%d %H:%M:%S",
        text_size=35,
        fields=_METRO_TRAFFIC_FIELDS,
        codes={
            "holiday": (
                "Christmas Day",
                "Columbus Day",
                "Independence Day",
                "Labor Day",
                "Martin Luther King Jr Day",
                "Memorial Day",
                "New Years Day",
                "None",
                "State Fair",
                "Thanksgiving Day",
                "Veterans Day",
                "Washingtons Birthday",
            ),
            "weather_main": (
                "Clear",
                "Clouds",
                "Drizzle",
                "Fog",
                "Haze",
                "Mist",
                "Rain",
                "Smoke",
                "Snow",
                "Squall",
                "Thunderstorm",
            ),
        },
    ),
    "mice_protein": Table(
        name="mice_protein",
        label="Mice Protein Expression",
        title="1,080 measurements of 77 proteins in the cortex of mice, 8 classes",
        licence="CC BY 4.0",
        source="https://archive.ics.uci.edu/dataset/342/mice+protein+expression",
        classes=("c_cs_m", "c_cs_s", "c_sc_m", "c_sc_s", "t_cs_m", "t_cs_s", "t_sc_m", "t_sc_s"),
        url="https://archive.ics.uci.edu/static/public/342/data.csv",
        header=True,
        text_size=9,
        fields=_MICE_PROTEIN_FIELDS,
        labels={
            "c-CS-m": 0,
            "c-CS-s": 1,
            "c-SC-m": 2,
            "c-SC-s": 3,
            "t-CS-m": 4,
            "t-CS-s": 5,
            "t-SC-m": 6,
            "t-SC-s": 7,
        },
        codes={
            "genotype": ("Control", "Ts65Dn"),
            "treatment": ("Memantine", "Saline"),
            "behavior": ("C/S", "S/C"),
        },
    ),
    "monks_problems": Table(
        name="monks_problems",
        label="MONK's Problems",
        title="432 toy robots described six ways, and whether the rule holds of each",
        licence="CC BY 4.0",
        source="https://archive.ics.uci.edu/dataset/70/monk+s+problems",
        classes=("false", "true"),
        url="https://archive.ics.uci.edu/static/public/70/data.csv",
        header=True,
        text_size=8,
        fields=_MONKS_PROBLEMS_FIELDS,
        labels={"0": 0, "1": 1},
    ),
    "multivariate_gait": Table(
        name="multivariate_gait",
        label="Multivariate Gait Data",
        title="181,800 joint angles measured as ten people walked at three speeds",
        licence="CC BY 4.0",
        source="https://archive.ics.uci.edu/dataset/760/multivariate+gait+data",
        classes=(),
        url="https://archive.ics.uci.edu/static/public/760/data.csv",
        header=True,
        fields=_MULTIVARIATE_GAIT_FIELDS,
    ),
    "musk_version1": Table(
        name="musk_version1",
        label="Musk (Version 1)",
        title="476 molecular conformations described 166 ways, musk or not",
        licence="CC BY 4.0",
        source="https://archive.ics.uci.edu/dataset/74/musk+version+1",
        classes=("not_musk", "musk"),
        url="https://archive.ics.uci.edu/static/public/74/data.csv",
        header=True,
        text_size=13,
        fields=_MUSK_VERSION1_FIELDS,
        labels={"0.": 0, "1.": 1},
    ),
    "musk_version2": Table(
        name="musk_version2",
        label="Musk (Version 2)",
        title="6,598 conformations of 102 molecules, musk or not",
        licence="CC BY 4.0",
        source="https://archive.ics.uci.edu/dataset/75/musk+version+2",
        classes=("not_musk", "musk"),
        url="https://archive.ics.uci.edu/static/public/75/data.csv",
        header=True,
        text_size=13,
        fields=_MUSK_VERSION2_FIELDS,
        labels={"0.": 0, "1.": 1},
    ),
    "news_popularity": Table(
        name="news_popularity",
        label="Online News Popularity",
        title="39,644 Mashable articles described 58 ways, and the times each was shared",
        licence="CC BY 4.0",
        source="https://archive.ics.uci.edu/dataset/332/online+news+popularity",
        classes=(),
        url="https://archive.ics.uci.edu/static/public/332/data.csv",
        header=True,
        text_size=192,
        fields=_NEWS_POPULARITY_FIELDS,
    ),
    "nhanes_age": Table(
        name="nhanes_age",
        label="NHANES 2013-2014 Age Prediction Subset",
        title="2,278 people in an American health survey, adult or senior",
        licence="CC BY 4.0",
        source="https://archive.ics.uci.edu/dataset/887/national+health+and+nutrition+health+survey+2013-2014+(nhanes)+age+prediction+subset",
        classes=("adult", "senior"),
        url="https://archive.ics.uci.edu/static/public/887/data.csv",
        header=True,
        fields=_NHANES_AGE_FIELDS,
        labels={"Adult": 0, "Senior": 1},
    ),
    "obesity_levels": Table(
        name="obesity_levels",
        label="Estimation of Obesity Levels Based On Eating Habits and Physical Condition",
        title="2,111 people's eating and moving habits, and the weight class each fell in",
        licence="CC BY 4.0",
        source="https://archive.ics.uci.edu/dataset/544/estimation+of+obesity+levels+based+on+eating+habits+and+physical+condition",
        classes=(
            "insufficient_weight",
            "normal_weight",
            "obesity_type_i",
            "obesity_type_ii",
            "obesity_type_iii",
            "overweight_level_i",
            "overweight_level_ii",
        ),
        url="https://archive.ics.uci.edu/static/public/544/data.csv",
        header=True,
        fields=_OBESITY_LEVELS_FIELDS,
        labels={
            "Insufficient_Weight": 0,
            "Normal_Weight": 1,
            "Obesity_Type_I": 2,
            "Obesity_Type_II": 3,
            "Obesity_Type_III": 4,
            "Overweight_Level_I": 5,
            "Overweight_Level_II": 6,
        },
        codes={
            "gender": ("Female", "Male"),
            "family_history_with_overweight": ("no", "yes"),
            "caec": ("Always", "Frequently", "Sometimes", "no"),
            "mtrans": ("Automobile", "Bike", "Motorbike", "Public_Transportation", "Walking"),
        },
    ),
    "ozone_level": Table(
        name="ozone_level",
        label="Ozone Level Detection",
        title="5,070 days of Houston weather, and which of them broke the ozone limit",
        licence="CC BY 4.0",
        source="https://archive.ics.uci.edu/dataset/172/ozone+level+detection",
        classes=("normal_day", "ozone_day"),
        url="https://archive.ics.uci.edu/static/public/172/data.csv",
        header=True,
        dates="%m/%d/%Y",
        fields=_OZONE_LEVEL_FIELDS,
        labels={"0": 0, "1": 1},
        codes={
            "dataset": ("1hr", "8hr"),
        },
    ),
    "page_blocks": Table(
        name="page_blocks",
        label="Page Blocks Classification",
        title="5,473 blocks of a scanned document page, 5 classes",
        licence="CC BY 4.0",
        source="https://archive.ics.uci.edu/dataset/78/page+blocks+classification",
        classes=("text", "horizontal_line", "picture", "vertical_line", "graphic"),
        url="https://archive.ics.uci.edu/static/public/78/data.csv",
        header=True,
        fields=_PAGE_BLOCKS_FIELDS,
        labels={"1": 0, "2": 1, "3": 2, "4": 3, "5": 4},
    ),
    "pittsburgh_bridges": Table(
        name="pittsburgh_bridges",
        label="Pittsburgh Bridges",
        title="108 bridges over the three rivers of Pittsburgh, by the era each was built in",
        licence="CC BY 4.0",
        source="https://archive.ics.uci.edu/dataset/18/pittsburgh+bridges",
        classes=("crafts", "emerging", "mature", "modern"),
        url="https://archive.ics.uci.edu/static/public/18/data.csv",
        header=True,
        text_size=5,
        fields=_PITTSBURGH_BRIDGES_FIELDS,
        labels={"CRAFTS": 0, "EMERGING": 1, "MATURE": 2, "MODERN": 3},
        codes={
            "river": ("A", "M", "O", "Y"),
            "purpose": ("AQUEDUCT", "HIGHWAY", "RR", "WALK"),
            "length": ("LONG", "MEDIUM", "SHORT"),
            "clear_g": ("G", "N"),
            "t_or_d": ("DECK", "THROUGH"),
            "material": ("IRON", "STEEL", "WOOD"),
            "rel_l": ("F", "S", "S-F"),
            "type": ("ARCH", "CANTILEV", "CONT-T", "NIL", "SIMPLE-T", "SUSPEN", "WOOD"),
        },
    ),
    "poker_hand": Table(
        name="poker_hand",
        label="Poker Hand",
        title="1,025,010 poker hands of five cards, by what each is worth",
        licence="CC BY 4.0",
        source="https://archive.ics.uci.edu/dataset/158/poker+hand",
        classes=(
            "nothing",
            "one_pair",
            "two_pairs",
            "three_of_a_kind",
            "straight",
            "flush",
            "full_house",
            "four_of_a_kind",
            "straight_flush",
            "royal_flush",
        ),
        url="https://archive.ics.uci.edu/static/public/158/data.csv",
        header=True,
        fields=_POKER_HAND_FIELDS,
        labels={"0": 0, "1": 1, "2": 2, "3": 3, "4": 4, "5": 5, "6": 6, "7": 7, "8": 8, "9": 9},
    ),
    "polish_bankruptcy": Table(
        name="polish_bankruptcy",
        label="Polish Companies Bankruptcy",
        title="43,405 years of Polish company accounts and which firms went bankrupt",
        licence="CC BY 4.0",
        source="https://archive.ics.uci.edu/dataset/365/polish+companies+bankruptcy+data",
        classes=("solvent", "bankrupt"),
        url="https://archive.ics.uci.edu/static/public/365/data.csv",
        header=True,
        fields=_POLISH_BANKRUPTCY_FIELDS,
        labels={"0": 0, "1": 1},
    ),
    "post_operative_patient": Table(
        name="post_operative_patient",
        label="Post-Operative Patient",
        title="90 patients leaving the recovery room, and where each was sent next",
        licence="CC BY 4.0",
        source="https://archive.ics.uci.edu/dataset/82/post+operative+patient",
        classes=("general_floor", "intensive_care", "sent_home"),
        url="https://archive.ics.uci.edu/static/public/82/data.csv",
        header=True,
        fields=_POST_OPERATIVE_PATIENT_FIELDS,
        labels={"A": 0, "I": 1, "S": 2},
        codes={
            "l_core": ("high", "low", "mid"),
            "l_o2": ("excellent", "good"),
            "surf_stbl": ("stable", "unstable"),
            "core_stbl": ("mod-stable", "stable", "unstable"),
        },
    ),
    "predictive_maintenance": Table(
        name="predictive_maintenance",
        label="AI4I 2020 Predictive Maintenance Dataset",
        title="10,000 simulated hours of a milling machine, and the tools that broke",
        licence="CC BY 4.0",
        source="https://archive.ics.uci.edu/dataset/601/ai4i+2020+predictive+maintenance+dataset",
        classes=("kept_going", "failed"),
        url="https://archive.ics.uci.edu/static/public/601/data.csv",
        header=True,
        text_size=6,
        fields=_PREDICTIVE_MAINTENANCE_FIELDS,
        labels={"0": 0, "1": 1},
        codes={
            "type": ("H", "L", "M"),
        },
    ),
    "room_occupancy_count": Table(
        name="room_occupancy_count",
        label="Room Occupancy Estimation",
        title="10,129 minutes of light, sound, temperature and CO2, and the people in the room",
        licence="CC BY 4.0",
        source="https://archive.ics.uci.edu/dataset/864/room+occupancy+estimation",
        classes=(),
        url="https://archive.ics.uci.edu/static/public/864/data.csv",
        header=True,
        dates="%H:%M:%S",
        fields=_ROOM_OCCUPANCY_COUNT_FIELDS,
        codes={
            "date_code": (
                "2017/12/22",
                "2017/12/23",
                "2017/12/24",
                "2017/12/25",
                "2017/12/26",
                "2018/01/10",
                "2018/01/11",
            ),
        },
    ),
    "secondary_mushroom": Table(
        name="secondary_mushroom",
        label="Secondary Mushroom",
        title="61,069 mushrooms grown from a field guide's own descriptions, edible or poisonous",
        licence="CC BY 4.0",
        source="https://archive.ics.uci.edu/dataset/848/secondary+mushroom+dataset",
        classes=("edible", "poisonous"),
        url="https://archive.ics.uci.edu/static/public/848/data.csv",
        header=True,
        fields=_SECONDARY_MUSHROOM_FIELDS,
        labels={"e": 0, "p": 1},
        codes={
            "cap_shape": ("b", "c", "f", "o", "p", "s", "x"),
            "cap_surface": ("d", "e", "g", "h", "i", "k", "l", "s", "t", "w", "y"),
            "cap_color": ("b", "e", "g", "k", "l", "n", "o", "p", "r", "u", "w", "y"),
            "does_bruise_or_bleed": ("f", "t"),
            "gill_attachment": ("a", "d", "e", "f", "p", "s", "x"),
            "gill_spacing": ("c", "d", "f"),
            "gill_color": ("b", "e", "f", "g", "k", "n", "o", "p", "r", "u", "w", "y"),
            "stem_root": ("b", "c", "f", "r", "s"),
            "stem_surface": ("f", "g", "h", "i", "k", "s", "t", "y"),
            "stem_color": ("b", "e", "f", "g", "k", "l", "n", "o", "p", "r", "u", "w", "y"),
            "veil_type": ("u",),
            "veil_color": ("e", "k", "n", "u", "w", "y"),
            "ring_type": ("e", "f", "g", "l", "m", "p", "r", "z"),
            "spore_print_color": ("g", "k", "n", "p", "r", "u", "w"),
            "habitat": ("d", "g", "h", "l", "m", "p", "u", "w"),
            "season": ("a", "s", "u", "w"),
        },
    ),
    "seoul_bike_sharing": Table(
        name="seoul_bike_sharing",
        label="Seoul Bike Sharing Demand",
        title="8,760 hours in Seoul, the weather at each, and the bikes rented",
        licence="CC BY 4.0",
        source="https://archive.ics.uci.edu/dataset/560/seoul+bike+sharing+demand",
        classes=(),
        url="https://archive.ics.uci.edu/static/public/560/data.csv",
        header=True,
        dates="%d/%m/%Y",
        fields=_SEOUL_BIKE_SHARING_FIELDS,
        codes={
            "seasons": ("Autumn", "Spring", "Summer", "Winter"),
            "holiday": ("Holiday", "No Holiday"),
            "functioning_day": ("No", "Yes"),
        },
    ),
    "sepsis_survival": Table(
        name="sepsis_survival",
        label="Sepsis Survival Minimal Clinical Records",
        title="110,341 hospital admissions for sepsis in Norway, and who lived",
        licence="CC BY 4.0",
        source="https://archive.ics.uci.edu/dataset/827/sepsis+survival+minimal+clinical+records",
        classes=("died", "survived"),
        url="https://archive.ics.uci.edu/static/public/827/data.csv",
        header=True,
        fields=_SEPSIS_SURVIVAL_FIELDS,
        labels={"0": 0, "1": 1},
    ),
    "skin_segmentation": Table(
        name="skin_segmentation",
        label="Skin Segmentation",
        title="245,057 pixels of face and background, as three colour channels",
        licence="CC BY 4.0",
        source="https://archive.ics.uci.edu/dataset/229/skin+segmentation",
        classes=("skin", "not_skin"),
        url="https://archive.ics.uci.edu/static/public/229/data.csv",
        header=True,
        fields=_SKIN_SEGMENTATION_FIELDS,
        labels={"1": 0, "2": 1},
    ),
    "soybean_cultivars": Table(
        name="soybean_cultivars",
        label="Forty Soybean Cultivars from Subsequent Harvests",
        title="320 soybean plants of forty cultivars, and the grain each yielded",
        licence="CC BY 4.0",
        source="https://archive.ics.uci.edu/dataset/913/forty+soybean+cultivars+from+subsequent+harvests",
        classes=(),
        url="https://archive.ics.uci.edu/static/public/913/data.csv",
        header=True,
        fields=_SOYBEAN_CULTIVARS_FIELDS,
        codes={
            "cultivar": (
                "74K75RSF CE",
                "77HO111I2X - GUAPORÉ",
                "79I81RSF IPRO",
                "82HO111 IPRO - HO COXIM IPRO",
                "82I78RSF IPRO",
                "83IX84RSF I2X",
                "96R29 IPRO",
                "97Y97 IPRO",
                "98R30 CE",
                "ADAPTA LTT 8402 IPRO",
                "ATAQUE I2X",
                "BRASMAX BÔNUS IPRO",
                "BRASMAX OLIMPO IPRO",
                "ELISA IPRO",
                "EXPANDE LTT 8301 IPRO",
                "FORTALECE L090183 RR",
                "FORTALEZA IPRO",
                "FTR 3179 IPRO",
                "FTR 3190 IPRO",
                "FTR 3868 IPRO",
                "FTR 4280 IPRO",
                "FTR 4288 IPRO",
                "GNS7700 IPRO",
                "GNS7900 IPRO - AMPLA",
                "LAT 1330BT",
                "LTT 7901 IPRO",
                "LYNDA IPRO",
                "M 8644 IPRO",
                "MANU IPRO",
                "MONSOY 8330I2X",
                "MONSOY M8606I2X",
                "NEO 760 CE",
                "NEO 790 IPRO",
                "NK 7777 IPRO",
                "NK 8100 IPRO",
                "NK 8770 IPRO",
                "PAULA IPRO",
                "SUZY IPRO",
                "SYN2282IPRO",
                "TMG 22X83I2X",
            ),
        },
    ),
    "soybean_small": Table(
        name="soybean_small",
        label="Soybean (Small)",
        title="47 soybean plants with one of four diseases, described 35 ways",
        licence="CC BY 4.0",
        source="https://archive.ics.uci.edu/dataset/91/soybean+small",
        classes=(
            "diaporthe_stem_canker",
            "charcoal_rot",
            "rhizoctonia_root_rot",
            "phytophthora_rot",
        ),
        url="https://archive.ics.uci.edu/static/public/91/data.csv",
        header=True,
        fields=_SOYBEAN_SMALL_FIELDS,
        labels={"D1": 0, "D2": 1, "D3": 2, "D4": 3},
    ),
    "spect_heart": Table(
        name="spect_heart",
        label="SPECT Heart",
        title="267 cardiac SPECT images reduced to 22 binary patterns, normal or not",
        licence="CC BY 4.0",
        source="https://archive.ics.uci.edu/dataset/95/spect+heart",
        classes=("normal", "abnormal"),
        url="https://archive.ics.uci.edu/static/public/95/data.csv",
        header=True,
        fields=_SPECT_HEART_FIELDS,
        labels={"0": 0, "1": 1},
    ),
    "spectf_heart": Table(
        name="spectf_heart",
        label="SPECTF Heart",
        title="267 cardiac SPECT images described by 44 counts, normal or not",
        licence="CC BY 4.0",
        source="https://archive.ics.uci.edu/dataset/96/spectf+heart",
        classes=("normal", "abnormal"),
        url="https://archive.ics.uci.edu/static/public/96/data.csv",
        header=True,
        fields=_SPECTF_HEART_FIELDS,
        labels={"0": 0, "1": 1},
    ),
    "splice_junctions": Table(
        name="splice_junctions",
        label="Molecular Biology (Splice-junction Gene Sequences)",
        title="3,190 stretches of DNA and the splice junction, if any, in the middle",
        licence="CC BY 4.0",
        source="https://archive.ics.uci.edu/dataset/69/molecular+biology+splice+junction+gene+sequences",
        classes=("exon_intron", "intron_exon", "neither"),
        url="https://archive.ics.uci.edu/static/public/69/data.csv",
        header=True,
        text_size=24,
        fields=_SPLICE_JUNCTIONS_FIELDS,
        labels={"EI": 0, "IE": 1, "N": 2},
        codes={
            "base1": ("A", "C", "D", "G", "T"),
            "base3": ("A", "C", "G", "T"),
            "base14": ("A", "C", "G", "N", "T"),
            "base35": ("A", "C", "G", "N", "R", "T"),
            "base36": ("A", "C", "G", "N", "S", "T"),
        },
    ),
    "steel_plates": Table(
        name="steel_plates",
        label="Steel Plates Faults",
        title="1,941 steel plates and the seven kinds of surface fault found on them",
        licence="CC BY 4.0",
        source="https://archive.ics.uci.edu/dataset/198/steel+plates+faults",
        classes=(),
        url="https://archive.ics.uci.edu/static/public/198/data.csv",
        header=True,
        fields=_STEEL_PLATES_FIELDS,
    ),
    "student_academics": Table(
        name="student_academics",
        label="Student Academics Performance",
        title="131 students in Kalyani and the band each ended the semester in",
        licence="CC BY 4.0",
        source="https://archive.ics.uci.edu/dataset/467/student+academics+performance",
        classes=("best", "very_good", "good", "pass"),
        url="https://archive.ics.uci.edu/static/public/467/data.csv",
        header=True,
        fields=_STUDENT_ACADEMICS_FIELDS,
        labels={"Best": 0, "Vg": 1, "Good": 2, "Pass": 3},
        codes={
            "ge": ("F", "M"),
            "cst": ("G", "MOBC", "OBC", "SC", "ST"),
            "tnp": ("Best", "Good", "Pass", "Vg"),
            "arr": ("N", "Y"),
            "ms": ("Unmarried",),
            "ls": ("T", "V"),
            "as": ("Free", "Paid"),
            "fmi": ("Am", "High", "Low", "Medium", "Vh"),
            "fs": ("Average", "Large", "Small"),
            "fq": ("10", "12", "Degree", "Il", "Pg", "Um"),
            "fo": ("Business", "Farmer", "Others", "Retired", "Service"),
            "mo": ("Business", "Housewife", "Others", "Retired", "Service"),
            "sh": ("Average", "Good", "Poor"),
            "ss": ("Govt", "Private"),
            "me": ("Asm", "Ben", "Eng", "Hin"),
        },
    ),
    "student_dropout": Table(
        name="student_dropout",
        label="Predict Students' Dropout and Academic Success",
        title="4,424 university students, and whether each dropped out, stayed on or graduated",
        licence="CC BY 4.0",
        source="https://archive.ics.uci.edu/dataset/697/predict+students+dropout+and+academic+success",
        classes=("dropout", "enrolled", "graduate"),
        url="https://archive.ics.uci.edu/static/public/697/data.csv",
        header=True,
        fields=_STUDENT_DROPOUT_FIELDS,
        labels={"Dropout": 0, "Enrolled": 1, "Graduate": 2},
    ),
    "superconductivity": Table(
        name="superconductivity",
        label="Superconductivty Data",
        title="21,263 superconductors described by their chemistry, and the critical temperature",
        licence="CC BY 4.0",
        source="https://archive.ics.uci.edu/dataset/464/superconductivty+data",
        classes=(),
        url="https://archive.ics.uci.edu/static/public/464/data.csv",
        header=True,
        fields=_SUPERCONDUCTIVITY_FIELDS,
    ),
    "support2": Table(
        name="support2",
        label="SUPPORT2",
        title="9,105 seriously ill hospital patients, and who died before going home",
        licence="CC BY 4.0",
        source="https://archive.ics.uci.edu/dataset/880/support2",
        classes=("left_hospital", "died_in_hospital"),
        url="https://archive.ics.uci.edu/static/public/880/data.csv",
        header=True,
        fields=_SUPPORT2_FIELDS,
        labels={"0": 0, "1": 1},
        codes={
            "sex": ("female", "male"),
            "dzgroup": (
                "ARF/MOSF w/Sepsis",
                "CHF",
                "COPD",
                "Cirrhosis",
                "Colon Cancer",
                "Coma",
                "Lung Cancer",
                "MOSF w/Malig",
            ),
            "dzclass": ("ARF/MOSF", "COPD/CHF/Cirrhosis", "Cancer", "Coma"),
            "income": ("$11-$25k", "$25-$50k", ">$50k", "under $11k"),
            "race": ("asian", "black", "hispanic", "other", "white"),
            "ca": ("metastatic", "no", "yes"),
            "dnr": ("dnr after sadm", "dnr before sadm", "no dnr"),
            "sfdm2": (
                "<2 mo. follow-up",
                "Coma or Intub",
                "SIP>=30",
                "adl>=4 (>=5 if sur)",
                "no(M2 and SIP pres)",
            ),
        },
    ),
    "taiwanese_bankruptcy": Table(
        name="taiwanese_bankruptcy",
        label="Taiwanese Bankruptcy Prediction",
        title="6,819 Taiwanese companies described by 95 financial ratios, solvent or bankrupt",
        licence="CC BY 4.0",
        source="https://archive.ics.uci.edu/dataset/572/taiwanese+bankruptcy+prediction",
        classes=("solvent", "bankrupt"),
        url="https://archive.ics.uci.edu/static/public/572/data.csv",
        header=True,
        fields=_TAIWANESE_BANKRUPTCY_FIELDS,
        labels={"0": 0, "1": 1},
    ),
    "tennis_majors": Table(
        name="tennis_majors",
        label="Tennis Major Tournament Match Statistics",
        title="943 matches at the 2013 grand slams, point by point, and who won",
        licence="CC BY 4.0",
        source="https://archive.ics.uci.edu/dataset/300/tennis+major+tournament+match+statistics",
        classes=("player_1_lost", "player_1_won"),
        url="https://archive.ics.uci.edu/static/public/300/data.csv",
        header=True,
        text_size=26,
        fields=_TENNIS_MAJORS_FIELDS,
        labels={"0": 0, "1": 1},
        codes={
            "tournament": (
                "AusOpen-men",
                "AusOpen-women",
                "FrenchOpen-men",
                "FrenchOpen-women",
                "USOpen-men",
                "USOpen-women",
            ),
        },
    ),
    "tetouan_power": Table(
        name="tetouan_power",
        label="Power Consumption of Tetouan City",
        title="52,416 ten-minute readings of Tetouan's weather and the power its three zones drew",
        licence="CC BY 4.0",
        source="https://archive.ics.uci.edu/dataset/849/power+consumption+of+tetouan+city",
        classes=(),
        url="https://archive.ics.uci.edu/static/public/849/data.csv",
        header=True,
        dates="%m/%d/%Y %H:%M",
        fields=_TETOUAN_POWER_FIELDS,
    ),
    "thoracic_surgery": Table(
        name="thoracic_surgery",
        label="Thoracic Surgery Data",
        title="470 lung cancer operations and who was still alive a year later",
        licence="CC BY 4.0",
        source="https://archive.ics.uci.edu/dataset/277/thoracic+surgery+data",
        classes=("survived", "died"),
        url="https://archive.ics.uci.edu/static/public/277/data.csv",
        header=True,
        fields=_THORACIC_SURGERY_FIELDS,
        labels={"F": 0, "T": 1},
        codes={
            "dgn": ("DGN1", "DGN2", "DGN3", "DGN4", "DGN5", "DGN6", "DGN8"),
            "pre6": ("PRZ0", "PRZ1", "PRZ2"),
            "pre7": ("F", "T"),
            "pre14": ("OC11", "OC12", "OC13", "OC14"),
        },
    ),
    "thyroid_recurrence": Table(
        name="thyroid_recurrence",
        label="Differentiated Thyroid Cancer Recurrence",
        title="383 thyroid cancer patients followed fifteen years, and whose cancer came back",
        licence="CC BY 4.0",
        source="https://archive.ics.uci.edu/dataset/915/differentiated+thyroid+cancer+recurrence",
        classes=("no", "yes"),
        url="https://archive.ics.uci.edu/static/public/915/data.csv",
        header=True,
        fields=_THYROID_RECURRENCE_FIELDS,
        labels={"No": 0, "Yes": 1},
        codes={
            "gender": ("F", "M"),
            "smoking": ("No", "Yes"),
            "thyroid_function": (
                "Clinical Hyperthyroidism",
                "Clinical Hypothyroidism",
                "Euthyroid",
                "Subclinical Hyperthyroidism",
                "Subclinical Hypothyroidism",
            ),
            "physical_examination": (
                "Diffuse goiter",
                "Multinodular goiter",
                "Normal",
                "Single nodular goiter-left",
                "Single nodular goiter-right",
            ),
            "adenopathy": ("Bilateral", "Extensive", "Left", "No", "Posterior", "Right"),
            "pathology": ("Follicular", "Hurthel cell", "Micropapillary", "Papillary"),
            "focality": ("Multi-Focal", "Uni-Focal"),
            "risk": ("High", "Intermediate", "Low"),
            "t": ("T1a", "T1b", "T2", "T3a", "T3b", "T4a", "T4b"),
            "n": ("N0", "N1a", "N1b"),
            "m": ("M0", "M1"),
            "stage": ("I", "II", "III", "IVA", "IVB"),
            "response": (
                "Biochemical Incomplete",
                "Excellent",
                "Indeterminate",
                "Structural Incomplete",
            ),
        },
    ),
    "user_knowledge": Table(
        name="user_knowledge",
        label="User Knowledge Modeling",
        title="403 students studying DC machines, and how well each knew the subject",
        licence="CC BY 4.0",
        source="https://archive.ics.uci.edu/dataset/257/user+knowledge+modeling",
        classes=("very_low", "low", "middle", "high"),
        url="https://archive.ics.uci.edu/static/public/257/data.csv",
        header=True,
        fields=_USER_KNOWLEDGE_FIELDS,
        labels={"very_low": 0, "Very Low": 0, "Low": 1, "Middle": 2, "High": 3},
    ),
    "waveform": Table(
        name="waveform",
        label="Waveform Database Generator (Version 1)",
        title="5,000 generated waveforms of three kinds, described by 21 noisy attributes",
        licence="CC BY 4.0",
        source="https://archive.ics.uci.edu/dataset/107/waveform+database+generator+version+1",
        classes=("wave_0", "wave_1", "wave_2"),
        url="https://archive.ics.uci.edu/static/public/107/data.csv",
        header=True,
        fields=_WAVEFORM_FIELDS,
        labels={"0": 0, "1": 1, "2": 2},
    ),
    "website_phishing": Table(
        name="website_phishing",
        label="Website Phishing",
        title="1,353 web pages judged phishing, suspicious or legitimate on nine signs",
        licence="CC BY 4.0",
        source="https://archive.ics.uci.edu/dataset/379/website+phishing",
        classes=("phishing", "suspicious", "legitimate"),
        url="https://archive.ics.uci.edu/static/public/379/data.csv",
        header=True,
        fields=_WEBSITE_PHISHING_FIELDS,
        labels={"-1": 0, "0": 1, "1": 2},
    ),
    "wholesale_customers": Table(
        name="wholesale_customers",
        label="Wholesale customers",
        title="440 wholesale customers and what each spent on six kinds of goods",
        licence="CC BY 4.0",
        source="https://archive.ics.uci.edu/dataset/292/wholesale+customers",
        classes=("horeca", "retail"),
        url="https://archive.ics.uci.edu/static/public/292/data.csv",
        header=True,
        fields=_WHOLESALE_CUSTOMERS_FIELDS,
        labels={"1": 0, "2": 1},
    ),
    "youtube_spam": Table(
        name="youtube_spam",
        label="YouTube Spam Collection",
        title="1,956 comments under five pop videos, spam or not",
        licence="CC BY 4.0",
        source="https://archive.ics.uci.edu/dataset/380/youtube+spam+collection",
        classes=("not_spam", "spam"),
        url="https://archive.ics.uci.edu/static/public/380/data.csv",
        header=True,
        text_size=1202,
        fields=_YOUTUBE_SPAM_FIELDS,
        labels={"0": 0, "1": 1},
        codes={
            "video": ("Eminem", "Katy Perry", "LMFAO", "Psy", "Shakira"),
        },
    ),
    "acute_myeloid_leukaemia": Table(
        name="acute_myeloid_leukaemia",
        label="Acute Myeloid Leukaemia Trial",
        title="646 patients in a trial of two treatments for acute myeloid leukaemia",
        licence="LGPL-2 or later",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/survival/myeloid.html",
        classes=("treatment_a", "treatment_b"),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/survival/myeloid.csv",
        header=True,
        fields=_ACUTE_MYELOID_LEUKAEMIA_FIELDS,
        labels={"A": 0, "B": 1},
        codes={
            "sex": ("f", "m"),
            "flt3": ("A", "B", "C"),
        },
    ),
    "alcohol_by_country": Table(
        name="alcohol_by_country",
        label="Alcohol Consumption by Country",
        title="193 countries and how much pure alcohol each drank a head",
        licence="MIT",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/fivethirtyeight/drinks.html",
        classes=(),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/fivethirtyeight/drinks.csv",
        header=True,
        text_size=28,
        fields=_ALCOHOL_BY_COUNTRY_FIELDS,
    ),
    "animal_scat": Table(
        name="animal_scat",
        label="Morphometrics of Animal Scat",
        title="110 droppings found on a trail and which animal left each",
        licence="MIT",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/modeldata/scat.html",
        classes=("bobcat", "coyote", "gray_fox"),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/modeldata/scat.csv",
        header=True,
        fields=_ANIMAL_SCAT_FIELDS,
        labels={"bobcat": 0, "coyote": 1, "gray_fox": 2},
        codes={
            "month": (
                "April",
                "August",
                "February",
                "January",
                "June",
                "May",
                "November",
                "October",
                "September",
            ),
            "site": ("ANNU", "YOLA"),
            "location": ("edge", "middle", "off_edge"),
        },
    ),
    "anorexia_treatment": Table(
        name="anorexia_treatment",
        label="Anorexia Treatment",
        title="72 young women treated for anorexia and what each weighed after",
        licence="GPL-2 or GPL-3",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/MASS/anorexia.html",
        classes=("cognitive_behavioural", "control", "family"),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/MASS/anorexia.csv",
        header=True,
        fields=_ANOREXIA_TREATMENT_FIELDS,
        labels={"CBT": 0, "Cont": 1, "FT": 2},
    ),
    "bad_drivers": Table(
        name="bad_drivers",
        label="Bad Drivers",
        title="51 American states and what car insurance cost a driver in each",
        licence="MIT",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/fivethirtyeight/bad_drivers.html",
        classes=(),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/fivethirtyeight/bad_drivers.csv",
        header=True,
        text_size=20,
        fields=_BAD_DRIVERS_FIELDS,
    ),
    "baseball_batting": Table(
        name="baseball_batting",
        label="Lahman Batting",
        title="128,598 player-seasons of major league batting and the home runs each brought",
        licence="GPL",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/Lahman/Batting.html",
        classes=(),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/Lahman/Batting.csv",
        header=True,
        text_size=9,
        fields=_BASEBALL_BATTING_FIELDS,
        codes={
            "lgid": (
                "AA",
                "AL",
                "ANL",
                "EAS",
                "ECL",
                "EWL",
                "FL",
                "IND",
                "INT",
                "NAC",
                "NAL",
                "NL",
                "NN2",
                "NNL",
                "NSL",
                "PL",
                "UA",
                "WES",
            ),
        },
    ),
    "baseball_fielding": Table(
        name="baseball_fielding",
        label="Lahman Fielding",
        title="174,332 player-seasons of major league fielding and the errors each brought",
        licence="GPL",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/Lahman/Fielding.html",
        classes=(),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/Lahman/Fielding.csv",
        header=True,
        text_size=9,
        fields=_BASEBALL_FIELDING_FIELDS,
        codes={
            "lgid": (
                "AA",
                "AL",
                "ANL",
                "EAS",
                "ECL",
                "EWL",
                "FL",
                "IND",
                "INT",
                "NAC",
                "NAL",
                "NL",
                "NN2",
                "NNL",
                "NSL",
                "PL",
                "UA",
                "WES",
            ),
            "pos": ("1B", "2B", "3B", "C", "OF", "P", "PH", "PR", "SS"),
        },
    ),
    "baseball_hall_of_fame": Table(
        name="baseball_hall_of_fame",
        label="Hall of Fame Voting",
        title="6,426 Hall of Fame ballots and how many votes each player drew",
        licence="GPL",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/Lahman/HallOfFame.html",
        classes=(),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/Lahman/HallOfFame.csv",
        header=True,
        text_size=102,
        fields=_BASEBALL_HALL_OF_FAME_FIELDS,
        codes={
            "inducted": ("N", "Y"),
            "category": (
                "Executive",
                "Manager",
                "Pioneer",
                "Pioneer/Executive",
                "Player",
                "Umpire",
            ),
        },
    ),
    "baseball_hitters": Table(
        name="baseball_hitters",
        label="Major League Hitters",
        title="322 baseball players in the 1986 season and what each was paid",
        licence="GPL-2",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/ISLR/Hitters.html",
        classes=(),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/ISLR/Hitters.csv",
        header=True,
        text_size=18,
        fields=_BASEBALL_HITTERS_FIELDS,
        codes={
            "league": ("A", "N"),
            "division": ("E", "W"),
        },
    ),
    "baseball_managers": Table(
        name="baseball_managers",
        label="Lahman Managers",
        title="4,410 manager-seasons and how many games each won",
        licence="GPL",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/Lahman/Managers.html",
        classes=(),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/Lahman/Managers.csv",
        header=True,
        text_size=9,
        fields=_BASEBALL_MANAGERS_FIELDS,
        codes={
            "lgid": (
                "AA",
                "AL",
                "ANL",
                "EAS",
                "ECL",
                "EWL",
                "FL",
                "IND",
                "INT",
                "NAC",
                "NAL",
                "NL",
                "NN2",
                "NNL",
                "NSL",
                "PL",
                "UA",
                "WES",
            ),
            "plyrmgr": ("N", "Y"),
        },
    ),
    "baseball_pitching": Table(
        name="baseball_pitching",
        label="Lahman Pitching",
        title="57,630 pitcher-seasons and the earned run average each finished with",
        licence="GPL",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/Lahman/Pitching.html",
        classes=(),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/Lahman/Pitching.csv",
        header=True,
        text_size=9,
        fields=_BASEBALL_PITCHING_FIELDS,
        codes={
            "lgid": (
                "AA",
                "AL",
                "ANL",
                "EAS",
                "ECL",
                "EWL",
                "FL",
                "IND",
                "INT",
                "NAC",
                "NAL",
                "NL",
                "NN2",
                "NNL",
                "NSL",
                "PL",
                "UA",
                "WES",
            ),
        },
    ),
    "baseball_players": Table(
        name="baseball_players",
        label="Lahman People",
        title="24,270 major league players and how tall each stood",
        licence="GPL",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/Lahman/People.html",
        classes=(),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/Lahman/People.csv",
        header=True,
        dates="%Y-%m-%d",
        text_size=43,
        fields=_BASEBALL_PLAYERS_FIELDS,
        codes={
            "bats": ("B", "L", "R"),
            "throws": ("B", "L", "R", "S"),
        },
    ),
    "baseball_salaries": Table(
        name="baseball_salaries",
        label="Lahman Salaries",
        title="26,428 player-seasons and what the player was paid",
        licence="GPL",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/Lahman/Salaries.html",
        classes=(),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/Lahman/Salaries.csv",
        header=True,
        text_size=9,
        fields=_BASEBALL_SALARIES_FIELDS,
        codes={
            "lgid": ("AL", "NL"),
        },
    ),
    "bechdel_test": Table(
        name="bechdel_test",
        label="The Bechdel Test",
        title="1,794 films and whether each passes the Bechdel test",
        licence="MIT",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/fivethirtyeight/bechdel.html",
        classes=("fail", "pass"),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/fivethirtyeight/bechdel.csv",
        header=True,
        text_size=83,
        fields=_BECHDEL_TEST_FIELDS,
        labels={"FAIL": 0, "PASS": 1},
        codes={
            "test": (
                "dubious",
                "dubious-disagree",
                "men",
                "men-disagree",
                "notalk",
                "notalk-disagree",
                "nowomen",
                "nowomen-disagree",
                "ok",
                "ok-disagree",
            ),
            "clean_test": ("dubious", "men", "notalk", "nowomen", "ok"),
        },
    ),
    "biliary_cholangitis": Table(
        name="biliary_cholangitis",
        label="Mayo Clinic Primary Biliary Cholangitis",
        title="418 patients with primary biliary cholangitis and how long each lived",
        licence="LGPL-2 or later",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/survival/pbc.html",
        classes=(),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/survival/pbc.csv",
        header=True,
        fields=_BILIARY_CHOLANGITIS_FIELDS,
        codes={
            "sex": ("f", "m"),
        },
    ),
    "black_cherry_trees": Table(
        name="black_cherry_trees",
        label="Black Cherry Trees",
        title="31 felled black cherry trees and how much timber each held",
        licence="GPL-2 or GPL-3",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/datasets/trees.html",
        classes=(),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/datasets/trees.csv",
        header=True,
        fields=_BLACK_CHERRY_TREES_FIELDS,
    ),
    "bladder_tumours": Table(
        name="bladder_tumours",
        label="Bladder Tumour Recurrences",
        title="340 follow-up records from a trial of thiotepa against bladder tumours",
        licence="LGPL-2 or later",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/survival/bladder.html",
        classes=(),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/survival/bladder.csv",
        header=True,
        fields=_BLADDER_TUMOURS_FIELDS,
    ),
    "boating_trips": Table(
        name="boating_trips",
        label="Boating Trips to Lake Somerville",
        title="659 households and how many boating trips each took in a season",
        licence="GPL-2 or GPL-3",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/AER/RecreationDemand.html",
        classes=(),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/AER/RecreationDemand.csv",
        header=True,
        fields=_BOATING_TRIPS_FIELDS,
        codes={
            "ski": ("no", "yes"),
        },
    ),
    "boston_housing": Table(
        name="boston_housing",
        label="Boston Housing",
        title="506 census tracts around Boston and what a home in each was worth",
        licence="GPL-2 or GPL-3",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/MASS/Boston.html",
        classes=(),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/MASS/Boston.csv",
        header=True,
        fields=_BOSTON_HOUSING_FIELDS,
    ),
    "breast_cancer_gbsg": Table(
        name="breast_cancer_gbsg",
        label="German Breast Cancer Study",
        title="686 women with node-positive breast cancer and how long each went clear",
        licence="LGPL-2 or later",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/survival/gbsg.html",
        classes=(),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/survival/gbsg.csv",
        header=True,
        fields=_BREAST_CANCER_GBSG_FIELDS,
    ),
    "brushtail_possums": Table(
        name="brushtail_possums",
        label="Possums in Australia and New Guinea",
        title="104 brushtail possums trapped and measured, and where each was caught",
        licence="GPL-3",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/openintro/possum.html",
        classes=("victoria", "elsewhere"),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/openintro/possum.csv",
        header=True,
        fields=_BRUSHTAIL_POSSUMS_FIELDS,
        labels={"Vic": 0, "other": 1},
        codes={
            "sex": ("f", "m"),
        },
    ),
    "california_schools": Table(
        name="california_schools",
        label="California Test Scores",
        title="420 Californian school districts and how their fifth-graders scored",
        licence="GPL-2 or GPL-3",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/AER/CASchools.html",
        classes=(),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/AER/CASchools.csv",
        header=True,
        text_size=39,
        fields=_CALIFORNIA_SCHOOLS_FIELDS,
        codes={
            "grades": ("KK-06", "KK-08"),
        },
    ),
    "canadian_interlocks": Table(
        name="canadian_interlocks",
        label="Interlocking Directorates in Canada",
        title="248 Canadian firms and how many boards each was tied to",
        licence="GPL-2 or later",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/carData/Ornstein.html",
        classes=(),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/carData/Ornstein.csv",
        header=True,
        fields=_CANADIAN_INTERLOCKS_FIELDS,
        codes={
            "sector": ("AGR", "BNK", "CON", "FIN", "HLD", "MAN", "MER", "MIN", "TRN", "WOD"),
            "nation": ("CAN", "OTH", "UK", "US"),
        },
    ),
    "canadian_womens_work": Table(
        name="canadian_womens_work",
        label="Canadian Women's Labour Force",
        title="263 Canadian women and whether each worked full time, part time or not at all",
        licence="GPL-2 or later",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/carData/Womenlf.html",
        classes=("not_working", "part_time", "full_time"),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/carData/Womenlf.csv",
        header=True,
        fields=_CANADIAN_WOMENS_WORK_FIELDS,
        labels={"not.work": 0, "parttime": 1, "fulltime": 2},
        codes={
            "children": ("absent", "present"),
            "region": ("Atlantic", "BC", "Ontario", "Prairie", "Quebec"),
        },
    ),
    "candy_rankings": Table(
        name="candy_rankings",
        label="Candy Power Ranking",
        title="85 sweets and how often each won a head-to-head vote",
        licence="MIT",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/fivethirtyeight/candy_rankings.html",
        classes=(),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/fivethirtyeight/candy_rankings.csv",
        header=True,
        text_size=27,
        fields=_CANDY_RANKINGS_FIELDS,
        codes={
            "chocolate": ("FALSE", "TRUE"),
        },
    ),
    "car_seat_sales": Table(
        name="car_seat_sales",
        label="Child Car Seat Sales",
        title="400 stores and how many child car seats each sold",
        licence="GPL-2",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/ISLR/Carseats.html",
        classes=(),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/ISLR/Carseats.csv",
        header=True,
        fields=_CAR_SEAT_SALES_FIELDS,
        codes={
            "shelveloc": ("Bad", "Good", "Medium"),
            "urban": ("No", "Yes"),
        },
    ),
    "card_default": Table(
        name="card_default",
        label="Credit Card Default",
        title="10,000 cardholders and whether each defaulted",
        licence="GPL-2",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/ISLR/Default.html",
        classes=("paid", "defaulted"),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/ISLR/Default.csv",
        header=True,
        fields=_CARD_DEFAULT_FIELDS,
        labels={"No": 0, "Yes": 1},
        codes={
            "student": ("No", "Yes"),
        },
    ),
    "cat_hearts": Table(
        name="cat_hearts",
        label="Anatomy of Domestic Cats",
        title="144 adult cats weighed body and heart, and the sex of each",
        licence="GPL-2 or GPL-3",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/MASS/cats.html",
        classes=("female", "male"),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/MASS/cats.csv",
        header=True,
        fields=_CAT_HEARTS_FIELDS,
        labels={"F": 0, "M": 1},
    ),
    "chicago_taxi": Table(
        name="chicago_taxi",
        label="Chicago Taxi Tips",
        title="10,000 Chicago taxi rides and whether the driver was tipped",
        licence="MIT",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/modeldata/taxi.html",
        classes=("no_tip", "tipped"),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/modeldata/taxi.csv",
        header=True,
        fields=_CHICAGO_TAXI_FIELDS,
        labels={"no": 0, "yes": 1},
        codes={
            "company": (
                "Chicago Independents",
                "City Service",
                "Flash Cab",
                "Sun Taxi",
                "Taxi Affiliation Services",
                "Taxicab Insurance Agency Llc",
                "other",
            ),
            "local": ("no", "yes"),
            "dow": ("Fri", "Mon", "Sat", "Sun", "Thu", "Tue", "Wed"),
            "month": ("Apr", "Feb", "Jan", "Mar"),
        },
    ),
    "chick_weights": Table(
        name="chick_weights",
        label="Chick Weights",
        title="578 weighings of chicks fed four different diets",
        licence="GPL-2 or GPL-3",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/datasets/ChickWeight.html",
        classes=(),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/datasets/ChickWeight.csv",
        header=True,
        fields=_CHICK_WEIGHTS_FIELDS,
    ),
    "chile_plebiscite": Table(
        name="chile_plebiscite",
        label="The 1988 Chilean Plebiscite",
        title="2,700 Chilean voters and how each said they would vote",
        licence="GPL-2 or later",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/carData/Chile.html",
        classes=("abstain", "no", "undecided", "yes", "no_answer"),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/carData/Chile.csv",
        header=True,
        fields=_CHILE_PLEBISCITE_FIELDS,
        labels={"A": 0, "N": 1, "U": 2, "Y": 3, "": 4},
        codes={
            "region": ("C", "M", "N", "S", "SA"),
            "sex": ("F", "M"),
            "education": ("P", "PS", "S"),
        },
    ),
    "chocolate_cakes": Table(
        name="chocolate_cakes",
        label="Breakage Angle of Chocolate Cakes",
        title="270 chocolate cakes baked to three recipes and the angle each broke at",
        licence="GPL-2 or later",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/lme4/cake.html",
        classes=(),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/lme4/cake.csv",
        header=True,
        fields=_CHOCOLATE_CAKES_FIELDS,
        codes={
            "recipe": ("A", "B", "C"),
        },
    ),
    "college_distance": Table(
        name="college_distance",
        label="College Distance",
        title="4,739 American schoolchildren and how far each lived from a college",
        licence="GPL-2 or GPL-3",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/AER/CollegeDistance.html",
        classes=(),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/AER/CollegeDistance.csv",
        header=True,
        fields=_COLLEGE_DISTANCE_FIELDS,
        codes={
            "gender": ("female", "male"),
            "ethnicity": ("afam", "hispanic", "other"),
            "fcollege": ("no", "yes"),
            "income": ("high", "low"),
            "region": ("other", "west"),
        },
    ),
    "college_majors": Table(
        name="college_majors",
        label="The Economic Guide to Picking a College Major",
        title="173 college majors and what their graduates earned",
        licence="MIT",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/fivethirtyeight/college_recent_grads.html",
        classes=(),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/fivethirtyeight/college_recent_grads.csv",
        header=True,
        text_size=65,
        fields=_COLLEGE_MAJORS_FIELDS,
        codes={
            "major_category": (
                "Agriculture & Natural Resources",
                "Arts",
                "Biology & Life Science",
                "Business",
                "Communications & Journalism",
                "Computers & Mathematics",
                "Education",
                "Engineering",
                "Health",
                "Humanities & Liberal Arts",
                "Industrial Arts & Consumer Services",
                "Interdisciplinary",
                "Law & Public Policy",
                "Physical Sciences",
                "Psychology & Social Work",
                "Social Science",
            ),
        },
    ),
    "colon_cancer_trial": Table(
        name="colon_cancer_trial",
        label="Chemotherapy for Colon Cancer",
        title="1,858 records from a trial of levamisole and fluorouracil after surgery",
        licence="LGPL-2 or later",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/survival/colon.html",
        classes=("observation", "levamisole", "levamisole_and_fluorouracil"),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/survival/colon.csv",
        header=True,
        fields=_COLON_CANCER_TRIAL_FIELDS,
        labels={"Obs": 0, "Lev": 1, "Lev+5FU": 2},
    ),
    "commercial_oils": Table(
        name="commercial_oils",
        label="Fatty Acids in Commercial Oils",
        title="96 samples of commercial oil and which plant each was pressed from",
        licence="MIT",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/modeldata/oils.html",
        classes=("corn", "olive", "peanut", "pumpkin", "rapeseed", "soybean", "sunflower"),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/modeldata/oils.csv",
        header=True,
        fields=_COMMERCIAL_OILS_FIELDS,
        labels={
            "corn": 0,
            "olive": 1,
            "peanut": 2,
            "pumpkin": 3,
            "rapeseed": 4,
            "soybean": 5,
            "sunflower": 6,
        },
    ),
    "congress_age": Table(
        name="congress_age",
        label="The Age of Congress",
        title="18,635 terms served in the US Congress and how old the member was",
        licence="MIT",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/fivethirtyeight/congress_age.html",
        classes=(),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/fivethirtyeight/congress_age.csv",
        header=True,
        dates="%Y-%m-%d",
        text_size=19,
        fields=_CONGRESS_AGE_FIELDS,
        codes={
            "chamber": ("house", "senate"),
            "suffix": ("II", "III", "IV", "Jr.", "Sr."),
            "party": ("AL", "D", "I", "ID", "L", "R"),
            "incumbent": ("FALSE", "TRUE"),
        },
    ),
    "cow_milk_protein": Table(
        name="cow_milk_protein",
        label="Protein in Cows' Milk",
        title="1,337 weekly milk samples from cows fed three diets",
        licence="GPL-2 or later",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/nlme/Milk.html",
        classes=(),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/nlme/Milk.csv",
        header=True,
        text_size=4,
        fields=_COW_MILK_PROTEIN_FIELDS,
        codes={
            "diet": ("barley", "barley+lupins", "lupins"),
        },
    ),
    "cps_wages": Table(
        name="cps_wages",
        label="Wages in the 1985 Current Population Survey",
        title="534 American workers surveyed in 1985 and what each earned an hour",
        licence="GPL-2 or GPL-3",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/AER/CPS1985.html",
        classes=(),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/AER/CPS1985.csv",
        header=True,
        fields=_CPS_WAGES_FIELDS,
        codes={
            "ethnicity": ("cauc", "hispanic", "other"),
            "region": ("other", "south"),
            "gender": ("female", "male"),
            "occupation": ("management", "office", "sales", "services", "technical", "worker"),
            "sector": ("construction", "manufacturing", "other"),
            "union": ("no", "yes"),
        },
    ),
    "credit_card_applications": Table(
        name="credit_card_applications",
        label="Credit Card Applications",
        title="1,319 credit card applications and whether each was accepted",
        licence="GPL-2 or GPL-3",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/AER/CreditCard.html",
        classes=("refused", "accepted"),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/AER/CreditCard.csv",
        header=True,
        fields=_CREDIT_CARD_APPLICATIONS_FIELDS,
        labels={"no": 0, "yes": 1},
        codes={
            "owner": ("no", "yes"),
        },
    ),
    "credit_card_balance": Table(
        name="credit_card_balance",
        label="Credit Card Balances",
        title="400 cardholders and the balance each carried",
        licence="GPL-2",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/ISLR/Credit.html",
        classes=(),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/ISLR/Credit.csv",
        header=True,
        fields=_CREDIT_CARD_BALANCE_FIELDS,
        codes={
            "gender": ("Female", "Male"),
            "student": ("No", "Yes"),
            "ethnicity": ("African American", "Asian", "Caucasian"),
        },
    ),
    "developer_survey": Table(
        name="developer_survey",
        label="Stack Overflow Developer Survey",
        title="5,594 software developers and what each was paid",
        licence="MIT",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/modeldata/stackoverflow.html",
        classes=(),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/modeldata/stackoverflow.csv",
        header=True,
        fields=_DEVELOPER_SURVEY_FIELDS,
        codes={
            "country": ("Canada", "Germany", "India", "United Kingdom", "United States"),
            "remote": ("Not remote", "Remote"),
        },
    ),
    "diamonds": Table(
        name="diamonds",
        label="Diamond Prices",
        title="53,940 round-cut diamonds and what each sold for",
        licence="MIT",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/ggplot2/diamonds.html",
        classes=(),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/ggplot2/diamonds.csv",
        header=True,
        fields=_DIAMONDS_FIELDS,
        codes={
            "cut": ("Fair", "Good", "Ideal", "Premium", "Very Good"),
            "color": ("D", "E", "F", "G", "H", "I", "J"),
            "clarity": ("I1", "IF", "SI1", "SI2", "VS1", "VS2", "VVS1", "VVS2"),
        },
    ),
    "dnase_assay": Table(
        name="dnase_assay",
        label="DNase ELISA Assay",
        title="176 wells of a DNase assay and the optical density each read",
        licence="GPL-2 or GPL-3",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/datasets/DNase.html",
        classes=(),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/datasets/DNase.csv",
        header=True,
        fields=_DNASE_ASSAY_FIELDS,
    ),
    "doctor_visits": Table(
        name="doctor_visits",
        label="Australian Doctor Visits",
        title="5,190 Australians in the 1977 health survey and how often each saw a doctor",
        licence="GPL-2 or GPL-3",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/AER/DoctorVisits.html",
        classes=(),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/AER/DoctorVisits.csv",
        header=True,
        fields=_DOCTOR_VISITS_FIELDS,
        codes={
            "gender": ("female", "male"),
            "private": ("no", "yes"),
        },
    ),
    "doctoral_publications": Table(
        name="doctoral_publications",
        label="Doctoral Publications",
        title="915 biochemistry doctoral students and how many papers each published",
        licence="GPL-2 or GPL-3",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/AER/PhDPublications.html",
        classes=(),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/AER/PhDPublications.csv",
        header=True,
        fields=_DOCTORAL_PUBLICATIONS_FIELDS,
        codes={
            "gender": ("female", "male"),
            "married": ("no", "yes"),
        },
    ),
    "earthquake_intensity": Table(
        name="earthquake_intensity",
        label="Earthquake Intensity",
        title="182 seismometer readings and the ground acceleration each recorded",
        licence="GPL-2 or later",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/nlme/Earthquake.html",
        classes=(),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/nlme/Earthquake.csv",
        header=True,
        fields=_EARTHQUAKE_INTENSITY_FIELDS,
    ),
    "economic_growth": Table(
        name="economic_growth",
        label="Determinants of Economic Growth",
        title="121 countries in the Mankiw, Romer and Weil growth study and how fast each grew",
        licence="GPL-2 or GPL-3",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/AER/GrowthDJ.html",
        classes=(),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/AER/GrowthDJ.csv",
        header=True,
        fields=_ECONOMIC_GROWTH_FIELDS,
        codes={
            "oil": ("no", "yes"),
        },
    ),
    "economics_journals": Table(
        name="economics_journals",
        label="Economics Journal Subscriptions",
        title="180 economics journals and how many libraries subscribed to each",
        licence="GPL-2 or GPL-3",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/AER/Journals.html",
        classes=(),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/AER/Journals.csv",
        header=True,
        text_size=55,
        fields=_ECONOMICS_JOURNALS_FIELDS,
        codes={
            "society": ("no", "yes"),
            "field": (
                "Agricultural Economics",
                "Area Studies",
                "Business",
                "Consumer Economics",
                "Demography",
                "Development",
                "Econometrics",
                "Economic History",
                "Finance",
                "General",
                "Health",
                "Industrial Organization",
                "Insurance",
                "Interdisciplinary",
                "International",
                "Labor",
                "Law and Economics",
                "Macroeconomics",
                "Management Science",
                "Natural Resources",
                "Public Finance",
                "Specialized",
                "Theory",
                "Urban and Regional",
            ),
        },
    ),
    "email_spam": Table(
        name="email_spam",
        label="Email Spam",
        title="3,921 emails to one account and whether each was spam",
        licence="GPL-3",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/openintro/email.html",
        classes=("ham", "spam"),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/openintro/email.csv",
        header=True,
        dates="%Y-%m-%dT%H:%M:%SZ",
        fields=_EMAIL_SPAM_FIELDS,
        labels={"0": 0, "1": 1},
        codes={
            "winner": ("no", "yes"),
            "number": ("big", "none", "small"),
        },
    ),
    "epilepsy_seizures": Table(
        name="epilepsy_seizures",
        label="Seizure Counts for Epileptics",
        title="236 clinic visits by epileptic patients and the seizures each brought",
        licence="GPL-2 or GPL-3",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/MASS/epil.html",
        classes=(),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/MASS/epil.csv",
        header=True,
        fields=_EPILEPSY_SEIZURES_FIELDS,
        codes={
            "trt": ("placebo", "progabide"),
        },
    ),
    "exercise_histories": Table(
        name="exercise_histories",
        label="Exercise Histories of Eating-Disordered Subjects",
        title="945 exercise reports from girls in treatment and their controls",
        licence="GPL-2 or later",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/carData/Blackmore.html",
        classes=("control", "patient"),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/carData/Blackmore.csv",
        header=True,
        text_size=4,
        fields=_EXERCISE_HISTORIES_FIELDS,
        labels={"control": 0, "patient": 1},
    ),
    "extramarital_affairs": Table(
        name="extramarital_affairs",
        label="Fair's Extramarital Affairs",
        title="601 people answering a 1969 magazine survey and how often each strayed",
        licence="GPL-2 or GPL-3",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/AER/Affairs.html",
        classes=(),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/AER/Affairs.csv",
        header=True,
        fields=_EXTRAMARITAL_AFFAIRS_FIELDS,
        codes={
            "gender": ("female", "male"),
            "children": ("no", "yes"),
        },
    ),
    "fandango_ratings": Table(
        name="fandango_ratings",
        label="Fandango Film Ratings",
        title="146 films and how four websites rated each",
        licence="MIT",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/fivethirtyeight/fandango.html",
        classes=(),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/fivethirtyeight/fandango.csv",
        header=True,
        text_size=63,
        fields=_FANDANGO_RATINGS_FIELDS,
    ),
    "fast_food_nutrition": Table(
        name="fast_food_nutrition",
        label="Nutrition in Fast Food",
        title="515 fast food items and how many calories each holds",
        licence="GPL-3",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/openintro/fastfood.html",
        classes=(),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/openintro/fastfood.csv",
        header=True,
        text_size=63,
        fields=_FAST_FOOD_NUTRITION_FIELDS,
        codes={
            "restaurant": (
                "Arbys",
                "Burger King",
                "Chick Fil-A",
                "Dairy Queen",
                "Mcdonalds",
                "Sonic",
                "Subway",
                "Taco Bell",
            ),
            "salad": ("Other",),
        },
    ),
    "fatty_liver_disease": Table(
        name="fatty_liver_disease",
        label="Non-Alcoholic Fatty Liver Disease",
        title="17,549 residents of Olmsted County and how heavy each was",
        licence="LGPL-2 or later",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/survival/nafld1.html",
        classes=(),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/survival/nafld1.csv",
        header=True,
        fields=_FATTY_LIVER_DISEASE_FIELDS,
    ),
    "fertility_labour": Table(
        name="fertility_labour",
        label="Fertility and Women's Labour Supply",
        title="254,654 American mothers in the 1980 census and whether each had a third child",
        licence="GPL-2 or GPL-3",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/AER/Fertility.html",
        classes=("two", "three_or_more"),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/AER/Fertility.csv",
        header=True,
        fields=_FERTILITY_LABOUR_FIELDS,
        labels={"no": 0, "yes": 1},
        codes={
            "gender1": ("female", "male"),
            "afam": ("no", "yes"),
        },
    ),
    "fiji_earthquakes": Table(
        name="fiji_earthquakes",
        label="Earthquakes off Fiji",
        title="1,000 earthquakes near Fiji and how strong each was",
        licence="GPL-2 or GPL-3",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/datasets/quakes.html",
        classes=(),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/datasets/quakes.csv",
        header=True,
        fields=_FIJI_EARTHQUAKES_FIELDS,
    ),
    "florida_2000_vote": Table(
        name="florida_2000_vote",
        label="Florida County Voting",
        title="67 Florida counties and how each voted in the 2000 presidential election",
        licence="GPL-2 or later",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/carData/Florida.html",
        classes=(),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/carData/Florida.csv",
        header=True,
        text_size=12,
        fields=_FLORIDA_2000_VOTE_FIELDS,
    ),
    "flying_etiquette": Table(
        name="flying_etiquette",
        label="Flying Etiquette",
        title="1,040 air travellers and what each thought of reclining a seat",
        licence="MIT",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/fivethirtyeight/flying.html",
        classes=("not_rude", "somewhat_rude", "very_rude", "no_answer"),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/fivethirtyeight/flying.csv",
        header=True,
        text_size=59,
        fields=_FLYING_ETIQUETTE_FIELDS,
        labels={"No": 0, "Somewhat": 1, "Very": 2, "": 3},
        codes={
            "gender": ("Female", "Male"),
            "age": ("18-29", "30-44", "45-60", "> 60"),
            "height": (
                "5'0\"",
                "5'1\"",
                "5'10\"",
                "5'11\"",
                "5'2\"",
                "5'3\"",
                "5'4\"",
                "5'5\"",
                "5'6\"",
                "5'7\"",
                "5'8\"",
                "5'9\"",
                "6'0\"",
                "6'1\"",
                "6'2\"",
                "6'3\"",
                "6'4\"",
                "6'5\"",
                "6'6\" and above",
                "Under 5 ft.",
            ),
            "children_under_18": ("FALSE", "TRUE"),
            "household_income": (
                "$0 - $24,999",
                "$100,000 - $149,999",
                "$25,000 - $49,999",
                "$50,000 - $99,999",
            ),
            "education": (
                "Bachelor degree",
                "Graduate degree",
                "High school degree",
                "Less than high school degree",
                "Some college or Associate degree",
            ),
            "location": (
                "East North Central",
                "East South Central",
                "Middle Atlantic",
                "Mountain",
                "New England",
                "Pacific",
                "South Atlantic",
                "West North Central",
                "West South Central",
            ),
            "frequency": (
                "A few times per month",
                "A few times per week",
                "Every day",
                "Never",
                "Once a month or less",
                "Once a year or less",
            ),
            "recline_frequency": (
                "About half the time",
                "Always",
                "Never",
                "Once in a while",
                "Usually",
            ),
            "switch_seats_friends": ("No", "Somewhat", "Very"),
            "get_up": (
                "Four times",
                "It is not okay to get up during flight",
                "More than five times times",
                "Once",
                "Three times",
                "Twice",
            ),
        },
    ),
    "free_light_chain": Table(
        name="free_light_chain",
        label="Serum Free Light Chain",
        title="7,874 residents of Olmsted County assayed for serum free light chains",
        licence="LGPL-2 or later",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/survival/flchain.html",
        classes=("female", "male"),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/survival/flchain.csv",
        header=True,
        fields=_FREE_LIGHT_CHAIN_FIELDS,
        labels={"F": 0, "M": 1},
        codes={
            "chapter": (
                "Blood",
                "Circulatory",
                "Congenital",
                "Digestive",
                "Endocrine",
                "External Causes",
                "Genitourinary",
                "Ill Defined",
                "Infectious",
                "Injury and Poisoning",
                "Mental",
                "Musculoskeletal",
                "Neoplasms",
                "Nervous",
                "Respiratory",
                "Skin",
            ),
        },
    ),
    "fuel_economy": Table(
        name="fuel_economy",
        label="Fuel Economy from 1999 to 2008",
        title="234 car models and what each did to the gallon on the highway",
        licence="MIT",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/ggplot2/mpg.html",
        classes=(),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/ggplot2/mpg.csv",
        header=True,
        text_size=22,
        fields=_FUEL_ECONOMY_FIELDS,
        codes={
            "manufacturer": (
                "audi",
                "chevrolet",
                "dodge",
                "ford",
                "honda",
                "hyundai",
                "jeep",
                "land rover",
                "lincoln",
                "mercury",
                "nissan",
                "pontiac",
                "subaru",
                "toyota",
                "volkswagen",
            ),
            "trans": (
                "auto(av)",
                "auto(l3)",
                "auto(l4)",
                "auto(l5)",
                "auto(l6)",
                "auto(s4)",
                "auto(s5)",
                "auto(s6)",
                "manual(m5)",
                "manual(m6)",
            ),
            "drv": ("4", "f", "r"),
            "fl": ("c", "d", "e", "p", "r"),
            "class": ("2seater", "compact", "midsize", "minivan", "pickup", "subcompact", "suv"),
        },
    ),
    "galton_heights": Table(
        name="galton_heights",
        label="Galton's Heights",
        title="898 adult children measured with their parents",
        licence="GPL-2 or later",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/mosaicData/Galton.html",
        classes=(),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/mosaicData/Galton.csv",
        header=True,
        text_size=4,
        fields=_GALTON_HEIGHTS_FIELDS,
        codes={
            "sex": ("F", "M"),
        },
    ),
    "gapminder": Table(
        name="gapminder",
        label="Gapminder",
        title="1,704 country-years of life expectancy, population and income",
        licence="CC0",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/gapminder/gapminder.html",
        classes=(),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/gapminder/gapminder.csv",
        header=True,
        text_size=24,
        fields=_GAPMINDER_FIELDS,
        codes={
            "continent": ("Africa", "Americas", "Asia", "Europe", "Oceania"),
        },
    ),
    "gestation_births": Table(
        name="gestation_births",
        label="Child Health and Development Births",
        title="1,236 births in the Child Health and Development Studies",
        licence="GPL-2 or later",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/mosaicData/Gestation.html",
        classes=(),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/mosaicData/Gestation.csv",
        header=True,
        dates="%Y-%m-%d",
        fields=_GESTATION_BIRTHS_FIELDS,
        codes={
            "plurality": ("single fetus",),
            "outcome": ("live birth",),
            "sex": ("male",),
            "race": ("asian", "black", "mex", "mixed", "white"),
            "ed": (
                "8th -12th grade - did not graduate",
                "College graduate",
                "HS graduate--no other schooling",
                "HS+some college",
                "HS+trade",
                "Trade school HS unclear",
                "less than 8th grade",
            ),
            "drace": ("asian", "black", "mex", "white"),
            "marital": ("divorced", "legally separated", "married", "never married"),
            "inc": (
                "0-2500",
                "10000-12500",
                "12500-15000",
                "15000+",
                "15000-17500",
                "17500-20000",
                "20000-22500",
                "2500-5000",
                "5000-7500",
                "7500-10000",
            ),
            "smoke": ("never", "now", "once did, not now", "until current pregnancy"),
            "time_code": (
                "1 to 2 years ago",
                "10+ years ago",
                "2 to 3 years ago",
                "3 to 4 years ago",
                "5 to 9 years ago",
                "don't know",
                "during current preg",
                "never smoked",
                "still smokes",
                "within 1 yr",
            ),
            "number": (
                "1-4 per day",
                "10-14 per day",
                "15-19 per day",
                "20-29 per day",
                "30-39 per day",
                "40-60 per day",
                "5-9 per day",
                "60+ per day",
                "never",
            ),
        },
    ),
    "granulomatous_disease": Table(
        name="granulomatous_disease",
        label="Chronic Granulomatous Disease",
        title="203 infection records from a trial of gamma interferon",
        licence="LGPL-2 or later",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/survival/cgd.html",
        classes=("placebo", "interferon"),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/survival/cgd.csv",
        header=True,
        dates="%Y-%m-%d",
        fields=_GRANULOMATOUS_DISEASE_FIELDS,
        labels={"placebo": 0, "rIFN-g": 1},
        codes={
            "center": (
                "Amsterdam",
                "Copenhagen",
                "Harvard Medical Sch",
                "L.A. Children's Hosp",
                "Mott Children's Hosp",
                "Mt. Sinai Medical Ctr",
                "NIH",
                "Scripps Institute",
                "Texas Children's Hosp",
                "Univ. of Minnesota",
                "Univ. of Utah",
                "Univ. of Washington",
                "Univ. of Zurich",
            ),
            "sex": ("female", "male"),
            "inherit": ("X-linked", "autosomal"),
            "hos_cat": ("Europe:Amsterdam", "Europe:other", "US:NIH", "US:other"),
        },
    ),
    "greenhouse_gases": Table(
        name="greenhouse_gases",
        label="Greenhouse Gas Concentrations",
        title="300 ice-core readings of three greenhouse gases over two thousand years",
        licence="Artistic-2.0",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/dslabs/greenhouse_gases.html",
        classes=("carbon_dioxide", "methane", "nitrous_oxide"),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/dslabs/greenhouse_gases.csv",
        header=True,
        fields=_GREENHOUSE_GASES_FIELDS,
        labels={"CO2": 0, "CH4": 1, "N2O": 2},
    ),
    "grouse_ticks": Table(
        name="grouse_ticks",
        label="Ticks on Red Grouse Chicks",
        title="403 grouse chicks and how many ticks each carried",
        licence="GPL-2 or later",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/lme4/grouseticks.html",
        classes=(),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/lme4/grouseticks.csv",
        header=True,
        fields=_GROUSE_TICKS_FIELDS,
    ),
    "guns_and_crime": Table(
        name="guns_and_crime",
        label="Guns and Crime",
        title="1,173 American state-years of crime rates and whether a carry law was in force",
        licence="GPL-2 or GPL-3",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/AER/Guns.html",
        classes=("no_law", "law"),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/AER/Guns.csv",
        header=True,
        text_size=20,
        fields=_GUNS_AND_CRIME_FIELDS,
        labels={"no": 0, "yes": 1},
    ),
    "hate_crimes": Table(
        name="hate_crimes",
        label="Hate Crimes and Income Inequality",
        title="51 American states and how many hate crimes each reported",
        licence="MIT",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/fivethirtyeight/hate_crimes.html",
        classes=(),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/fivethirtyeight/hate_crimes.csv",
        header=True,
        text_size=20,
        fields=_HATE_CRIMES_FIELDS,
    ),
    "help_study": Table(
        name="help_study",
        label="Health Evaluation and Linkage to Primary Care",
        title="453 adults leaving detoxification and which substance each used",
        licence="GPL-2 or later",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/mosaicData/HELPrct.html",
        classes=("alcohol", "cocaine", "heroin"),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/mosaicData/HELPrct.csv",
        header=True,
        fields=_HELP_STUDY_FIELDS,
        labels={"alcohol": 0, "cocaine": 1, "heroin": 2},
        codes={
            "anysub": ("no", "yes"),
            "sex": ("female", "male"),
            "homeless": ("homeless", "housed"),
            "racegrp": ("black", "hispanic", "other", "white"),
        },
    ),
    "high_school_and_beyond": Table(
        name="high_school_and_beyond",
        label="High School and Beyond",
        title="200 American schoolchildren and which programme each was in",
        licence="GPL-3",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/openintro/hsb2.html",
        classes=("academic", "general", "vocational"),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/openintro/hsb2.csv",
        header=True,
        fields=_HIGH_SCHOOL_AND_BEYOND_FIELDS,
        labels={"academic": 0, "general": 1, "vocational": 2},
        codes={
            "gender": ("female", "male"),
            "race": ("african american", "asian", "hispanic", "white"),
            "ses": ("high", "low", "middle"),
            "schtyp": ("private", "public"),
        },
    ),
    "historic_co2": Table(
        name="historic_co2",
        label="Atmospheric Carbon Dioxide",
        title="694 readings of atmospheric carbon dioxide over eight hundred thousand years",
        licence="Artistic-2.0",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/dslabs/historic_co2.html",
        classes=("ice_cores", "mauna_loa"),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/dslabs/historic_co2.csv",
        header=True,
        fields=_HISTORIC_CO2_FIELDS,
        labels={"Ice Cores": 0, "Mauna Loa": 1},
    ),
    "hpc_jobs": Table(
        name="hpc_jobs",
        label="High-Performance Computing Jobs",
        title="4,331 jobs run on a compute cluster and how long each took",
        licence="MIT",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/modeldata/hpc_data.html",
        classes=("very_fast", "fast", "moderate", "long"),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/modeldata/hpc_data.csv",
        header=True,
        fields=_HPC_JOBS_FIELDS,
        labels={"VF": 0, "F": 1, "M": 2, "L": 3},
        codes={
            "protocol": ("A", "C", "D", "E", "F", "G", "H", "I", "J", "K", "L", "M", "N", "O"),
            "day": ("Fri", "Mon", "Sat", "Sun", "Thu", "Tue", "Wed"),
        },
    ),
    "infant_mortality": Table(
        name="infant_mortality",
        label="Infant Mortality by Nation",
        title="105 nations around 1970 and how many infants each lost per thousand born",
        licence="GPL-2 or later",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/carData/Leinhardt.html",
        classes=(),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/carData/Leinhardt.csv",
        header=True,
        text_size=24,
        fields=_INFANT_MORTALITY_FIELDS,
        codes={
            "region": ("Africa", "Americas", "Asia", "Europe"),
            "oil": ("no", "yes"),
        },
    ),
    "infertility": Table(
        name="infertility",
        label="Infertility after Abortion",
        title="248 women in a matched study of infertility, cases and their controls",
        licence="GPL-2 or GPL-3",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/datasets/infert.html",
        classes=("control", "case"),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/datasets/infert.csv",
        header=True,
        fields=_INFERTILITY_FIELDS,
        labels={"0": 0, "1": 1},
        codes={
            "education": ("0-5yrs", "12+ yrs", "6-11yrs"),
        },
    ),
    "insect_sprays": Table(
        name="insect_sprays",
        label="Effectiveness of Insect Sprays",
        title="72 plots treated with six insecticides and how many insects survived",
        licence="GPL-2 or GPL-3",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/datasets/InsectSprays.html",
        classes=("a", "b", "c", "d", "e", "f"),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/datasets/InsectSprays.csv",
        header=True,
        fields=_INSECT_SPRAYS_FIELDS,
        labels={"A": 0, "B": 1, "C": 2, "D": 3, "E": 4, "F": 5},
    ),
    "italian_olive_oils": Table(
        name="italian_olive_oils",
        label="Italian Olive Oils",
        title="572 Italian olive oils and which part of the country each came from",
        licence="Artistic-2.0",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/dslabs/olive.html",
        classes=("northern_italy", "sardinia", "southern_italy"),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/dslabs/olive.csv",
        header=True,
        fields=_ITALIAN_OLIVE_OILS_FIELDS,
        labels={"Northern Italy": 0, "Sardinia": 1, "Southern Italy": 2},
        codes={
            "area": (
                "Calabria",
                "Coast-Sardinia",
                "East-Liguria",
                "Inland-Sardinia",
                "North-Apulia",
                "Sicily",
                "South-Apulia",
                "Umbria",
                "West-Liguria",
            ),
        },
    ),
    "lecture_ratings": Table(
        name="lecture_ratings",
        label="University Lecture Ratings",
        title="73,421 ratings students at ETH Zurich gave their lecturers",
        licence="GPL-2 or later",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/lme4/InstEval.html",
        classes=(),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/lme4/InstEval.csv",
        header=True,
        fields=_LECTURE_RATINGS_FIELDS,
    ),
    "lending_club": Table(
        name="lending_club",
        label="Lending Club Loans",
        title="9,857 personal loans and whether each went bad",
        licence="MIT",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/modeldata/lending_club.html",
        classes=("bad", "good"),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/modeldata/lending_club.csv",
        header=True,
        text_size=2,
        fields=_LENDING_CLUB_FIELDS,
        labels={"bad": 0, "good": 1},
        codes={
            "term": ("term_36", "term_60"),
            "verification_status": ("Not_Verified", "Source_Verified", "Verified"),
            "emp_length": (
                "emp_1",
                "emp_2",
                "emp_3",
                "emp_4",
                "emp_5",
                "emp_6",
                "emp_7",
                "emp_8",
                "emp_9",
                "emp_ge_10",
                "emp_lt_1",
                "emp_unk",
            ),
        },
    ),
    "leptograpsus_crabs": Table(
        name="leptograpsus_crabs",
        label="Leptograpsus Crabs",
        title="200 rock crabs measured five ways, in two colour forms",
        licence="GPL-2 or GPL-3",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/MASS/crabs.html",
        classes=("blue", "orange"),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/MASS/crabs.csv",
        header=True,
        fields=_LEPTOGRAPSUS_CRABS_FIELDS,
        labels={"B": 0, "O": 1},
        codes={
            "sex": ("F", "M"),
        },
    ),
    "life_cycle_savings": Table(
        name="life_cycle_savings",
        label="Intercountry Life-Cycle Savings",
        title="50 countries in the 1960s and how much of their income each saved",
        licence="GPL-2 or GPL-3",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/datasets/LifeCycleSavings.html",
        classes=(),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/datasets/LifeCycleSavings.csv",
        header=True,
        text_size=14,
        fields=_LIFE_CYCLE_SAVINGS_FIELDS,
    ),
    "liver_transplant_list": Table(
        name="liver_transplant_list",
        label="Liver Transplant Waiting List",
        title="815 people put on a liver transplant list and what became of each",
        licence="LGPL-2 or later",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/survival/transplant.html",
        classes=("waiting", "died", "transplanted", "withdrawn"),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/survival/transplant.csv",
        header=True,
        fields=_LIVER_TRANSPLANT_LIST_FIELDS,
        labels={"censored": 0, "death": 1, "ltx": 2, "withdraw": 3},
        codes={
            "sex": ("f", "m"),
            "abo": ("A", "AB", "B", "O"),
        },
    ),
    "loblolly_pines": Table(
        name="loblolly_pines",
        label="Growth of Loblolly Pines",
        title="84 measurements of loblolly pine seedlings and how tall each stood",
        licence="GPL-2 or GPL-3",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/datasets/Loblolly.html",
        classes=(),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/datasets/Loblolly.csv",
        header=True,
        fields=_LOBLOLLY_PINES_FIELDS,
    ),
    "low_birth_weight": Table(
        name="low_birth_weight",
        label="Low Infant Birth Weight",
        title="189 births at a Massachusetts hospital and how much each baby weighed",
        licence="GPL-2 or GPL-3",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/MASS/birthwt.html",
        classes=(),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/MASS/birthwt.csv",
        header=True,
        fields=_LOW_BIRTH_WEIGHT_FIELDS,
    ),
    "lung_cancer_survival": Table(
        name="lung_cancer_survival",
        label="NCCTG Lung Cancer",
        title="228 patients with advanced lung cancer and how long each lived",
        licence="LGPL-2 or later",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/survival/cancer.html",
        classes=(),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/survival/cancer.csv",
        header=True,
        fields=_LUNG_CANCER_SURVIVAL_FIELDS,
    ),
    "mammal_sleep": Table(
        name="mammal_sleep",
        label="Mammal Sleep",
        title="83 mammals and how long each sleeps in a day",
        licence="MIT",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/ggplot2/msleep.html",
        classes=(),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/ggplot2/msleep.csv",
        header=True,
        text_size=30,
        fields=_MAMMAL_SLEEP_FIELDS,
        codes={
            "vore": ("carni", "herbi", "insecti", "omni"),
            "order": (
                "Afrosoricida",
                "Artiodactyla",
                "Carnivora",
                "Cetacea",
                "Chiroptera",
                "Cingulata",
                "Didelphimorphia",
                "Diprotodontia",
                "Erinaceomorpha",
                "Hyracoidea",
                "Lagomorpha",
                "Monotremata",
                "Perissodactyla",
                "Pilosa",
                "Primates",
                "Proboscidea",
                "Rodentia",
                "Scandentia",
                "Soricomorpha",
            ),
            "conservation": ("cd", "domesticated", "en", "lc", "nt", "vu"),
        },
    ),
    "marijuana_arrests": Table(
        name="marijuana_arrests",
        label="Toronto Marijuana Arrests",
        title="5,226 people arrested in Toronto and whether each was released with a summons",
        licence="GPL-2 or later",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/carData/Arrests.html",
        classes=("held", "released"),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/carData/Arrests.csv",
        header=True,
        fields=_MARIJUANA_ARRESTS_FIELDS,
        labels={"No": 0, "Yes": 1},
        codes={
            "colour": ("Black", "White"),
            "sex": ("Female", "Male"),
            "employed": ("No", "Yes"),
        },
    ),
    "marriage_licences": Table(
        name="marriage_licences",
        label="Mobile County Marriage Licences",
        title="98 people named on marriage licences in Mobile County",
        licence="GPL-2 or later",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/mosaicData/Marriage.html",
        classes=("bride", "groom"),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/mosaicData/Marriage.csv",
        header=True,
        dates="%Y-%m-%d",
        text_size=9,
        fields=_MARRIAGE_LICENCES_FIELDS,
        labels={"Bride": 0, "Groom": 1},
        codes={
            "officialtitle": (
                "BISHOP",
                "CATHOLIC PRIEST",
                "CHIEF CLERK",
                "CIRCUIT JUDGE",
                "ELDER",
                "MARRIAGE OFFICIAL",
                "MINISTER",
                "PASTOR",
                "REVEREND",
            ),
            "race": ("American Indian", "Black", "Hispanic", "White"),
            "prevconc": ("Death", "Divorce"),
            "sign": (
                "Aquarius",
                "Aries",
                "Cancer",
                "Capricorn",
                "Gemini",
                "Leo",
                "Libra",
                "Pisces",
                "Saggitarius",
                "Scorpio",
                "Taurus",
                "Virgo",
            ),
        },
    ),
    "math_achievement": Table(
        name="math_achievement",
        label="Mathematics Achievement Scores",
        title="7,185 American schoolchildren and how each scored in mathematics",
        licence="GPL-2 or later",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/nlme/MathAchieve.html",
        classes=(),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/nlme/MathAchieve.csv",
        header=True,
        fields=_MATH_ACHIEVEMENT_FIELDS,
        codes={
            "minority": ("No", "Yes"),
            "sex": ("Female", "Male"),
        },
    ),
    "medical_care_demand": Table(
        name="medical_care_demand",
        label="Demand for Medical Care",
        title="4,406 elderly Americans in a 1987 survey and how often each saw a doctor",
        licence="GPL-2 or GPL-3",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/AER/NMES1988.html",
        classes=(),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/AER/NMES1988.csv",
        header=True,
        fields=_MEDICAL_CARE_DEMAND_FIELDS,
        codes={
            "health": ("average", "excellent", "poor"),
            "adl": ("limited", "normal"),
            "region": ("midwest", "northeast", "other", "west"),
            "afam": ("no", "yes"),
            "gender": ("female", "male"),
        },
    ),
    "mid_atlantic_wages": Table(
        name="mid_atlantic_wages",
        label="Mid-Atlantic Wages",
        title="3,000 men in the mid-Atlantic states and what each earned",
        licence="GPL-2",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/ISLR/Wage.html",
        classes=(),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/ISLR/Wage.csv",
        header=True,
        fields=_MID_ATLANTIC_WAGES_FIELDS,
        codes={
            "maritl": (
                "1. Never Married",
                "2. Married",
                "3. Widowed",
                "4. Divorced",
                "5. Separated",
            ),
            "race": ("1. White", "2. Black", "3. Asian", "4. Other"),
            "education": (
                "1. < HS Grad",
                "2. HS Grad",
                "3. Some College",
                "4. College Grad",
                "5. Advanced Degree",
            ),
            "region": ("2. Middle Atlantic",),
            "jobclass": ("1. Industrial", "2. Information"),
            "health": ("1. <=Good", "2. >=Very Good"),
            "health_ins": ("1. Yes", "2. No"),
        },
    ),
    "midwest_counties": Table(
        name="midwest_counties",
        label="Midwest Demographics",
        title="437 counties of the American midwest and how many in each were poor",
        licence="MIT",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/ggplot2/midwest.html",
        classes=(),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/ggplot2/midwest.csv",
        header=True,
        text_size=14,
        fields=_MIDWEST_COUNTIES_FIELDS,
        codes={
            "state": ("IL", "IN", "MI", "OH", "WI"),
            "category": (
                "AAR",
                "AAU",
                "AHR",
                "AHU",
                "ALR",
                "ALU",
                "HAR",
                "HAU",
                "HHR",
                "HHU",
                "HLR",
                "HLU",
                "LAR",
                "LAU",
                "LHR",
                "LHU",
            ),
        },
    ),
    "monoclonal_gammopathy": Table(
        name="monoclonal_gammopathy",
        label="Monoclonal Gammopathy",
        title="1,384 patients with monoclonal gammopathy followed to death",
        licence="LGPL-2 or later",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/survival/mgus2.html",
        classes=(),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/survival/mgus2.csv",
        header=True,
        fields=_MONOCLONAL_GAMMOPATHY_FIELDS,
        codes={
            "sex": ("F", "M"),
        },
    ),
    "mortgage_denial": Table(
        name="mortgage_denial",
        label="Boston Mortgage Applications",
        title="2,380 Boston mortgage applications and whether each was turned down",
        licence="GPL-2 or GPL-3",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/AER/HMDA.html",
        classes=("granted", "denied"),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/AER/HMDA.csv",
        header=True,
        fields=_MORTGAGE_DENIAL_FIELDS,
        labels={"no": 0, "yes": 1},
        codes={
            "phist": ("no", "yes"),
        },
    ),
    "motor_trend_cars": Table(
        name="motor_trend_cars",
        label="Motor Trend Car Road Tests",
        title="32 cars road-tested by Motor Trend in 1974 and what each did to the gallon",
        licence="GPL-2 or GPL-3",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/datasets/mtcars.html",
        classes=(),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/datasets/mtcars.csv",
        header=True,
        text_size=19,
        fields=_MOTOR_TREND_CARS_FIELDS,
    ),
    "movielens": Table(
        name="movielens",
        label="MovieLens Ratings",
        title="100,004 ratings people gave to films",
        licence="Artistic-2.0",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/dslabs/movielens.html",
        classes=(),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/dslabs/movielens.csv",
        header=True,
        text_size=152,
        fields=_MOVIELENS_FIELDS,
    ),
    "new_york_air": Table(
        name="new_york_air",
        label="New York Air Quality",
        title="153 days in New York in 1973 and the ozone measured on each",
        licence="GPL-2 or GPL-3",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/datasets/airquality.html",
        classes=(),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/datasets/airquality.csv",
        header=True,
        fields=_NEW_YORK_AIR_FIELDS,
    ),
    "nyc_flights": Table(
        name="nyc_flights",
        label="Flights out of New York in 2013",
        title="336,776 flights out of New York in 2013 and how late each arrived",
        licence="CC0",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/nycflights13/flights.html",
        classes=(),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/nycflights13/flights.csv",
        header=True,
        dates="%Y-%m-%dT%H:%M:%SZ",
        text_size=6,
        fields=_NYC_FLIGHTS_FIELDS,
        codes={
            "carrier": (
                "9E",
                "AA",
                "AS",
                "B6",
                "DL",
                "EV",
                "F9",
                "FL",
                "HA",
                "MQ",
                "OO",
                "UA",
                "US",
                "VX",
                "WN",
                "YV",
            ),
            "origin": ("EWR", "JFK", "LGA"),
        },
    ),
    "nyc_weather": Table(
        name="nyc_weather",
        label="Hourly Weather in New York",
        title="26,115 hours of weather at the three New York airports",
        licence="CC0",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/nycflights13/weather.html",
        classes=(),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/nycflights13/weather.csv",
        header=True,
        dates="%Y-%m-%dT%H:%M:%SZ",
        fields=_NYC_WEATHER_FIELDS,
        codes={
            "origin": ("EWR", "JFK", "LGA"),
        },
    ),
    "occupational_prestige": Table(
        name="occupational_prestige",
        label="Prestige of Canadian Occupations",
        title="102 Canadian occupations and how each was rated for prestige",
        licence="GPL-2 or later",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/carData/Prestige.html",
        classes=(),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/carData/Prestige.csv",
        header=True,
        text_size=25,
        fields=_OCCUPATIONAL_PRESTIGE_FIELDS,
        codes={
            "type": ("bc", "prof", "wc"),
        },
    ),
    "oesophageal_cancer": Table(
        name="oesophageal_cancer",
        label="Smoking, Alcohol and Oesophageal Cancer",
        title="88 groups of French men and how many in each had oesophageal cancer",
        licence="GPL-2 or GPL-3",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/datasets/esoph.html",
        classes=(),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/datasets/esoph.csv",
        header=True,
        fields=_OESOPHAGEAL_CANCER_FIELDS,
        codes={
            "agegp": ("25-34", "35-44", "45-54", "55-64", "65-74", "75+"),
            "alcgp": ("0-39g/day", "120+", "40-79", "80-119"),
            "tobgp": ("0-9g/day", "10-19", "20-29", "30+"),
        },
    ),
    "old_faithful": Table(
        name="old_faithful",
        label="Old Faithful Geyser",
        title="272 eruptions of Old Faithful and how long the wait before each",
        licence="GPL-2 or GPL-3",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/datasets/faithful.html",
        classes=(),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/datasets/faithful.csv",
        header=True,
        fields=_OLD_FAITHFUL_FIELDS,
    ),
    "orange_juice": Table(
        name="orange_juice",
        label="Orange Juice Purchases",
        title="1,070 shoppers and which of two orange juices each bought",
        licence="GPL-2",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/ISLR/OJ.html",
        classes=("citrus_hill", "minute_maid"),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/ISLR/OJ.csv",
        header=True,
        fields=_ORANGE_JUICE_FIELDS,
        labels={"CH": 0, "MM": 1},
        codes={
            "store7": ("No", "Yes"),
        },
    ),
    "orange_trees": Table(
        name="orange_trees",
        label="Growth of Orange Trees",
        title="35 measurements of orange trees and how far around each had grown",
        licence="GPL-2 or GPL-3",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/datasets/Orange.html",
        classes=(),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/datasets/Orange.csv",
        header=True,
        fields=_ORANGE_TREES_FIELDS,
    ),
    "orchard_sprays": Table(
        name="orchard_sprays",
        label="Potency of Orchard Sprays",
        title="64 cells of a Latin square and how far each spray put the bees off",
        licence="GPL-2 or GPL-3",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/datasets/OrchardSprays.html",
        classes=("a", "b", "c", "d", "e", "f", "g", "h"),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/datasets/OrchardSprays.csv",
        header=True,
        fields=_ORCHARD_SPRAYS_FIELDS,
        labels={"A": 0, "B": 1, "C": 2, "D": 3, "E": 4, "F": 5, "G": 6, "H": 7},
    ),
    "orthodontic_growth": Table(
        name="orthodontic_growth",
        label="Orthodontic Growth Curves",
        title="108 skull measurements of children followed through adolescence",
        licence="GPL-2 or later",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/nlme/Orthodont.html",
        classes=(),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/nlme/Orthodont.csv",
        header=True,
        text_size=3,
        fields=_ORTHODONTIC_GROWTH_FIELDS,
        codes={
            "sex": ("Female", "Male"),
        },
    ),
    "oxford_boys": Table(
        name="oxford_boys",
        label="Heights of Boys in Oxford",
        title="234 height measurements of twenty-six boys in Oxford",
        licence="GPL-2 or later",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/nlme/Oxboys.html",
        classes=(),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/nlme/Oxboys.csv",
        header=True,
        fields=_OXFORD_BOYS_FIELDS,
    ),
    "penicillin_testing": Table(
        name="penicillin_testing",
        label="Variation in Penicillin Testing",
        title="144 plates of a penicillin assay and how wide the clear zone grew",
        licence="GPL-2 or later",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/lme4/Penicillin.html",
        classes=(),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/lme4/Penicillin.csv",
        header=True,
        fields=_PENICILLIN_TESTING_FIELDS,
        codes={
            "plate": (
                "a",
                "b",
                "c",
                "d",
                "e",
                "f",
                "g",
                "h",
                "i",
                "j",
                "k",
                "l",
                "m",
                "n",
                "o",
                "p",
                "q",
                "r",
                "s",
                "t",
                "u",
                "v",
                "w",
                "x",
            ),
            "sample": ("A", "B", "C", "D", "E", "F"),
        },
    ),
    "petroleum_rock": Table(
        name="petroleum_rock",
        label="Measurements on Petroleum Rock",
        title="48 slices of reservoir rock and how well each let fluid through",
        licence="GPL-2 or GPL-3",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/datasets/rock.html",
        classes=(),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/datasets/rock.csv",
        header=True,
        fields=_PETROLEUM_ROCK_FIELDS,
    ),
    "phenobarbital": Table(
        name="phenobarbital",
        label="Phenobarbital in Newborn Infants",
        title="744 doses given and blood samples drawn from newborn infants",
        licence="GPL-2 or later",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/nlme/Phenobarb.html",
        classes=(),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/nlme/Phenobarb.csv",
        header=True,
        fields=_PHENOBARBITAL_FIELDS,
        codes={
            "apgarind": ("< 5", ">= 5"),
        },
    ),
    "pima_diabetes": Table(
        name="pima_diabetes",
        label="Diabetes in Pima Women",
        title="200 Pima women and whether each tested diabetic",
        licence="GPL-2 or GPL-3",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/MASS/Pima.tr.html",
        classes=("negative", "positive"),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/MASS/Pima.tr.csv",
        header=True,
        fields=_PIMA_DIABETES_FIELDS,
        labels={"No": 0, "Yes": 1},
    ),
    "professor_salaries": Table(
        name="professor_salaries",
        label="Salaries for Professors",
        title="397 American professors and what each was paid over 2008 and 2009",
        licence="GPL-2 or later",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/carData/Salaries.html",
        classes=(),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/carData/Salaries.csv",
        header=True,
        fields=_PROFESSOR_SALARIES_FIELDS,
        codes={
            "rank": ("AssocProf", "AsstProf", "Prof"),
            "discipline": ("A", "B"),
            "sex": ("Female", "Male"),
        },
    ),
    "psid_labour": Table(
        name="psid_labour",
        label="Labour Force Participation in the PSID",
        title="753 married women in the 1976 panel study and whether each worked for pay",
        licence="GPL-2 or GPL-3",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/AER/PSID1976.html",
        classes=("at_home", "working"),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/AER/PSID1976.csv",
        header=True,
        fields=_PSID_LABOUR_FIELDS,
        labels={"no": 0, "yes": 1},
        codes={
            "city": ("no", "yes"),
        },
    ),
    "reported_weight": Table(
        name="reported_weight",
        label="Self-Reports of Height and Weight",
        title="200 people who gave both their measured and their reported weight",
        licence="GPL-2 or later",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/carData/Davis.html",
        classes=("female", "male"),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/carData/Davis.csv",
        header=True,
        fields=_REPORTED_WEIGHT_FIELDS,
        labels={"F": 0, "M": 1},
    ),
    "resume_callbacks": Table(
        name="resume_callbacks",
        label="Which Resume Attributes Drive Callbacks",
        title="4,870 fictitious resumes sent to employers and which drew a call back",
        licence="GPL-3",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/openintro/resume.html",
        classes=("no_callback", "called_back"),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/openintro/resume.csv",
        header=True,
        text_size=8,
        fields=_RESUME_CALLBACKS_FIELDS,
        labels={"0": 0, "1": 1},
        codes={
            "job_city": ("Boston", "Chicago"),
            "job_industry": (
                "business_and_personal_service",
                "finance_insurance_real_estate",
                "manufacturing",
                "other_service",
                "transportation_communication",
                "wholesale_and_retail_trade",
            ),
            "job_type": (
                "clerical",
                "manager",
                "retail_sales",
                "sales_rep",
                "secretary",
                "supervisor",
            ),
            "job_ownership": ("nonprofit", "private", "public", "unknown"),
            "job_req_min_experience": (
                "0",
                "0.5",
                "1",
                "10",
                "2",
                "3",
                "4",
                "5",
                "6",
                "7",
                "8",
                "some",
            ),
            "job_req_school": ("college", "high_school_grad", "none_listed", "some_college"),
            "race": ("black", "white"),
            "gender": ("f", "m"),
            "resume_quality": ("high", "low"),
        },
    ),
    "retinopathy_laser": Table(
        name="retinopathy_laser",
        label="Laser Treatment for Diabetic Retinopathy",
        title="394 eyes treated with laser coagulation and how long each kept its sight",
        licence="LGPL-2 or later",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/survival/retinopathy.html",
        classes=("argon", "xenon"),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/survival/retinopathy.csv",
        header=True,
        fields=_RETINOPATHY_LASER_FIELDS,
        labels={"argon": 0, "xenon": 1},
        codes={
            "eye": ("left", "right"),
            "type": ("adult", "juvenile"),
        },
    ),
    "sat_and_gpa": Table(
        name="sat_and_gpa",
        label="SAT Scores and College Grades",
        title="1,000 students and the grade average each finished the first year with",
        licence="GPL-3",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/openintro/satgpa.html",
        classes=(),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/openintro/satgpa.csv",
        header=True,
        fields=_SAT_AND_GPA_FIELDS,
    ),
    "school_absences": Table(
        name="school_absences",
        label="Absences in Rural New South Wales",
        title="146 Australian schoolchildren and how many days each missed",
        licence="GPL-2 or GPL-3",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/MASS/quine.html",
        classes=(),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/MASS/quine.csv",
        header=True,
        fields=_SCHOOL_ABSENCES_FIELDS,
        codes={
            "eth": ("A", "N"),
            "sex": ("F", "M"),
            "age": ("F0", "F1", "F2", "F3"),
            "lrn": ("AL", "SL"),
        },
    ),
    "seat_belt_laws": Table(
        name="seat_belt_laws",
        label="Mandatory Seat Belt Laws",
        title="765 American state-years of road deaths and how belt wearing was enforced",
        licence="GPL-2 or GPL-3",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/AER/USSeatBelts.html",
        classes=("none", "primary", "secondary"),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/AER/USSeatBelts.csv",
        header=True,
        text_size=2,
        fields=_SEAT_BELT_LAWS_FIELDS,
        labels={"no": 0, "primary": 1, "secondary": 2},
        codes={
            "speed65": ("no", "yes"),
        },
    ),
    "seattle_pets": Table(
        name="seattle_pets",
        label="Names of Pets in Seattle",
        title="52,519 licensed pets in Seattle and what kind of animal each is",
        licence="GPL-3",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/openintro/seattlepets.html",
        classes=("cat", "dog", "goat", "pig"),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/openintro/seattlepets.csv",
        header=True,
        dates="%Y-%m-%d",
        text_size=55,
        fields=_SEATTLE_PETS_FIELDS,
        labels={"Cat": 0, "Dog": 1, "Goat": 2, "Pig": 3},
    ),
    "sleep_deprivation": Table(
        name="sleep_deprivation",
        label="Reaction Times under Sleep Deprivation",
        title="180 reaction times from eighteen drivers kept short of sleep",
        licence="GPL-2 or later",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/lme4/sleepstudy.html",
        classes=(),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/lme4/sleepstudy.csv",
        header=True,
        fields=_SLEEP_DEPRIVATION_FIELDS,
    ),
    "slid_wages": Table(
        name="slid_wages",
        label="Survey of Labour and Income Dynamics",
        title="7,425 Ontario workers and what each earned an hour",
        licence="GPL-2 or later",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/carData/SLID.html",
        classes=(),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/carData/SLID.csv",
        header=True,
        fields=_SLID_WAGES_FIELDS,
        codes={
            "sex": ("Female", "Male"),
            "language": ("English", "French", "Other"),
        },
    ),
    "snail_mortality": Table(
        name="snail_mortality",
        label="Snail Mortality",
        title="96 groups of snails held in a laboratory and how many died",
        licence="GPL-2 or GPL-3",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/MASS/snails.html",
        classes=(),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/MASS/snails.csv",
        header=True,
        fields=_SNAIL_MORTALITY_FIELDS,
        codes={
            "species": ("A", "B"),
        },
    ),
    "soybean_growth": Table(
        name="soybean_growth",
        label="Growth of Soybean Plants",
        title="412 weighings of soybean plants through a growing season",
        licence="GPL-2 or later",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/nlme/Soybean.html",
        classes=(),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/nlme/Soybean.csv",
        header=True,
        text_size=6,
        fields=_SOYBEAN_GROWTH_FIELDS,
        codes={
            "variety": ("F", "P"),
        },
    ),
    "sp500_daily": Table(
        name="sp500_daily",
        label="S&P 500 Daily Returns",
        title="1,250 trading days on the S&P 500 and whether the index rose",
        licence="GPL-2",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/ISLR/Smarket.html",
        classes=("down", "up"),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/ISLR/Smarket.csv",
        header=True,
        fields=_SP500_DAILY_FIELDS,
        labels={"Down": 0, "Up": 1},
    ),
    "sp500_weekly": Table(
        name="sp500_weekly",
        label="S&P 500 Weekly Returns",
        title="1,089 weeks on the S&P 500 and whether the index rose",
        licence="GPL-2",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/ISLR/Weekly.html",
        classes=("down", "up"),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/ISLR/Weekly.csv",
        header=True,
        fields=_SP500_WEEKLY_FIELDS,
        labels={"Down": 0, "Up": 1},
    ),
    "spruce_growth": Table(
        name="spruce_growth",
        label="Growth of Spruce Trees",
        title="1,027 measurements of spruce trees grown in ozone chambers",
        licence="GPL-2 or later",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/nlme/Spruce.html",
        classes=(),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/nlme/Spruce.csv",
        header=True,
        text_size=5,
        fields=_SPRUCE_GROWTH_FIELDS,
    ),
    "stanford_heart": Table(
        name="stanford_heart",
        label="Stanford Heart Transplants",
        title="172 follow-up records from the Stanford heart transplant programme",
        licence="LGPL-2 or later",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/survival/heart.html",
        classes=(),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/survival/heart.csv",
        header=True,
        fields=_STANFORD_HEART_FIELDS,
    ),
    "star_properties": Table(
        name="star_properties",
        label="Physical Properties of Stars",
        title="96 stars and how hot the surface of each burns",
        licence="Artistic-2.0",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/dslabs/stars.html",
        classes=(),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/dslabs/stars.csv",
        header=True,
        text_size=17,
        fields=_STAR_PROPERTIES_FIELDS,
        codes={
            "type": ("A", "B", "DA", "DB", "DF", "F", "G", "K", "M", "O"),
        },
    ),
    "state_sat_scores": Table(
        name="state_sat_scores",
        label="State by State SAT Data",
        title="50 American states and what each spent on a pupil",
        licence="GPL-2 or later",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/mosaicData/SAT.html",
        classes=(),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/mosaicData/SAT.csv",
        header=True,
        text_size=14,
        fields=_STATE_SAT_SCORES_FIELDS,
    ),
    "steak_preferences": Table(
        name="steak_preferences",
        label="How Americans Like Their Steak",
        title="550 Americans and how each likes a steak cooked",
        licence="MIT",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/fivethirtyeight/steak_survey.html",
        classes=("rare", "medium_rare", "medium", "medium_well", "well_done", "no_answer"),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/fivethirtyeight/steak_survey.csv",
        header=True,
        fields=_STEAK_PREFERENCES_FIELDS,
        labels={"Rare": 0, "Medium rare": 1, "Medium": 2, "Medium Well": 3, "Well": 4, "": 5},
        codes={
            "lottery_a": ("FALSE", "TRUE"),
            "age": ("18-29", "30-44", "45-60", "> 60"),
            "hhold_income": (
                "$0 - $24,999",
                "$100,000 - $149,999",
                "$150,000+",
                "$25,000 - $49,999",
                "$50,000 - $99,999",
            ),
            "educ": (
                "Bachelor degree",
                "Graduate degree",
                "High school degree",
                "Less than high school degree",
                "Some college or Associate degree",
            ),
            "region": (
                "East North Central",
                "East South Central",
                "Middle Atlantic",
                "Mountain",
                "New England",
                "Pacific",
                "South Atlantic",
                "West North Central",
                "West South Central",
            ),
        },
    ),
    "student_survey": Table(
        name="student_survey",
        label="Adelaide Student Survey",
        title="237 Australian statistics students and which hand each wrote with",
        licence="GPL-2 or GPL-3",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/MASS/survey.html",
        classes=("left", "right", "not_recorded"),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/MASS/survey.csv",
        header=True,
        fields=_STUDENT_SURVEY_FIELDS,
        labels={"Left": 0, "Right": 1, "": 2},
        codes={
            "sex": ("Female", "Male"),
            "fold": ("L on R", "Neither", "R on L"),
            "clap": ("Left", "Neither", "Right"),
            "exer": ("Freq", "None", "Some"),
            "smoke": ("Heavy", "Never", "Occas", "Regul"),
            "m_i": ("Imperial", "Metric"),
        },
    ),
    "swiss_fertility": Table(
        name="swiss_fertility",
        label="Swiss Fertility in 1888",
        title="47 French-speaking Swiss provinces in 1888 and how fertile each was",
        licence="GPL-2 or GPL-3",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/datasets/swiss.html",
        classes=(),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/datasets/swiss.csv",
        header=True,
        text_size=12,
        fields=_SWISS_FERTILITY_FIELDS,
    ),
    "swiss_labour": Table(
        name="swiss_labour",
        label="Swiss Labour Market Participation",
        title="872 Swiss women and whether each was in the labour force",
        licence="GPL-2 or GPL-3",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/AER/SwissLabor.html",
        classes=("at_home", "working"),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/AER/SwissLabor.csv",
        header=True,
        fields=_SWISS_LABOUR_FIELDS,
        labels={"no": 0, "yes": 1},
        codes={
            "foreign": ("no", "yes"),
        },
    ),
    "tarantino_scripts": Table(
        name="tarantino_scripts",
        label="Swearing and Death in Tarantino Films",
        title="1,894 curses and deaths counted through seven Tarantino films",
        licence="MIT",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/fivethirtyeight/tarantino.html",
        classes=(
            "reservoir_dogs",
            "pulp_fiction",
            "jackie_brown",
            "kill_bill_1",
            "kill_bill_2",
            "inglourious_basterds",
            "django_unchained",
        ),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/fivethirtyeight/tarantino.csv",
        header=True,
        text_size=13,
        fields=_TARANTINO_SCRIPTS_FIELDS,
        labels={
            "Reservoir Dogs": 0,
            "Pulp Fiction": 1,
            "Jackie Brown": 2,
            "Kill Bill: Vol. 1": 3,
            "Kill Bill: Vol. 2": 4,
            "Inglorious Basterds": 5,
            "Django Unchained": 6,
        },
        codes={
            "profane": ("FALSE", "TRUE"),
        },
    ),
    "teaching_evaluations": Table(
        name="teaching_evaluations",
        label="Professor Evaluations and Beauty",
        title="463 university courses and how the students rated the teacher",
        licence="GPL-3",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/openintro/evals.html",
        classes=(),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/openintro/evals.csv",
        header=True,
        fields=_TEACHING_EVALUATIONS_FIELDS,
        codes={
            "rank": ("teaching", "tenure track", "tenured"),
            "ethnicity": ("minority", "not minority"),
            "gender": ("female", "male"),
            "language": ("english", "non-english"),
            "cls_level": ("lower", "upper"),
            "cls_profs": ("multiple", "single"),
            "cls_credits": ("multi credit", "one credit"),
            "pic_outfit": ("formal", "not formal"),
            "pic_color": ("black&white", "color"),
        },
    ),
    "telecom_churn": Table(
        name="telecom_churn",
        label="Telecom Customer Churn",
        title="5,000 phone customers and whether each left",
        licence="MIT",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/modeldata/mlc_churn.html",
        classes=("stayed", "left"),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/modeldata/mlc_churn.csv",
        header=True,
        text_size=2,
        fields=_TELECOM_CHURN_FIELDS,
        labels={"no": 0, "yes": 1},
        codes={
            "area_code": ("area_code_408", "area_code_415", "area_code_510"),
            "international_plan": ("no", "yes"),
        },
    ),
    "telecom_contracts": Table(
        name="telecom_contracts",
        label="Telecom Contract Churn",
        title="7,043 telecom customers and whether each left",
        licence="MIT",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/modeldata/wa_churn.html",
        classes=("stayed", "left"),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/modeldata/wa_churn.csv",
        header=True,
        fields=_TELECOM_CONTRACTS_FIELDS,
        labels={"No": 0, "Yes": 1},
        codes={
            "multiple_lines": ("No", "No phone service", "Yes"),
            "internet_service": ("DSL", "Fiber optic", "No"),
            "online_security": ("No", "No internet service", "Yes"),
            "contract": ("Month-to-month", "One year", "Two year"),
            "payment_method": (
                "Bank transfer (automatic)",
                "Credit card (automatic)",
                "Electronic check",
                "Mailed check",
            ),
        },
    ),
    "temperature_and_carbon": Table(
        name="temperature_and_carbon",
        label="Global Temperature and Carbon Emissions",
        title="268 years of global temperature anomalies and the carbon burnt in each",
        licence="Artistic-2.0",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/dslabs/temp_carbon.html",
        classes=(),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/dslabs/temp_carbon.csv",
        header=True,
        fields=_TEMPERATURE_AND_CARBON_FIELDS,
    ),
    "ten_mile_race": Table(
        name="ten_mile_race",
        label="The Cherry Blossom Ten Mile Race",
        title="8,636 runners of the Cherry Blossom race and how long each took",
        licence="GPL-2 or later",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/mosaicData/TenMileRace.html",
        classes=(),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/mosaicData/TenMileRace.csv",
        header=True,
        text_size=11,
        fields=_TEN_MILE_RACE_FIELDS,
        codes={
            "sex": ("F", "M"),
        },
    ),
    "texas_housing": Table(
        name="texas_housing",
        label="Housing Sales in Texas",
        title="8,602 city-months of Texas house sales and the median price in each",
        licence="MIT",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/ggplot2/txhousing.html",
        classes=(),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/ggplot2/txhousing.csv",
        header=True,
        text_size=21,
        fields=_TEXAS_HOUSING_FIELDS,
    ),
    "theophylline": Table(
        name="theophylline",
        label="Pharmacokinetics of Theophylline",
        title="132 blood samples from twelve subjects given theophylline",
        licence="GPL-2 or GPL-3",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/datasets/Theoph.html",
        classes=(),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/datasets/Theoph.csv",
        header=True,
        fields=_THEOPHYLLINE_FIELDS,
    ),
    "titanic": Table(
        name="titanic",
        label="Survival on the Titanic",
        title="1,309 people aboard the Titanic and which of them lived",
        licence="GPL-2 or later",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/carData/TitanicSurvival.html",
        classes=("died", "survived"),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/carData/TitanicSurvival.csv",
        header=True,
        text_size=31,
        fields=_TITANIC_FIELDS,
        labels={"no": 0, "yes": 1},
        codes={
            "sex": ("female", "male"),
            "passengerclass": ("1st", "2nd", "3rd"),
        },
    ),
    "tooth_growth": Table(
        name="tooth_growth",
        label="Vitamin C and Tooth Growth",
        title="60 guinea pigs given vitamin C two ways and how far their teeth grew",
        licence="GPL-2 or GPL-3",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/datasets/ToothGrowth.html",
        classes=("orange_juice", "ascorbic_acid"),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/datasets/ToothGrowth.csv",
        header=True,
        fields=_TOOTH_GROWTH_FIELDS,
        labels={"OJ": 0, "VC": 1},
    ),
    "travel_mode": Table(
        name="travel_mode",
        label="Intercity Travel Mode Choice",
        title="840 rows of an Australian trip survey, one a way the traveller could have gone",
        licence="GPL-2 or GPL-3",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/AER/TravelMode.html",
        classes=("air", "bus", "car", "train"),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/AER/TravelMode.csv",
        header=True,
        fields=_TRAVEL_MODE_FIELDS,
        labels={"air": 0, "bus": 1, "car": 2, "train": 3},
        codes={
            "choice": ("no", "yes"),
        },
    ),
    "uk_smoking": Table(
        name="uk_smoking",
        label="UK Smoking Survey",
        title="1,691 British adults and whether each smoked",
        licence="GPL-3",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/openintro/smoking.html",
        classes=("non_smoker", "smoker"),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/openintro/smoking.csv",
        header=True,
        fields=_UK_SMOKING_FIELDS,
        labels={"No": 0, "Yes": 1},
        codes={
            "gender": ("Female", "Male"),
            "marital_status": ("Divorced", "Married", "Separated", "Single", "Widowed"),
            "highest_qualification": (
                "A Levels",
                "Degree",
                "GCSE/CSE",
                "GCSE/O Level",
                "Higher/Sub Degree",
                "No Qualification",
                "ONC/BTEC",
                "Other/Sub Degree",
            ),
            "nationality": (
                "British",
                "English",
                "Irish",
                "Other",
                "Refused",
                "Scottish",
                "Unknown",
                "Welsh",
            ),
            "ethnicity": ("Asian", "Black", "Chinese", "Mixed", "Refused", "Unknown", "White"),
            "gross_income": (
                "10,400 to 15,600",
                "15,600 to 20,800",
                "2,600 to 5,200",
                "20,800 to 28,600",
                "28,600 to 36,400",
                "5,200 to 10,400",
                "Above 36,400",
                "Refused",
                "Under 2,600",
                "Unknown",
            ),
            "region": (
                "London",
                "Midlands & East Anglia",
                "Scotland",
                "South East",
                "South West",
                "The North",
                "Wales",
            ),
            "type": ("Both/Mainly Hand-Rolled", "Both/Mainly Packets", "Hand-Rolled", "Packets"),
        },
    ),
    "un_national_statistics": Table(
        name="un_national_statistics",
        label="National Statistics from the United Nations",
        title="213 countries and how long a woman born in each could expect to live",
        licence="GPL-2 or later",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/carData/UN.html",
        classes=(),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/carData/UN.csv",
        header=True,
        text_size=32,
        fields=_UN_NATIONAL_STATISTICS_FIELDS,
        codes={
            "region": (
                "Africa",
                "Asia",
                "Caribbean",
                "Europe",
                "Latin Amer",
                "North America",
                "NorthAtlantic",
                "Oceania",
            ),
            "group": ("africa", "oecd", "other"),
        },
    ),
    "us_aircraft": Table(
        name="us_aircraft",
        label="Aircraft Registrations",
        title="3,322 aircraft that flew out of New York and how many seats each had",
        licence="CC0",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/nycflights13/planes.html",
        classes=(),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/nycflights13/planes.csv",
        header=True,
        text_size=29,
        fields=_US_AIRCRAFT_FIELDS,
        codes={
            "type": ("Fixed wing multi engine", "Fixed wing single engine", "Rotorcraft"),
            "engine": (
                "4 Cycle",
                "Reciprocating",
                "Turbo-fan",
                "Turbo-jet",
                "Turbo-prop",
                "Turbo-shaft",
            ),
        },
    ),
    "us_airports": Table(
        name="us_airports",
        label="US Airports",
        title="1,458 American airports and how high above the sea each stands",
        licence="CC0",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/nycflights13/airports.html",
        classes=(),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/nycflights13/airports.csv",
        header=True,
        text_size=51,
        fields=_US_AIRPORTS_FIELDS,
        codes={
            "dst": ("A", "N", "U"),
            "tzone": (
                "America/Anchorage",
                "America/Chicago",
                "America/Denver",
                "America/Los_Angeles",
                "America/New_York",
                "America/Phoenix",
                "America/Vancouver",
                "Asia/Chongqing",
                "Pacific/Honolulu",
            ),
        },
    ),
    "us_arrests": Table(
        name="us_arrests",
        label="Violent Crime Rates by US State",
        title="50 American states and how many were murdered in each per hundred thousand",
        licence="GPL-2 or GPL-3",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/datasets/USArrests.html",
        classes=(),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/datasets/USArrests.csv",
        header=True,
        text_size=14,
        fields=_US_ARRESTS_FIELDS,
    ),
    "us_births_1978": Table(
        name="us_births_1978",
        label="US Births in 1978",
        title="365 days of 1978 and how many Americans were born on each",
        licence="GPL-2 or later",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/mosaicData/Births78.html",
        classes=(),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/mosaicData/Births78.csv",
        header=True,
        dates="%Y-%m-%d",
        fields=_US_BIRTHS_1978_FIELDS,
        codes={
            "wday": ("Fri", "Mon", "Sat", "Sun", "Thu", "Tue", "Wed"),
        },
    ),
    "us_births_2014": Table(
        name="us_births_2014",
        label="US Births in 2014",
        title="1,000 American births in 2014 and how much each baby weighed",
        licence="GPL-3",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/openintro/births14.html",
        classes=(),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/openintro/births14.csv",
        header=True,
        fields=_US_BIRTHS_2014_FIELDS,
        codes={
            "mature": ("mature mom", "younger mom"),
            "premie": ("full term", "premie"),
            "lowbirthweight": ("low", "not low"),
            "sex": ("female", "male"),
            "habit": ("nonsmoker", "smoker"),
            "marital": ("married", "not married"),
            "whitemom": ("not white", "white"),
        },
    ),
    "us_cereals": Table(
        name="us_cereals",
        label="US Breakfast Cereals",
        title="65 American breakfast cereals and which company made each",
        licence="GPL-2 or GPL-3",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/MASS/UScereal.html",
        classes=("general_mills", "kelloggs", "nabisco", "post", "quaker", "ralston"),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/MASS/UScereal.csv",
        header=True,
        text_size=37,
        fields=_US_CEREALS_FIELDS,
        labels={"G": 0, "K": 1, "N": 2, "P": 3, "Q": 4, "R": 5},
        codes={
            "vitamins": ("100%", "enriched", "none"),
        },
    ),
    "us_colleges": Table(
        name="us_colleges",
        label="US News College Data",
        title="777 American colleges and whether each was private",
        licence="GPL-2",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/ISLR/College.html",
        classes=("public", "private"),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/ISLR/College.csv",
        header=True,
        text_size=45,
        fields=_US_COLLEGES_FIELDS,
        labels={"No": 0, "Yes": 1},
    ),
    "us_economics": Table(
        name="us_economics",
        label="US Economic Time Series",
        title="574 months of American spending, saving and unemployment",
        licence="MIT",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/ggplot2/economics.html",
        classes=(),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/ggplot2/economics.csv",
        header=True,
        dates="%Y-%m-%d",
        fields=_US_ECONOMICS_FIELDS,
    ),
    "us_gun_murders": Table(
        name="us_gun_murders",
        label="US Gun Murders in 2010",
        title="51 American states and how many gun murders each saw in 2010",
        licence="Artistic-2.0",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/dslabs/murders.html",
        classes=(),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/dslabs/murders.csv",
        header=True,
        text_size=20,
        fields=_US_GUN_MURDERS_FIELDS,
        codes={
            "region": ("North Central", "Northeast", "South", "West"),
        },
    ),
    "us_state_education": Table(
        name="us_state_education",
        label="Education by US State",
        title="51 American states and how their students scored on the SAT",
        licence="GPL-2 or later",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/carData/States.html",
        classes=(),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/carData/States.csv",
        header=True,
        text_size=2,
        fields=_US_STATE_EDUCATION_FIELDS,
        codes={
            "region": ("ENC", "ESC", "MA", "MTN", "NE", "PAC", "SA", "WNC", "WSC"),
        },
    ),
    "used_car_prices": Table(
        name="used_car_prices",
        label="Kelley Blue Book Car Prices",
        title="804 used cars from the 2005 model year and what each was worth",
        licence="MIT",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/modeldata/car_prices.html",
        classes=(),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/modeldata/car_prices.csv",
        header=True,
        fields=_USED_CAR_PRICES_FIELDS,
    ),
    "utility_bills": Table(
        name="utility_bills",
        label="Utility Bills",
        title="117 monthly gas and electricity bills for one house",
        licence="GPL-2 or later",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/mosaicData/Utilities.html",
        classes=(),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/mosaicData/Utilities.csv",
        header=True,
        text_size=58,
        fields=_UTILITY_BILLS_FIELDS,
    ),
    "verbal_aggression": Table(
        name="verbal_aggression",
        label="Verbal Aggression Item Responses",
        title="7,584 answers to a questionnaire about wanting to curse, scold or shout",
        licence="GPL-2 or later",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/lme4/VerbAgg.html",
        classes=("no", "perhaps", "yes"),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/lme4/VerbAgg.csv",
        header=True,
        fields=_VERBAL_AGGRESSION_FIELDS,
        labels={"no": 0, "perhaps": 1, "yes": 2},
        codes={
            "gender": ("F", "M"),
            "item": (
                "S1DoCurse",
                "S1DoScold",
                "S1DoShout",
                "S1WantCurse",
                "S1WantScold",
                "S1WantShout",
                "S2DoCurse",
                "S2DoScold",
                "S2DoShout",
                "S2WantCurse",
                "S2WantScold",
                "S2WantShout",
                "S3DoCurse",
                "S3DoScold",
                "S3DoShout",
                "S3WantCurse",
                "S3WantScold",
                "S3WantShout",
                "S4DoCurse",
                "S4DoScold",
                "S4DoShout",
                "S4WantScold",
                "S4WantShout",
                "S4wantCurse",
            ),
            "btype": ("curse", "scold", "shout"),
            "situ": ("other", "self"),
            "mode": ("do", "want"),
            "r2": ("N", "Y"),
        },
    ),
    "vocabulary_test": Table(
        name="vocabulary_test",
        label="Vocabulary and Education",
        title="30,351 Americans given a ten-word vocabulary test and how many each knew",
        licence="GPL-2 or later",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/carData/Vocab.html",
        classes=(),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/carData/Vocab.csv",
        header=True,
        fields=_VOCABULARY_TEST_FIELDS,
        codes={
            "sex": ("Female", "Male"),
        },
    ),
    "volunteering": Table(
        name="volunteering",
        label="Volunteering for Psychological Research",
        title="1,421 people scored on two personality scales and whether each volunteered",
        licence="GPL-2 or later",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/carData/Cowles.html",
        classes=("declined", "volunteered"),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/carData/Cowles.csv",
        header=True,
        fields=_VOLUNTEERING_FIELDS,
        labels={"no": 0, "yes": 1},
        codes={
            "sex": ("female", "male"),
        },
    ),
    "warp_breaks": Table(
        name="warp_breaks",
        label="Breaks in Yarn during Weaving",
        title="54 looms of wool and how often the yarn broke on each",
        licence="GPL-2 or GPL-3",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/datasets/warpbreaks.html",
        classes=(),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/datasets/warpbreaks.csv",
        header=True,
        fields=_WARP_BREAKS_FIELDS,
        codes={
            "wool": ("A", "B"),
            "tension": ("H", "L", "M"),
        },
    ),
    "wheat_yield_trials": Table(
        name="wheat_yield_trials",
        label="Wheat Yield Trials",
        title="224 plots of a wheat variety trial and what each yielded",
        licence="GPL-2 or later",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/nlme/Wheat2.html",
        classes=(),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/nlme/Wheat2.csv",
        header=True,
        text_size=10,
        fields=_WHEAT_YIELD_TRIALS_FIELDS,
    ),
    "whickham_smoking": Table(
        name="whickham_smoking",
        label="The Whickham Smoking Survey",
        title="1,314 women followed for twenty years and whether each was still alive",
        licence="GPL-2 or later",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/mosaicData/Whickham.html",
        classes=("alive", "dead"),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/mosaicData/Whickham.csv",
        header=True,
        fields=_WHICKHAM_SMOKING_FIELDS,
        labels={"Alive": 0, "Dead": 1},
        codes={
            "smoker": ("No", "Yes"),
        },
    ),
    "windsor_house_prices": Table(
        name="windsor_house_prices",
        label="House Prices in Windsor",
        title="546 houses sold in Windsor, Ontario and what each fetched",
        licence="GPL-2 or GPL-3",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/AER/HousePrices.html",
        classes=(),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/AER/HousePrices.csv",
        header=True,
        fields=_WINDSOR_HOUSE_PRICES_FIELDS,
        codes={
            "driveway": ("no", "yes"),
        },
    ),
    "womens_labour_1975": Table(
        name="womens_labour_1975",
        label="US Women's Labour-Force Participation",
        title="753 married women in 1975 and whether each worked for pay",
        licence="GPL-2 or later",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/carData/Mroz.html",
        classes=("at_home", "working"),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/carData/Mroz.csv",
        header=True,
        fields=_WOMENS_LABOUR_1975_FIELDS,
        labels={"no": 0, "yes": 1},
        codes={
            "wc": ("no", "yes"),
        },
    ),
    "workplace_smoking_ban": Table(
        name="workplace_smoking_ban",
        label="Workplace Smoking Bans",
        title="10,000 American workers and whether each smoked",
        licence="GPL-2 or GPL-3",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/AER/SmokeBan.html",
        classes=("non_smoker", "smoker"),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/AER/SmokeBan.csv",
        header=True,
        fields=_WORKPLACE_SMOKING_BAN_FIELDS,
        labels={"no": 0, "yes": 1},
        codes={
            "ban": ("no", "yes"),
            "education": ("college", "hs", "hs drop out", "master", "some college"),
            "gender": ("female", "male"),
        },
    ),
    "world_values_survey": Table(
        name="world_values_survey",
        label="World Values Surveys",
        title="5,381 people in four countries and what each thought the state owed the poor",
        licence="GPL-2 or later",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/carData/WVS.html",
        classes=("too_little", "about_right", "too_much"),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/carData/WVS.csv",
        header=True,
        fields=_WORLD_VALUES_SURVEY_FIELDS,
        labels={"Too Little": 0, "About Right": 1, "Too Much": 2},
        codes={
            "religion": ("no", "yes"),
            "country": ("Australia", "Norway", "Sweden", "USA"),
            "gender": ("female", "male"),
        },
    ),
    "youth_risk_behaviour": Table(
        name="youth_risk_behaviour",
        label="Youth Risk Behavior Surveillance",
        title="13,583 American schoolchildren and how much each weighed",
        licence="GPL-3",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/openintro/yrbss.html",
        classes=(),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/openintro/yrbss.csv",
        header=True,
        text_size=41,
        fields=_YOUTH_RISK_BEHAVIOUR_FIELDS,
        codes={
            "gender": ("female", "male"),
            "grade": ("10", "11", "12", "9", "other"),
            "hispanic": ("hispanic", "not"),
            "helmet_12m": (
                "always",
                "did not ride",
                "most of time",
                "never",
                "rarely",
                "sometimes",
            ),
            "text_while_driving_30d": (
                "0",
                "1-2",
                "10-19",
                "20-29",
                "3-5",
                "30",
                "6-9",
                "did not drive",
            ),
            "hours_tv_per_school_day": ("1", "2", "3", "4", "5+", "<1", "do not watch"),
            "school_night_hours_sleep": ("10+", "5", "6", "7", "8", "9", "<5"),
        },
    ),
    "gdp_per_capita_worldbank": Table(
        name="gdp_per_capita_worldbank",
        label="GDP per Capita (World Bank)",
        title="7,445 country-years of output per person, in 2021 international dollars",
        licence="CC BY 4.0",
        source="https://ourworldindata.org/grapher/gdp-per-capita-worldbank",
        classes=(),
        url="https://ourworldindata.org/grapher/gdp-per-capita-worldbank.csv?v=1&csvType=full&useColumnShortNames=true",
        header=True,
        text_size=56,
        fields=_GDP_PER_CAPITA_WORLDBANK_FIELDS,
    ),
    "electricity_access": Table(
        name="electricity_access",
        label="Access to Electricity",
        title="7,140 country-years and the share of people with electricity at home",
        licence="CC BY 4.0",
        source="https://ourworldindata.org/grapher/share-of-the-population-with-access-to-electricity",
        classes=(),
        url="https://ourworldindata.org/grapher/share-of-the-population-with-access-to-electricity.csv?v=1&csvType=full&useColumnShortNames=true",
        header=True,
        text_size=56,
        fields=_ELECTRICITY_ACCESS_FIELDS,
    ),
    "internet_use": Table(
        name="internet_use",
        label="Internet Use",
        title="6,476 country-years and the share of people using the internet",
        licence="CC BY 4.0",
        source="https://ourworldindata.org/grapher/share-of-individuals-using-the-internet",
        classes=(),
        url="https://ourworldindata.org/grapher/share-of-individuals-using-the-internet.csv?v=1&csvType=full&useColumnShortNames=true",
        header=True,
        text_size=56,
        fields=_INTERNET_USE_FIELDS,
    ),
    "consumer_price_inflation": Table(
        name="consumer_price_inflation",
        label="Consumer Price Inflation",
        title="9,795 country-years and how fast consumer prices rose in each",
        licence="CC BY 4.0",
        source="https://ourworldindata.org/grapher/inflation-of-consumer-prices",
        classes=(),
        url="https://ourworldindata.org/grapher/inflation-of-consumer-prices.csv?v=1&csvType=full&useColumnShortNames=true",
        header=True,
        text_size=56,
        fields=_CONSUMER_PRICE_INFLATION_FIELDS,
    ),
    "unemployment_rate": Table(
        name="unemployment_rate",
        label="Unemployment Rate",
        title="6,986 country-years and the share of the workforce out of work",
        licence="CC BY 4.0",
        source="https://ourworldindata.org/grapher/unemployment-rate",
        classes=(),
        url="https://ourworldindata.org/grapher/unemployment-rate.csv?v=1&csvType=full&useColumnShortNames=true",
        header=True,
        text_size=56,
        fields=_UNEMPLOYMENT_RATE_FIELDS,
    ),
    "freshwater_withdrawals": Table(
        name="freshwater_withdrawals",
        label="Freshwater Withdrawals",
        title="6,401 country-years and how much fresh water each drew, in cubic kilometres",
        licence="CC BY 4.0",
        source="https://ourworldindata.org/grapher/annual-freshwater-withdrawals",
        classes=(),
        url="https://ourworldindata.org/grapher/annual-freshwater-withdrawals.csv?v=1&csvType=full&useColumnShortNames=true",
        header=True,
        text_size=56,
        fields=_FRESHWATER_WITHDRAWALS_FIELDS,
    ),
    "air_passengers": Table(
        name="air_passengers",
        label="Air Passengers Carried",
        title="8,593 country-years and how many air passengers each carried",
        licence="CC BY 4.0",
        source="https://ourworldindata.org/grapher/air-passengers-carried",
        classes=(),
        url="https://ourworldindata.org/grapher/air-passengers-carried.csv?v=1&csvType=full&useColumnShortNames=true",
        header=True,
        text_size=56,
        fields=_AIR_PASSENGERS_FIELDS,
    ),
    "mobile_subscriptions": Table(
        name="mobile_subscriptions",
        label="Mobile Phone Subscriptions",
        title="9,521 country-years and how many mobile subscriptions each had per hundred people",
        licence="CC BY 4.0",
        source="https://ourworldindata.org/grapher/mobile-cellular-subscriptions-per-100-people",
        classes=(),
        url="https://ourworldindata.org/grapher/mobile-cellular-subscriptions-per-100-people.csv?v=1&csvType=full&useColumnShortNames=true",
        header=True,
        text_size=56,
        fields=_MOBILE_SUBSCRIPTIONS_FIELDS,
    ),
    "internet_users": Table(
        name="internet_users",
        label="Internet Users",
        title="6,006 country-years and how many people used the internet in each",
        licence="CC BY 4.0",
        source="https://ourworldindata.org/grapher/number-of-internet-users",
        classes=(),
        url="https://ourworldindata.org/grapher/number-of-internet-users.csv?v=1&csvType=full&useColumnShortNames=true",
        header=True,
        text_size=64,
        fields=_INTERNET_USERS_FIELDS,
    ),
    "co2_emissions": Table(
        name="co2_emissions",
        label="Annual CO2 Emissions",
        title="29,384 country-years of carbon dioxide emitted, in tonnes",
        licence="CC BY 4.0",
        source="https://ourworldindata.org/grapher/annual-co2-emissions-per-country",
        classes=(),
        url="https://ourworldindata.org/grapher/annual-co2-emissions-per-country.csv?v=1&csvType=full&useColumnShortNames=true",
        header=True,
        text_size=32,
        fields=_CO2_EMISSIONS_FIELDS,
    ),
    "co2_emissions_per_person": Table(
        name="co2_emissions_per_person",
        label="CO2 Emissions per Person",
        title="26,509 country-years of carbon dioxide emitted for every person living there",
        licence="CC BY 4.0",
        source="https://ourworldindata.org/grapher/co-emissions-per-capita",
        classes=(),
        url="https://ourworldindata.org/grapher/co-emissions-per-capita.csv?v=1&csvType=full&useColumnShortNames=true",
        header=True,
        text_size=32,
        fields=_CO2_EMISSIONS_PER_PERSON_FIELDS,
    ),
    "cumulative_co2_emissions": Table(
        name="cumulative_co2_emissions",
        label="Cumulative CO2 Emissions",
        title="27,563 country-years of carbon dioxide emitted since 1750, in tonnes",
        licence="CC BY 4.0",
        source="https://ourworldindata.org/grapher/cumulative-co2-emissions",
        classes=(),
        url="https://ourworldindata.org/grapher/cumulative-co2-emissions.csv?v=1&csvType=full&useColumnShortNames=true",
        header=True,
        text_size=32,
        fields=_CUMULATIVE_CO2_EMISSIONS_FIELDS,
    ),
    "co2_per_dollar": Table(
        name="co2_per_dollar",
        label="Carbon Intensity of Output",
        title="17,528 country-years of carbon dioxide emitted for every dollar of output",
        licence="CC BY 4.0",
        source="https://ourworldindata.org/grapher/co2-intensity",
        classes=(),
        url="https://ourworldindata.org/grapher/co2-intensity.csv?v=1&csvType=full&useColumnShortNames=true",
        header=True,
        text_size=29,
        fields=_CO2_PER_DOLLAR_FIELDS,
    ),
    "renewable_electricity": Table(
        name="renewable_electricity",
        label="Renewable Share of Electricity",
        title="7,872 country-years and the share of electricity generated from renewables",
        licence="CC BY 4.0",
        source="https://ourworldindata.org/grapher/share-electricity-renewables",
        classes=(),
        url="https://ourworldindata.org/grapher/share-electricity-renewables.csv?v=1&csvType=full&useColumnShortNames=true",
        header=True,
        text_size=35,
        fields=_RENEWABLE_ELECTRICITY_FIELDS,
    ),
    "electricity_generation": Table(
        name="electricity_generation",
        label="Electricity Generation",
        title="7,913 country-years of electricity generated, in terawatt-hours",
        licence="CC BY 4.0",
        source="https://ourworldindata.org/grapher/electricity-generation",
        classes=(),
        url="https://ourworldindata.org/grapher/electricity-generation.csv?v=1&csvType=full&useColumnShortNames=true",
        header=True,
        text_size=35,
        fields=_ELECTRICITY_GENERATION_FIELDS,
    ),
    "renewable_energy": Table(
        name="renewable_energy",
        label="Renewable Share of Energy",
        title="6,379 country-years and the share of primary energy that came from renewables",
        licence="CC BY 4.0",
        source="https://ourworldindata.org/grapher/renewable-share-energy",
        classes=(),
        url="https://ourworldindata.org/grapher/renewable-share-energy.csv?v=1&csvType=full&useColumnShortNames=true",
        header=True,
        text_size=30,
        fields=_RENEWABLE_ENERGY_FIELDS,
    ),
    "energy_use_per_person": Table(
        name="energy_use_per_person",
        label="Energy Use per Person",
        title="11,225 country-years of primary energy used per person, in kilowatt-hours",
        licence="CC BY 4.0",
        source="https://ourworldindata.org/grapher/per-capita-energy-use",
        classes=(),
        url="https://ourworldindata.org/grapher/per-capita-energy-use.csv?v=1&csvType=full&useColumnShortNames=true",
        header=True,
        text_size=32,
        fields=_ENERGY_USE_PER_PERSON_FIELDS,
    ),
    "primary_energy": Table(
        name="primary_energy",
        label="Primary Energy Consumption",
        title="13,414 country-years of primary energy used, in terawatt-hours",
        licence="CC BY 4.0",
        source="https://ourworldindata.org/grapher/primary-energy-cons",
        classes=(),
        url="https://ourworldindata.org/grapher/primary-energy-cons.csv?v=1&csvType=full&useColumnShortNames=true",
        header=True,
        text_size=32,
        fields=_PRIMARY_ENERGY_FIELDS,
    ),
    "cereal_yields": Table(
        name="cereal_yields",
        label="Cereal Yields",
        title="13,488 country-years of cereal harvested, in tonnes a hectare",
        licence="CC BY 4.0",
        source="https://ourworldindata.org/grapher/cereal-yield",
        classes=(),
        url="https://ourworldindata.org/grapher/cereal-yield.csv?v=1&csvType=full&useColumnShortNames=true",
        header=True,
        text_size=45,
        fields=_CEREAL_YIELDS_FIELDS,
    ),
    "calorie_supply": Table(
        name="calorie_supply",
        label="Daily Calorie Supply",
        title="13,454 country-years and how many calories a day each had for every person",
        licence="CC BY 4.0",
        source="https://ourworldindata.org/grapher/daily-per-capita-caloric-supply",
        classes=(),
        url="https://ourworldindata.org/grapher/daily-per-capita-caloric-supply.csv?v=1&csvType=full&useColumnShortNames=true",
        header=True,
        text_size=45,
        fields=_CALORIE_SUPPLY_FIELDS,
    ),
    "gdp_per_capita_maddison": Table(
        name="gdp_per_capita_maddison",
        label="GDP per Capita (Maddison)",
        title="21,586 country-years of output per person back to the year one, in 2011 dollars",
        licence="CC BY 4.0",
        source="https://ourworldindata.org/grapher/gdp-per-capita-maddison",
        classes=(),
        url="https://ourworldindata.org/grapher/gdp-per-capita-maddison.csv?v=1&csvType=full&useColumnShortNames=true",
        header=True,
        text_size=48,
        fields=_GDP_PER_CAPITA_MADDISON_FIELDS,
    ),
    "population_density": Table(
        name="population_density",
        label="Population Density",
        title="76,576 country-years and how many people lived on each square kilometre",
        licence="CC BY 4.0",
        source="https://ourworldindata.org/grapher/population-density",
        classes=(),
        url="https://ourworldindata.org/grapher/population-density.csv?v=1&csvType=full&useColumnShortNames=true",
        header=True,
        text_size=32,
        fields=_POPULATION_DENSITY_FIELDS,
    ),
    "urban_population": Table(
        name="urban_population",
        label="Urban Population Share",
        title="21,052 country-years and the share of people living in towns and cities",
        licence="CC BY 4.0",
        source="https://ourworldindata.org/grapher/share-of-population-urban",
        classes=(),
        url="https://ourworldindata.org/grapher/share-of-population-urban.csv?v=1&csvType=full&useColumnShortNames=true",
        header=True,
        text_size=61,
        fields=_URBAN_POPULATION_FIELDS,
    ),
    "life_expectancy_at_birth": Table(
        name="life_expectancy_at_birth",
        label="Life Expectancy at Birth",
        title="18,722 country-years and how long a child born then could expect to live",
        licence="CC BY 4.0",
        source="https://ourworldindata.org/grapher/life-expectancy-at-birth-total-years",
        classes=(),
        url="https://ourworldindata.org/grapher/life-expectancy-at-birth-total-years.csv?v=1&csvType=full&useColumnShortNames=true",
        header=True,
        text_size=59,
        fields=_LIFE_EXPECTANCY_AT_BIRTH_FIELDS,
    ),
    "child_mortality": Table(
        name="child_mortality",
        label="Child Mortality",
        title="17,066 country-years and how many children in a hundred died before turning five",
        licence="CC BY 4.0",
        source="https://ourworldindata.org/grapher/child-mortality",
        classes=(),
        url="https://ourworldindata.org/grapher/child-mortality.csv?v=1&csvType=full&useColumnShortNames=true",
        header=True,
        text_size=32,
        fields=_CHILD_MORTALITY_FIELDS,
    ),
    "child_mortality_igme": Table(
        name="child_mortality_igme",
        label="Child Mortality (UN IGME)",
        title="13,980 country-years of child deaths per hundred live births, as the UN counts them",
        licence="CC BY 4.0",
        source="https://ourworldindata.org/grapher/child-mortality-igme",
        classes=(),
        url="https://ourworldindata.org/grapher/child-mortality-igme.csv?v=1&csvType=full&useColumnShortNames=true",
        header=True,
        text_size=32,
        fields=_CHILD_MORTALITY_IGME_FIELDS,
    ),
    "adult_literacy": Table(
        name="adult_literacy",
        label="Adult Literacy",
        title="1,833 country-years and the share of adults who could read and write",
        licence="CC BY 4.0",
        source="https://ourworldindata.org/grapher/literacy-rate-adults",
        classes=(),
        url="https://ourworldindata.org/grapher/literacy-rate-adults.csv?v=1&csvType=full&useColumnShortNames=true",
        header=True,
        text_size=51,
        fields=_ADULT_LITERACY_FIELDS,
    ),
    "human_development_index": Table(
        name="human_development_index",
        label="Human Development Index",
        title="6,604 country-years scored on health, schooling and income together",
        licence="CC BY 4.0",
        source="https://ourworldindata.org/grapher/human-development-index",
        classes=(),
        url="https://ourworldindata.org/grapher/human-development-index.csv?v=1&csvType=full&useColumnShortNames=true",
        header=True,
        text_size=38,
        fields=_HUMAN_DEVELOPMENT_INDEX_FIELDS,
    ),
    "sea_level": Table(
        name="sea_level",
        label="Global Sea Level",
        title="563 quarterly readings of how far the sea has risen since 1880, in millimetres",
        licence="CC BY 4.0",
        source="https://ourworldindata.org/grapher/sea-level",
        classes=(),
        url="https://ourworldindata.org/grapher/sea-level.csv?v=1&csvType=full&useColumnShortNames=true",
        header=True,
        text_size=16,
        fields=_SEA_LEVEL_FIELDS,
    ),
    "abortion_and_crime": Table(
        name="abortion_and_crime",
        label="Abortion and Crime",
        title="19,584 state-years used to ask whether legal abortion cut crime",
        licence="MIT",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/causaldata/abortion.html",
        classes=(),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/causaldata/abortion.csv",
        header=True,
        fields=_ABORTION_AND_CRIME_FIELDS,
    ),
    "adult_services": Table(
        name="adult_services",
        label="Adult Services and Risk",
        title="1,787 sessions sold by escorts and what each one was paid for",
        licence="MIT",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/causaldata/adult_services.html",
        classes=(),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/causaldata/adult_services.csv",
        header=True,
        fields=_ADULT_SERVICES_FIELDS,
    ),
    "affair_counts": Table(
        name="affair_counts",
        label="Affairs as Counts",
        title="601 married people and how many affairs each admitted to",
        licence="GPL-2",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/COUNT/affairs.html",
        classes=(),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/COUNT/affairs.csv",
        header=True,
        fields=_AFFAIR_COUNTS_FIELDS,
    ),
    "alone_episodes": Table(
        name="alone_episodes",
        label="Alone Episodes",
        title="172 episodes of the survival show and how viewers rated each",
        licence="CC0",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/alone/episodes.html",
        classes=(),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/alone/episodes.csv",
        header=True,
        dates="%Y-%m-%d",
        text_size=432,
        fields=_ALONE_EPISODES_FIELDS,
        codes={
            "version": ("AU", "US", "US Frozen"),
        },
    ),
    "alone_loadouts": Table(
        name="alone_loadouts",
        label="Alone Loadouts",
        title="1,240 items survivalists chose to carry into the wild",
        licence="CC0",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/alone/loadouts.html",
        classes=(),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/alone/loadouts.csv",
        header=True,
        text_size=103,
        fields=_ALONE_LOADOUTS_FIELDS,
        codes={
            "version": ("US",),
        },
    ),
    "alone_seasons": Table(
        name="alone_seasons",
        label="Alone Seasons",
        title="21 seasons of the survival show and where each was filmed",
        licence="CC0",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/alone/seasons.html",
        classes=(),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/alone/seasons.csv",
        header=True,
        dates="%Y-%m-%d",
        fields=_ALONE_SEASONS_FIELDS,
        codes={
            "version": ("AU", "US", "US Frozen"),
            "subtitle": (
                "Africa",
                "Arctic Circle",
                "Grizzly Mountain",
                "Lost and Found",
                "Million Dollar Challenge",
                "Polar Bear Island",
                "Predator Lake",
                "Redemption",
                "The Arctic",
            ),
            "location": (
                "Big River",
                "Chilko Lake",
                "Coast",
                "Great Karoo Desert",
                "Great Slave Lake",
                "Mackenzie River delta in Arctic",
                "Patagonia",
                "Reindeer Lake",
                "Selenge Province",
                "Vancouver Island",
                "lutruwita / Tasmania",
            ),
            "region": (
                "British Columbia",
                "Inuvik, Northwest Territories",
                "Labrador",
                "Northwest Territories",
                "Patagonia",
                "Saskatchewan",
                "South Island",
                "lutruwita / Tasmania",
            ),
            "country": (
                "Argentina",
                "Australia",
                "Canada",
                "Mongolia",
                "New Zealand",
                "South Africa",
            ),
        },
    ),
    "alone_survivalists": Table(
        name="alone_survivalists",
        label="Alone Survivalists",
        title="160 survivalists dropped in the wild and how many days each lasted",
        licence="CC0",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/alone/survivalists.html",
        classes=(),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/alone/survivalists.csv",
        header=True,
        text_size=122,
        fields=_ALONE_SURVIVALISTS_FIELDS,
        codes={
            "version": ("AU", "US", "US Frozen"),
            "gender": ("Female", "Male"),
            "country": (
                "Australia",
                "Canada",
                "New Zealand",
                "U.S. Virgin Islands",
                "United Kingdom",
                "United States",
            ),
            "medically_evacuated": ("FALSE", "TRUE"),
            "reason_category": ("Health", "Loss of inventory", "Personal"),
            "team": (
                "Alex and Logan Ribar (father/son)",
                "Brad and Josh Richardson (brothers)",
                "Chris and Brody Wilkes (brothers)",
                "Dave and Brooke Whipple (husband/wife)",
                "Jesse and Shannon Bosdell (brothers)",
                "Pete and Sam Brockdorff (father/son)",
                "Ted and Jim Baird (brothers)",
            ),
        },
    ),
    "ancient_shipwrecks": Table(
        name="ancient_shipwrecks",
        label="Ancient Mediterranean Shipwrecks",
        title="1,784 ancient wrecks and how deep in the Mediterranean each lies",
        licence="GPL-3 or later",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/folio/shipwrecks.html",
        classes=(),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/folio/shipwrecks.csv",
        header=True,
        text_size=50,
        fields=_ANCIENT_SHIPWRECKS_FIELDS,
        codes={
            "sea": (
                "Adriatic",
                "Aegean",
                "Black Sea",
                "Central Mediterranean",
                "Eastern Mediterranean",
                "Indian Ocean",
                "Ionian",
                "Northern Aegean",
                "Red Sea",
                "Southern Aegean",
                "Tyrrhenian Sea",
                "West Mediterranean",
                "Western Mediterranean",
            ),
            "country": (
                "Albania",
                "Bulgaria",
                "Croatia",
                "Cyprus",
                "Egypt",
                "France",
                "Greece",
                "India",
                "International waters",
                "Israel",
                "Italy",
                "Italy - Sicily",
                "Lebanon",
                "Libya",
                "Malta",
                "Minorca",
                "Montenegro",
                "Romania",
                "Spain",
                "Sudan",
                "Syria",
                "Tunisia",
                "Turkey",
                "ZZ-Non-Mediterranean",
            ),
        },
    ),
    "animal_attributes": Table(
        name="animal_attributes",
        label="Attributes of Animals",
        title="20 animals and which of six attributes each of them has",
        licence="GPL-2 or later",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/cluster/animals.html",
        classes=(),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/cluster/animals.csv",
        header=True,
        text_size=3,
        fields=_ANIMAL_ATTRIBUTES_FIELDS,
    ),
    "anscombe_quartet": Table(
        name="anscombe_quartet",
        label="Anscombe's Quartet",
        title="44 points of the four sets that share a mean and a fit and nothing else",
        licence="MIT",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/quartets/anscombe_quartet.html",
        classes=("linear", "nonlinear", "outlier", "leverage"),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/quartets/anscombe_quartet.csv",
        header=True,
        fields=_ANSCOMBE_QUARTET_FIELDS,
        labels={"(1) Linear": 0, "(2) Nonlinear": 1, "(3) Outlier": 2, "(4) Leverage": 3},
    ),
    "ansett_passengers": Table(
        name="ansett_passengers",
        label="Ansett Airline Passengers",
        title="7,407 weeks of passengers flown between Australian cities",
        licence="GPL-3",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/tsibbledata/ansett.html",
        classes=(),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/tsibbledata/ansett.csv",
        header=True,
        text_size=8,
        fields=_ANSETT_PASSENGERS_FIELDS,
        codes={
            "airports": (
                "ADL-PER",
                "MEL-ADL",
                "MEL-BNE",
                "MEL-OOL",
                "MEL-PER",
                "MEL-SYD",
                "SYD-ADL",
                "SYD-BNE",
                "SYD-OOL",
                "SYD-PER",
            ),
            "class": ("Business", "Economy", "First"),
        },
    ),
    "arbuthnot_christenings": Table(
        name="arbuthnot_christenings",
        label="Arbuthnot's Christenings",
        title="82 years of London christenings and how many of each sex were baptised",
        licence="GPL",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/HistData/Arbuthnot.html",
        classes=(),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/HistData/Arbuthnot.csv",
        header=True,
        fields=_ARBUTHNOT_CHRISTENINGS_FIELDS,
    ),
    "arctic_pit_houses": Table(
        name="arctic_pit_houses",
        label="Pit Houses of Arctic Norway",
        title="45 pit houses in arctic Norway and how large each was built",
        licence="GPL-2 or later",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/archdata/PitHouses.html",
        classes=("large", "medium", "small"),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/archdata/PitHouses.csv",
        header=True,
        fields=_ARCTIC_PIT_HOUSES_FIELDS,
        labels={"Large": 0, "Medium": 1, "Small": 2},
        codes={
            "hearths": ("Charcoal Conc", "None", "One", "Two"),
            "depth": ("Deep", "Shallow"),
            "form": ("Oval", "Rectangular"),
            "orient": ("Gabel Toward Coast", "Parallel Coast"),
            "entrance": ("Front and One Side", "None", "One Side"),
        },
    ),
    "arizona_cardiac_stays": Table(
        name="arizona_cardiac_stays",
        label="Arizona Cardiac Hospital Stays",
        title="3,589 Arizona cardiac patients and how many days each stayed",
        licence="GPL-2",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/COUNT/azpro.html",
        classes=(),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/COUNT/azpro.csv",
        header=True,
        fields=_ARIZONA_CARDIAC_STAYS_FIELDS,
    ),
    "arthritis_treatment": Table(
        name="arthritis_treatment",
        label="Arthritis Treatment",
        title="84 patients given a new treatment for arthritis and how each fared",
        licence="GPL-2",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/vcd/Arthritis.html",
        classes=("marked", "none", "some"),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/vcd/Arthritis.csv",
        header=True,
        fields=_ARTHRITIS_TREATMENT_FIELDS,
        labels={"Marked": 0, "None": 1, "Some": 2},
        codes={
            "treatment": ("Placebo", "Treated"),
            "sex": ("Female", "Male"),
        },
    ),
    "ashkenazi_breast_cancer": Table(
        name="ashkenazi_breast_cancer",
        label="Ashkenazi Breast Cancer",
        title="3,920 Ashkenazi women and whether each carried the mutation",
        licence="CC0",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/asaur/ashkenazi.html",
        classes=("no_mutation", "mutation"),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/asaur/ashkenazi.csv",
        header=True,
        fields=_ASHKENAZI_BREAST_CANCER_FIELDS,
        labels={"0": 0, "1": 1},
    ),
    "atmospheric_radiocarbon": Table(
        name="atmospheric_radiocarbon",
        label="Atmospheric Radiocarbon in Norway",
        title="620 air samples measured for the radiocarbon the bomb tests left",
        licence="GPL-3 or later",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/folio/nydal1996.html",
        classes=("fruholmen", "gr_kallen", "kapp_linn", "lindesnes", "vassfjellet"),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/folio/nydal1996.csv",
        header=True,
        dates="%Y-%m-%d",
        text_size=8,
        fields=_ATMOSPHERIC_RADIOCARBON_FIELDS,
        labels={"Fruholmen": 0, "Gråkallen": 1, "Kapp Linné": 2, "Lindesnes": 3, "Vassfjellet": 4},
    ),
    "australian_car_policies": Table(
        name="australian_car_policies",
        label="Australian Car Insurance Policies",
        title="67,856 Australian car policies and what each claimed in a year",
        licence="GPL-2",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/insuranceData/dataCar.html",
        classes=(),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/insuranceData/dataCar.csv",
        header=True,
        fields=_AUSTRALIAN_CAR_POLICIES_FIELDS,
        codes={
            "veh_body": (
                "BUS",
                "CONVT",
                "COUPE",
                "HBACK",
                "HDTOP",
                "MCARA",
                "MIBUS",
                "PANVN",
                "RDSTR",
                "SEDAN",
                "STNWG",
                "TRUCK",
                "UTE",
            ),
            "gender": ("F", "M"),
            "area": ("A", "B", "C", "D", "E", "F"),
            "x_obstat": ("01101    0    0    0",),
        },
    ),
    "australian_livestock": Table(
        name="australian_livestock",
        label="Australian Livestock Slaughter",
        title="29,364 months of animals sent to slaughter across Australia",
        licence="GPL-3",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/tsibbledata/aus_livestock.html",
        classes=(),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/tsibbledata/aus_livestock.csv",
        header=True,
        text_size=8,
        fields=_AUSTRALIAN_LIVESTOCK_FIELDS,
        codes={
            "animal": (
                "Bulls, bullocks and steers",
                "Calves",
                "Cattle (excl. calves)",
                "Cows and heifers",
                "Lambs",
                "Pigs",
                "Sheep",
            ),
            "state": (
                "Australian Capital Territory",
                "New South Wales",
                "Northern Territory",
                "Queensland",
                "South Australia",
                "Tasmania",
                "Victoria",
                "Western Australia",
            ),
        },
    ),
    "australian_production": Table(
        name="australian_production",
        label="Australian Quarterly Production",
        title="218 quarters of Australian beer, bricks, cement and electricity",
        licence="GPL-3",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/tsibbledata/aus_production.html",
        classes=(),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/tsibbledata/aus_production.csv",
        header=True,
        text_size=7,
        fields=_AUSTRALIAN_PRODUCTION_FIELDS,
    ),
    "australian_retail": Table(
        name="australian_retail",
        label="Australian Retail Turnover",
        title="64,532 months of retail takings by state and by trade",
        licence="GPL-3",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/tsibbledata/aus_retail.html",
        classes=(),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/tsibbledata/aus_retail.csv",
        header=True,
        text_size=65,
        fields=_AUSTRALIAN_RETAIL_FIELDS,
        codes={
            "state": (
                "Australian Capital Territory",
                "New South Wales",
                "Northern Territory",
                "Queensland",
                "South Australia",
                "Tasmania",
                "Victoria",
                "Western Australia",
            ),
        },
    ),
    "automobile_claims": Table(
        name="automobile_claims",
        label="Automobile Claims",
        title="6,773 car insurance claims and what each one paid out",
        licence="GPL-2",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/insuranceData/AutoClaims.html",
        classes=(),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/insuranceData/AutoClaims.csv",
        header=True,
        fields=_AUTOMOBILE_CLAIMS_FIELDS,
        codes={
            "state": (
                "STATE 01",
                "STATE 02",
                "STATE 03",
                "STATE 04",
                "STATE 06",
                "STATE 07",
                "STATE 10",
                "STATE 11",
                "STATE 12",
                "STATE 13",
                "STATE 14",
                "STATE 15",
                "STATE 17",
            ),
            "class": (
                "C1",
                "C11",
                "C1A",
                "C1B",
                "C1C",
                "C2",
                "C6",
                "C7",
                "C71",
                "C72",
                "C7A",
                "C7B",
                "C7C",
                "F1",
                "F11",
                "F6",
                "F7",
                "F71",
            ),
            "gender": ("F", "M"),
        },
    ),
    "bad_health_visits": Table(
        name="bad_health_visits",
        label="Doctor Visits and Bad Health",
        title="1,127 Germans and how often each went to a doctor in a year",
        licence="GPL-2",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/COUNT/badhealth.html",
        classes=(),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/COUNT/badhealth.csv",
        header=True,
        fields=_BAD_HEALTH_VISITS_FIELDS,
    ),
    "bakeoff_bakers": Table(
        name="bakeoff_bakers",
        label="Great British Bake Off Bakers",
        title="120 bakers who competed and how far into the series each got",
        licence="MIT",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/bakeoff/bakers.html",
        classes=(),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/bakeoff/bakers.csv",
        header=True,
        dates="%Y-%m-%d",
        text_size=42,
        fields=_BAKEOFF_BAKERS_FIELDS,
    ),
    "bakeoff_challenges": Table(
        name="bakeoff_challenges",
        label="Great British Bake Off Challenges",
        title="1,136 bakes and how the baker who made each one fared that week",
        licence="MIT",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/bakeoff/challenges.html",
        classes=(
            "stayed_in",
            "went_out",
            "runner_up",
            "star_baker",
            "withdrew",
            "winner",
            "footnoted",
            "not_recorded",
        ),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/bakeoff/challenges.csv",
        header=True,
        text_size=202,
        fields=_BAKEOFF_CHALLENGES_FIELDS,
        labels={
            "IN": 0,
            "OUT": 1,
            "Runner-up": 2,
            "STAR BAKER": 3,
            "WD": 4,
            "WINNER": 5,
            "[a]": 6,
            "": 7,
        },
    ),
    "bakeoff_episodes": Table(
        name="bakeoff_episodes",
        label="Great British Bake Off Episodes",
        title="94 episodes and how many bakers were left in each of them",
        licence="MIT",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/bakeoff/episodes.html",
        classes=(),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/bakeoff/episodes.csv",
        header=True,
        text_size=16,
        fields=_BAKEOFF_EPISODES_FIELDS,
        codes={
            "winner_name": (
                "Candice",
                "David",
                "Edd",
                "Frances",
                "Joanne",
                "John",
                "Nadiya",
                "Nancy",
                "Rahul",
                "Sophie",
            ),
        },
    ),
    "bakeoff_ratings": Table(
        name="bakeoff_ratings",
        label="Great British Bake Off Ratings",
        title="94 episodes and how many people watched each one",
        licence="MIT",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/bakeoff/ratings.html",
        classes=(),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/bakeoff/ratings.csv",
        header=True,
        dates="%Y-%m-%d",
        text_size=18,
        fields=_BAKEOFF_RATINGS_FIELDS,
    ),
    "barley_yields": Table(
        name="barley_yields",
        label="Barley Yields in Minnesota",
        title="120 plots of barley grown in the 1930s and what each yielded",
        licence="GPL-2 or later",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/lattice/barley.html",
        classes=(),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/lattice/barley.csv",
        header=True,
        fields=_BARLEY_YIELDS_FIELDS,
        codes={
            "variety": (
                "Glabron",
                "Manchuria",
                "No. 457",
                "No. 462",
                "No. 475",
                "Peatland",
                "Svansota",
                "Trebi",
                "Velvet",
                "Wisconsin No. 38",
            ),
            "site": ("Crookston", "Duluth", "Grand Rapids", "Morris", "University Farm", "Waseca"),
        },
    ),
    "benthic_oxygen_stack": Table(
        name="benthic_oxygen_stack",
        label="The Benthic Oxygen Isotope Stack",
        title="2,115 points of the deep-sea record of the last five million years",
        licence="GPL-3 or later",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/folio/lisiecki2005.html",
        classes=(),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/folio/lisiecki2005.csv",
        header=True,
        fields=_BENTHIC_OXYGEN_STACK_FIELDS,
    ),
    "big_tech_shares": Table(
        name="big_tech_shares",
        label="Big Technology Share Prices",
        title="5,032 trading days of four big technology shares",
        licence="GPL-3",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/tsibbledata/gafa_stock.html",
        classes=(),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/tsibbledata/gafa_stock.csv",
        header=True,
        dates="%Y-%m-%d",
        fields=_BIG_TECH_SHARES_FIELDS,
        codes={
            "symbol": ("AAPL", "AMZN", "FB", "GOOG"),
        },
    ),
    "blood_storage": Table(
        name="blood_storage",
        label="Blood Storage and Prostate Cancer",
        title="316 men transfused during surgery and whether the cancer came back",
        licence="MIT",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/medicaldata/blood_storage.html",
        classes=("no_recurrence", "recurrence"),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/medicaldata/blood_storage.csv",
        header=True,
        fields=_BLOOD_STORAGE_FIELDS,
        labels={"0": 0, "1": 1},
    ),
    "bodily_injury_claims": Table(
        name="bodily_injury_claims",
        label="Automobile Bodily Injury Claims",
        title="1,340 bodily injury claims and what each cost, in thousands",
        licence="GPL-2",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/insuranceData/AutoBi.html",
        classes=(),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/insuranceData/AutoBi.csv",
        header=True,
        fields=_BODILY_INJURY_CLAIMS_FIELDS,
    ),
    "bone_marrow_leukaemia": Table(
        name="bone_marrow_leukaemia",
        label="Bone Marrow Transplants for Leukaemia",
        title="137 leukaemia patients given a bone marrow transplant",
        licence="GPL-3 or later",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/KMsurv/bmt.html",
        classes=(),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/KMsurv/bmt.csv",
        header=True,
        fields=_BONE_MARROW_LEUKAEMIA_FIELDS,
    ),
    "bornholm_brooches": Table(
        name="bornholm_brooches",
        label="Bornholm Brooch Assemblages",
        title="77 Danish iron age graves and the brooch types found in each",
        licence="GPL-2 or later",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/archdata/Bornholm.html",
        classes=("f_1a", "f_1b", "f_2a", "f_2b", "f_2c", "f_3a", "f_3b"),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/archdata/Bornholm.csv",
        header=True,
        text_size=19,
        fields=_BORNHOLM_BROOCHES_FIELDS,
        labels={"1a": 0, "1b": 1, "2a": 2, "2b": 3, "2c": 4, "3a": 5, "3b": 6},
    ),
    "bowley_wages": Table(
        name="bowley_wages",
        label="Bowley's Wages",
        title="45 years of the average British wage, in pounds a year",
        licence="GPL",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/HistData/Bowley.html",
        classes=(),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/HistData/Bowley.csv",
        header=True,
        fields=_BOWLEY_WAGES_FIELDS,
    ),
    "breast_feeding": Table(
        name="breast_feeding",
        label="How Long Mothers Breast Fed",
        title="927 American mothers and how many weeks each breast fed",
        licence="GPL-3 or later",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/KMsurv/bfeed.html",
        classes=(),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/KMsurv/bfeed.csv",
        header=True,
        fields=_BREAST_FEEDING_FIELDS,
    ),
    "breslau_life_table": Table(
        name="breslau_life_table",
        label="Halley's Breslau Life Table",
        title="100 ages and how many people of each died in Breslau in the 1680s",
        licence="GPL",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/HistData/Breslau.html",
        classes=(),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/HistData/Breslau.csv",
        header=True,
        fields=_BRESLAU_LIFE_TABLE_FIELDS,
    ),
    "bronze_age_cups": Table(
        name="bronze_age_cups",
        label="Bronze Age Cups from Italy",
        title="60 Italian bronze age cups measured and dated to a phase",
        licence="GPL-2 or later",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/archdata/BACups.html",
        classes=("protoapennine", "subapennine"),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/archdata/BACups.csv",
        header=True,
        fields=_BRONZE_AGE_CUPS_FIELDS,
        labels={"Protoapennine": 0, "Subapennine": 1},
    ),
    "bundesliga_matches": Table(
        name="bundesliga_matches",
        label="Bundesliga Match Results",
        title="14,018 German league matches and how many goals each side scored",
        licence="GPL-2",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/vcd/Bundesliga.html",
        classes=(),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/vcd/Bundesliga.csv",
        header=True,
        dates="%Y-%m-%dT%H:%M:%SZ",
        text_size=25,
        fields=_BUNDESLIGA_MATCHES_FIELDS,
    ),
    "bundestag_2005": Table(
        name="bundestag_2005",
        label="The 2005 German Election",
        title="80 seat counts by party and state in the 2005 German election",
        licence="GPL-2",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/vcd/Bundestag2005.html",
        classes=(),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/vcd/Bundestag2005.csv",
        header=True,
        fields=_BUNDESTAG_2005_FIELDS,
        codes={
            "bundesland": (
                "Baden-Wuerttemberg",
                "Bayern",
                "Berlin",
                "Brandenburg",
                "Bremen",
                "Hamburg",
                "Hessen",
                "Mecklenburg-Vorpommern",
                "Niedersachsen",
                "Nordrhein-Westfalen",
                "Rheinland-Pfalz",
                "Saarland",
                "Sachsen",
                "Sachsen-Anhalt",
                "Schleswig-Holstein",
                "Thueringen",
            ),
            "fraktion": ("CDU/CSU", "FDP", "Gruene", "Linke", "SPD"),
        },
    ),
    "burn_wound_infection": Table(
        name="burn_wound_infection",
        label="Burn Wound Infection",
        title="154 burn patients and how long each went before the wound was infected",
        licence="GPL-3 or later",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/KMsurv/burn.html",
        classes=(),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/KMsurv/burn.csv",
        header=True,
        fields=_BURN_WOUND_INFECTION_FIELDS,
    ),
    "care_home_incidents": Table(
        name="care_home_incidents",
        label="Care Home Incidents",
        title="1,216 care home inspections and whether the home was found to be failing",
        licence="MIT",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/MLDataR/care_home_incidents.html",
        classes=("passing", "failing"),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/MLDataR/care_home_incidents.csv",
        header=True,
        fields=_CARE_HOME_INCIDENTS_FIELDS,
        labels={"0": 0, "1": 1},
    ),
    "cavendish_density": Table(
        name="cavendish_density",
        label="Cavendish's Density of the Earth",
        title="29 weighings of the earth against water",
        licence="GPL",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/HistData/Cavendish.html",
        classes=(),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/HistData/Cavendish.csv",
        header=True,
        fields=_CAVENDISH_DENSITY_FIELDS,
    ),
    "chinese_bronzes": Table(
        name="chinese_bronzes",
        label="Chinese Bronze Compositions",
        title="369 Chinese bronzes assayed and dated to a dynasty",
        licence="GPL-3 or later",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/folio/bronze.html",
        classes=("eastern_zhou", "shang", "western_zhou"),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/folio/bronze.csv",
        header=True,
        fields=_CHINESE_BRONZES_FIELDS,
        labels={"Eastern Zhou": 0, "Shang": 1, "Western Zhou": 2},
    ),
    "cholera_deaths_1849": Table(
        name="cholera_deaths_1849",
        label="Cholera Deaths in 1849",
        title="730 days of 1849 and how many Londoners died of cholera or of diarrhoea",
        licence="GPL",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/HistData/CholeraDeaths1849.html",
        classes=("cholera", "diarrhaea"),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/HistData/CholeraDeaths1849.csv",
        header=True,
        dates="%Y-%m-%d",
        fields=_CHOLERA_DEATHS_1849_FIELDS,
        labels={"Cholera": 0, "Diarrhaea": 1},
        codes={
            "month": (
                "Apr",
                "Aug",
                "Dec",
                "Feb",
                "Jan",
                "July",
                "Jun",
                "Mar",
                "May",
                "Nov",
                "Oct",
                "Sept",
            ),
            "day_of_week": ("Fri", "Mon", "Sat", "Sun", "Thu", "Tue", "Wed"),
        },
    ),
    "choral_singers": Table(
        name="choral_singers",
        label="Heights of New York Choristers",
        title="235 singers in the New York Choral Society and what each sings",
        licence="GPL-2 or later",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/lattice/singer.html",
        classes=(
            "alto_1",
            "alto_2",
            "bass_1",
            "bass_2",
            "soprano_1",
            "soprano_2",
            "tenor_1",
            "tenor_2",
        ),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/lattice/singer.csv",
        header=True,
        fields=_CHORAL_SINGERS_FIELDS,
        labels={
            "Alto 1": 0,
            "Alto 2": 1,
            "Bass 1": 2,
            "Bass 2": 3,
            "Soprano 1": 4,
            "Soprano 2": 5,
            "Tenor 1": 6,
            "Tenor 2": 7,
        },
    ),
    "coal_miners_breathing": Table(
        name="coal_miners_breathing",
        label="Breathlessness and Wheeze in Coal Miners",
        title="36 groupings of British coal miners by age and by what ailed them",
        licence="GPL-2",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/vcd/CoalMiners.html",
        classes=(),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/vcd/CoalMiners.csv",
        header=True,
        fields=_COAL_MINERS_BREATHING_FIELDS,
        codes={
            "breathlessness": ("B", "NoB"),
            "wheeze": ("NoW", "W"),
            "age": (
                "20-24",
                "25-29",
                "30-34",
                "35-39",
                "40-44",
                "45-49",
                "50-54",
                "55-59",
                "60-64",
            ),
        },
    ),
    "college_proximity": Table(
        name="college_proximity",
        label="Living Near a College",
        title="3,010 American men and what each earned, near a college or far from one",
        licence="MIT",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/causaldata/close_college.html",
        classes=(),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/causaldata/close_college.csv",
        header=True,
        fields=_COLLEGE_PROXIMITY_FIELDS,
    ),
    "college_scorecard": Table(
        name="college_scorecard",
        label="College Scorecard Schools",
        title="11,300 American colleges and who runs each of them",
        licence="CC0",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/collegeScorecard/school.html",
        classes=("for_profit", "nonprofit", "public", "not_recorded"),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/collegeScorecard/school.csv",
        header=True,
        text_size=136,
        fields=_COLLEGE_SCORECARD_FIELDS,
        labels={"For-profit": 0, "Nonprofit": 1, "Public": 2, "": 3},
        codes={
            "deg_predominant": ("Associate", "Bachelor", "Certificate", "Graduate"),
            "locale_type": ("City", "Rural", "Suburb", "Town"),
            "locale_size": ("Distant", "Fringe", "Large", "Midsize", "Remote", "Small"),
            "adm_req_test": ("Considered", "Not recommended", "Recommended", "Required"),
            "is_hbcu": ("FALSE", "TRUE"),
        },
    ),
    "corporal_punishment": Table(
        name="corporal_punishment",
        label="Attitudes to Corporal Punishment",
        title="36 groupings of Germans asked whether children should be smacked",
        licence="GPL-2",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/vcd/Punishment.html",
        classes=(),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/vcd/Punishment.csv",
        header=True,
        fields=_CORPORAL_PUNISHMENT_FIELDS,
        codes={
            "attitude": ("moderate", "no"),
            "memory": ("no", "yes"),
            "education": ("elementary", "high", "secondary"),
            "age": ("15-24", "25-39", "40-"),
        },
    ),
    "covid_testing": Table(
        name="covid_testing",
        label="COVID Testing at a Children's Hospital",
        title="15,524 COVID tests run in 2020 and what each came back as",
        licence="MIT",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/medicaldata/covid_testing.html",
        classes=("invalid", "negative", "positive"),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/medicaldata/covid_testing.csv",
        header=True,
        text_size=32,
        fields=_COVID_TESTING_FIELDS,
        labels={"invalid": 0, "negative": 1, "positive": 2},
        codes={
            "gender": ("female", "male"),
            "test_id": ("covid", "xcvd1"),
            "demo_group": ("client", "misc adult", "other adult", "patient", "unidentified"),
            "payor_group": (
                "charity care",
                "commercial",
                "government",
                "medical assistance",
                "other",
                "self pay",
                "unassigned",
            ),
            "patient_class": (
                "admit after surgery-ip",
                "admit after surgery-obs",
                "day surgery",
                "emergency",
                "inpatient",
                "not applicable",
                "observation",
                "outpatient",
                "recurring outpatient",
            ),
        },
    ),
    "csgo_matches": Table(
        name="csgo_matches",
        label="Counter-Strike Matches",
        title="1,133 Counter-Strike matches and how each one ended",
        licence="MIT",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/MLDataR/csgo.html",
        classes=("lost", "tie", "win"),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/MLDataR/csgo.csv",
        header=True,
        text_size=10,
        fields=_CSGO_MATCHES_FIELDS,
        labels={"Lost": 0, "Tie": 1, "Win": 2},
        codes={
            "map": (
                "Austria",
                "Cache",
                "Canals",
                "Cobblestone",
                "Dust II",
                "Inferno",
                "Italy",
                "Mirage",
                "Nuke",
                "Overpass",
            ),
        },
    ),
    "cytomegalovirus": Table(
        name="cytomegalovirus",
        label="Cytomegalovirus after Transplant",
        title="64 stem cell transplants and whether the virus woke up afterwards",
        licence="MIT",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/medicaldata/cytomegalovirus.html",
        classes=("quiet", "reactivated"),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/medicaldata/cytomegalovirus.csv",
        header=True,
        fields=_CYTOMEGALOVIRUS_FIELDS,
        labels={"0": 0, "1": 1},
        codes={
            "diagnosis": (
                "Hodgkin lymphoma",
                "acute lymphoblastic leukemia",
                "acute myeloid leukemia",
                "aplastic anemia",
                "chronic lymphocytic leukemia",
                "chronic myeloid leukemia",
                "congenital anemia",
                "multiple myelomas",
                "myelodysplastic syndrome",
                "myelofibrosis",
                "myeloproliferative disorder",
                "non-Hodgkin lymphoma",
                "renal cell carcinoma",
            ),
        },
    ),
    "danish_welfare": Table(
        name="danish_welfare",
        label="The Danish Welfare Study",
        title="180 groupings of Danes by drink, income, marriage and where they lived",
        licence="GPL-2",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/vcd/DanishWelfare.html",
        classes=(),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/vcd/DanishWelfare.csv",
        header=True,
        fields=_DANISH_WELFARE_FIELDS,
        codes={
            "alcohol": ("1-2", "<1", ">2"),
            "income": ("0-50", "100-150", "50-100", ">150"),
            "status": ("Married", "Unmarried", "Widow"),
            "urban": ("City", "Copenhagen", "Country", "LargeCity", "SubCopenhagen"),
        },
    ),
    "dart_points": Table(
        name="dart_points",
        label="Texas Dart Points",
        title="91 dart points from Texas and which of five types each is",
        licence="GPL-2 or later",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/archdata/DartPoints.html",
        classes=("darl", "ensor", "pedernales", "travis", "wells"),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/archdata/DartPoints.csv",
        header=True,
        text_size=8,
        fields=_DART_POINTS_FIELDS,
        labels={"Darl": 0, "Ensor": 1, "Pedernales": 2, "Travis": 3, "Wells": 4},
        codes={
            "blade_sh": ("E", "I", "R", "S"),
            "should_sh": ("E", "I", "S", "X"),
            "should_or": ("B", "H", "T", "X"),
            "haft_sh": ("A", "E", "I", "R", "S"),
            "haft_or": ("C", "E", "P", "T", "V"),
        },
    ),
    "datasaurus_dozen": Table(
        name="datasaurus_dozen",
        label="The Datasaurus Dozen",
        title="1,846 points of the thirteen sets that share every summary there is",
        licence="MIT",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/quartets/datasaurus_dozen.html",
        classes=(
            "away",
            "bullseye",
            "circle",
            "dino",
            "dots",
            "h_lines",
            "high_lines",
            "slant_down",
            "slant_up",
            "star",
            "v_lines",
            "wide_lines",
            "x_shape",
        ),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/quartets/datasaurus_dozen.csv",
        header=True,
        fields=_DATASAURUS_DOZEN_FIELDS,
        labels={
            "away": 0,
            "bullseye": 1,
            "circle": 2,
            "dino": 3,
            "dots": 4,
            "h_lines": 5,
            "high_lines": 6,
            "slant_down": 7,
            "slant_up": 8,
            "star": 9,
            "v_lines": 10,
            "wide_lines": 11,
            "x_shape": 12,
        },
    ),
    "deep_sea_fish": Table(
        name="deep_sea_fish",
        label="Deep Sea Fish Abundance",
        title="147 trawls of the deep Atlantic and how many fish each brought up",
        licence="GPL-2",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/COUNT/fishing.html",
        classes=(),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/COUNT/fishing.csv",
        header=True,
        fields=_DEEP_SEA_FISH_FIELDS,
        codes={
            "period": ("1977-1989", "2000-2002"),
        },
    ),
    "drag_race_appearances": Table(
        name="drag_race_appearances",
        label="Drag Race Contestants by Episode",
        title="2,320 appearances by a queen in an episode and how each placed",
        licence="GPL-2",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/dragracer/rpdr_contep.html",
        classes=(),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/dragracer/rpdr_contep.csv",
        header=True,
        text_size=28,
        fields=_DRAG_RACE_APPEARANCES_FIELDS,
        codes={
            "season": (
                "S01",
                "S02",
                "S03",
                "S04",
                "S05",
                "S06",
                "S07",
                "S08",
                "S09",
                "S10",
                "S11",
                "S12",
                "S13",
                "S14",
            ),
        },
    ),
    "drag_race_contestants": Table(
        name="drag_race_contestants",
        label="Drag Race Contestants",
        title="184 queens who competed and how old each was on entering",
        licence="GPL-2",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/dragracer/rpdr_contestants.html",
        classes=(),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/dragracer/rpdr_contestants.csv",
        header=True,
        dates="%Y-%m-%d",
        text_size=33,
        fields=_DRAG_RACE_CONTESTANTS_FIELDS,
        codes={
            "season": (
                "S01",
                "S02",
                "S03",
                "S04",
                "S05",
                "S06",
                "S07",
                "S08",
                "S09",
                "S10",
                "S11",
                "S12",
                "S13",
                "S14",
            ),
        },
    ),
    "drag_race_episodes": Table(
        name="drag_race_episodes",
        label="Drag Race Episodes",
        title="191 episodes and how many queens were still in the running",
        licence="GPL-2",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/dragracer/rpdr_ep.html",
        classes=(),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/dragracer/rpdr_ep.csv",
        header=True,
        dates="%Y-%m-%d",
        text_size=72,
        fields=_DRAG_RACE_EPISODES_FIELDS,
        codes={
            "season": (
                "S01",
                "S02",
                "S03",
                "S04",
                "S05",
                "S06",
                "S07",
                "S08",
                "S09",
                "S10",
                "S11",
                "S12",
                "S13",
                "S14",
            ),
            "minicw3": (
                "Ivy Winters",
                "Jiggly Caliente",
                "Lady Camden",
                "Monique Heart",
                "Phi Phi O'Hara",
                "Roxxxy Andrews",
            ),
            "minicw4": ("Willow Pill",),
            "bottom3": ("Daya Betty", "Jorgeous", "Plastique Tiara"),
            "bottom4": ("Jasmine Kennedie", "Ra’jah O’Hara"),  # noqa: RUF001
            "bottom5": ("Jorgeous", "Scarlet Envy"),
            "bottom6": ("Lady Camden", "Shuga Cain"),
            "eliminated2": ("Jorgeous", "Laila McQueen", "Vivienne Pinay"),
        },
    ),
    "drinks_and_wages": Table(
        name="drinks_and_wages",
        label="Elderton's Drink and Wages",
        title="70 trades in 1910 Britain and what a man in each earned a week",
        licence="GPL",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/HistData/DrinksWages.html",
        classes=(),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/HistData/DrinksWages.csv",
        header=True,
        text_size=16,
        fields=_DRINKS_AND_WAGES_FIELDS,
        codes={
            "class": ("A", "B", "C"),
        },
    ),
    "end_scrapers": Table(
        name="end_scrapers",
        label="End Scrapers from the Dordogne",
        title="48 groupings of end scraper shape and how many were found in each",
        licence="GPL-2 or later",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/archdata/EndScrapers.html",
        classes=(),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/archdata/EndScrapers.csv",
        header=True,
        fields=_END_SCRAPERS_FIELDS,
        codes={
            "width": ("Narrow", "Wide"),
            "sides": ("Convergent", "Parallel"),
            "curvature": ("Medium", "Round", "Shallow"),
            "retouched": ("Retouched", "Unretouched"),
            "site": ("Castenet A", "Ferrassie H"),
        },
    ),
    "epica_carbon_dioxide": Table(
        name="epica_carbon_dioxide",
        label="EPICA Dome C Carbon Dioxide",
        title="1,096 readings of the carbon dioxide trapped in Antarctic ice",
        licence="GPL-3 or later",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/folio/epica2008.html",
        classes=(),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/folio/epica2008.csv",
        header=True,
        fields=_EPICA_CARBON_DIOXIDE_FIELDS,
    ),
    "ernest_witte_burials": Table(
        name="ernest_witte_burials",
        label="Ernest Witte Burials",
        title="49 burials in a Texas cemetery and whether each was given grave goods",
        licence="GPL-2 or later",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/archdata/EWBurials.html",
        classes=("absent", "present"),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/archdata/EWBurials.csv",
        header=True,
        text_size=4,
        fields=_ERNEST_WITTE_BURIALS_FIELDS,
        labels={"Absent": 0, "Present": 1},
        codes={
            "age": ("Adolescent", "Adult", "Child", "Middle Adult", "Old Adult", "Young Adult"),
            "sex": ("Female", "Male"),
        },
    ),
    "esophageal_cancer": Table(
        name="esophageal_cancer",
        label="Oesophageal Cancer in Ille-et-Vilaine",
        title="88 groupings by age, drink and tobacco and how many in each had cancer",
        licence="MIT",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/medicaldata/esoph_ca.html",
        classes=(),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/medicaldata/esoph_ca.csv",
        header=True,
        fields=_ESOPHAGEAL_CANCER_FIELDS,
        codes={
            "agegp": ("25-34", "35-44", "45-54", "55-64", "65-74", "75+"),
            "alcgp": ("0-39g/day", "120+", "40-79", "80-119"),
            "tobgp": ("0-9g/day", "10-19", "20-29", "30+"),
        },
    ),
    "ethanol_engine": Table(
        name="ethanol_engine",
        label="Nitric Oxide from an Ethanol Engine",
        title="88 runs of an ethanol engine and how much nitric oxide came out",
        licence="GPL-2 or later",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/lattice/ethanol.html",
        classes=(),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/lattice/ethanol.csv",
        header=True,
        fields=_ETHANOL_ENGINE_FIELDS,
    ),
    "familial_polyposis": Table(
        name="familial_polyposis",
        label="Sulindac for Familial Polyposis",
        title="22 patients treated for bowel polyps and how many each had a year on",
        licence="MIT",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/medicaldata/polyps.html",
        classes=(),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/medicaldata/polyps.csv",
        header=True,
        fields=_FAMILIAL_POLYPOSIS_FIELDS,
        codes={
            "sex": ("female", "male"),
            "treatment": ("placebo", "sulindac"),
        },
    ),
    "fingerprint_patterns": Table(
        name="fingerprint_patterns",
        label="Whorls and Loops on Fingerprints",
        title="36 pairings of whorls and loops and how many hands showed each",
        licence="GPL",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/HistData/Fingerprints.html",
        classes=(),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/HistData/Fingerprints.csv",
        header=True,
        fields=_FINGERPRINT_PATTERNS_FIELDS,
    ),
    "fish_adult_growth": Table(
        name="fish_adult_growth",
        label="Adult Fish Growth",
        title="16,795 readings taken down the ear stones of adult fish",
        licence="GPL-3",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/fishdata/adult_growth.html",
        classes=(),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/fishdata/adult_growth.csv",
        header=True,
        text_size=4,
        fields=_FISH_ADULT_GROWTH_FIELDS,
    ),
    "fish_juvenile_catches": Table(
        name="fish_juvenile_catches",
        label="Juvenile Fish Catches",
        title="496 juvenile fish caught off New Zealand and where each was taken",
        licence="GPL-3",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/fishdata/juveniles.html",
        classes=("hutt", "wainuiomata"),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/fishdata/juveniles.csv",
        header=True,
        dates="%Y-%m-%d",
        text_size=6,
        fields=_FISH_JUVENILE_CATCHES_FIELDS,
        labels={"Hutt": 0, "Wainuiomata": 1},
        codes={
            "month": ("August", "November", "October", "September"),
        },
    ),
    "fish_juvenile_growth": Table(
        name="fish_juvenile_growth",
        label="Juvenile Fish Growth",
        title="496 juvenile fish and how fast each grew in its first year",
        licence="GPL-3",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/fishdata/juvenile_metrics.html",
        classes=(),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/fishdata/juvenile_metrics.csv",
        header=True,
        dates="%Y-%m-%d",
        text_size=6,
        fields=_FISH_JUVENILE_GROWTH_FIELDS,
    ),
    "funnel_beaker_pottery": Table(
        name="funnel_beaker_pottery",
        label="Funnel Beaker Pottery",
        title="118 Neolithic pots outlined by hand and sorted by shape",
        licence="GPL-2 or later",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/archdata/TRBPottery.html",
        classes=("bowls", "flasks", "funnel_beakers"),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/archdata/TRBPottery.csv",
        header=True,
        fields=_FUNNEL_BEAKER_POTTERY_FIELDS,
        labels={"Bowls": 0, "Flasks": 1, "Funnel beakers": 2},
    ),
    "furze_platt_handaxes": Table(
        name="furze_platt_handaxes",
        label="Furze Platt Handaxes",
        title="600 Acheulian handaxes measured at Furze Platt",
        licence="GPL-2 or later",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/archdata/Handaxes.html",
        classes=(),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/archdata/Handaxes.csv",
        header=True,
        text_size=10,
        fields=_FURZE_PLATT_HANDAXES_FIELDS,
    ),
    "galton_families": Table(
        name="galton_families",
        label="Galton's Families",
        title="934 children of 205 Victorian families and how tall each grew",
        licence="GPL",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/HistData/GaltonFamilies.html",
        classes=(),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/HistData/GaltonFamilies.csv",
        header=True,
        text_size=4,
        fields=_GALTON_FAMILIES_FIELDS,
        codes={
            "gender": ("female", "male"),
        },
    ),
    "galton_parent_child": Table(
        name="galton_parent_child",
        label="Galton's Parent and Child Heights",
        title="928 children of Victorian parents and how tall each grew",
        licence="GPL",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/HistData/Galton.html",
        classes=(),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/HistData/Galton.csv",
        header=True,
        fields=_GALTON_PARENT_CHILD_FIELDS,
    ),
    "geologic_time_scale": Table(
        name="geologic_time_scale",
        label="The Geologic Time Scale",
        title="176 named divisions of geologic time and when each began",
        licence="GPL-3 or later",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/folio/stratigraphy.html",
        classes=("eon", "era", "period", "series", "stage", "unranked"),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/folio/stratigraphy.csv",
        header=True,
        text_size=20,
        fields=_GEOLOGIC_TIME_SCALE_FIELDS,
        labels={"eon": 0, "era": 1, "period": 2, "series": 3, "stage": 4, "": 5},
    ),
    "german_health_1984": Table(
        name="german_health_1984",
        label="The German Health Survey of 1984",
        title="3,874 Germans in 1984 and how often each went to a doctor",
        licence="GPL-2",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/COUNT/rwm1984.html",
        classes=(),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/COUNT/rwm1984.csv",
        header=True,
        fields=_GERMAN_HEALTH_1984_FIELDS,
    ),
    "german_health_reform": Table(
        name="german_health_reform",
        label="German Health Reform Doctor Visits",
        title="2,227 Germans seen before and after the 1997 health reform",
        licence="GPL-2",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/COUNT/mdvis.html",
        classes=(),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/COUNT/mdvis.csv",
        header=True,
        fields=_GERMAN_HEALTH_REFORM_FIELDS,
    ),
    "german_suicides": Table(
        name="german_suicides",
        label="Suicide by Age, Sex and Method",
        title="306 groupings of German suicides by age, by sex and by method",
        licence="GPL-2",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/vcd/Suicide.html",
        classes=(),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/vcd/Suicide.csv",
        header=True,
        fields=_GERMAN_SUICIDES_FIELDS,
        codes={
            "sex": ("female", "male"),
            "method": (
                "cookgas",
                "drown",
                "gun",
                "hang",
                "jump",
                "knife",
                "other",
                "poison",
                "toxicgas",
            ),
            "age_group": ("10-20", "25-35", "40-50", "55-65", "70-90"),
            "method2": ("drown", "gas", "gun", "hang", "jump", "knife", "other", "poison"),
        },
    ),
    "global_economy": Table(
        name="global_economy",
        label="The Global Economy",
        title="15,150 country-years of output, trade and population",
        licence="GPL-3",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/tsibbledata/global_economy.html",
        classes=(),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/tsibbledata/global_economy.csv",
        header=True,
        text_size=52,
        fields=_GLOBAL_ECONOMY_FIELDS,
    ),
    "gosset_yeast_cells": Table(
        name="gosset_yeast_cells",
        label="Gosset's Yeast Cell Counts",
        title="36 counts of yeast cells under a haemacytometer",
        licence="GPL",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/HistData/Yeast.html",
        classes=(),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/HistData/Yeast.csv",
        header=True,
        fields=_GOSSET_YEAST_CELLS_FIELDS,
        codes={
            "sample": ("A", "B", "C", "D"),
        },
    ),
    "government_transfers": Table(
        name="government_transfers",
        label="Government Cash Transfers",
        title="1,948 households either side of the cut-off for a cash transfer",
        licence="MIT",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/causaldata/gov_transfers.html",
        classes=(),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/causaldata/gov_transfers.csv",
        header=True,
        fields=_GOVERNMENT_TRANSFERS_FIELDS,
    ),
    "guerry_moral_statistics": Table(
        name="guerry_moral_statistics",
        label="Guerry's Moral Statistics of France",
        title="86 French departments and the moral statistics Guerry gathered in 1833",
        licence="GPL",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/HistData/Guerry.html",
        classes=(),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/HistData/Guerry.csv",
        header=True,
        text_size=19,
        fields=_GUERRY_MORAL_STATISTICS_FIELDS,
        codes={
            "region": ("C", "E", "N", "S", "W"),
            "maincity": ("1:Sm", "2:Med", "3:Lg"),
        },
    ),
    "hare_and_lynx_pelts": Table(
        name="hare_and_lynx_pelts",
        label="Hare and Lynx Pelts",
        title="91 years of pelts traded by the Hudson's Bay Company",
        licence="GPL-3",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/tsibbledata/pelt.html",
        classes=(),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/tsibbledata/pelt.csv",
        header=True,
        fields=_HARE_AND_LYNX_PELTS_FIELDS,
    ),
    "hepatocellular_carcinoma": Table(
        name="hepatocellular_carcinoma",
        label="Hepatocellular Carcinoma",
        title="227 liver cancer patients and how long each survived surgery",
        licence="CC0",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/asaur/hepatoCellular.html",
        classes=(),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/asaur/hepatoCellular.csv",
        header=True,
        fields=_HEPATOCELLULAR_CARCINOMA_FIELDS,
    ),
    "hiv_test_results": Table(
        name="hiv_test_results",
        label="Learning an HIV Test Result",
        title="4,820 Malawians offered a little money to come back for their result",
        licence="MIT",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/causaldata/thornton_hiv.html",
        classes=("stayed_away", "came_back", "not_recorded"),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/causaldata/thornton_hiv.csv",
        header=True,
        fields=_HIV_TEST_RESULTS_FIELDS,
        labels={"0": 0, "1": 1, "": 2},
    ),
    "household_budgets": Table(
        name="household_budgets",
        label="Household Budgets",
        title="88 country-years of what households owed, saved and spent",
        licence="GPL-3",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/tsibbledata/hh_budget.html",
        classes=(),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/tsibbledata/hh_budget.csv",
        header=True,
        fields=_HOUSEHOLD_BUDGETS_FIELDS,
        codes={
            "country": ("Australia", "Canada", "Japan", "USA"),
        },
    ),
    "indomethacin_trial": Table(
        name="indomethacin_trial",
        label="Indomethacin after an Endoscopy",
        title="602 patients given indomethacin or a placebo after an endoscopy",
        licence="MIT",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/medicaldata/indo_rct.html",
        classes=("no_pancreatitis", "pancreatitis"),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/medicaldata/indo_rct.csv",
        header=True,
        fields=_INDOMETHACIN_TRIAL_FIELDS,
        labels={"0_no": 0, "1_yes": 1},
        codes={
            "site": ("1_UM", "2_IU", "3_UK", "4_Case"),
            "gender": ("1_female", "2_male"),
            "sod": ("0_no", "1_yes"),
            "asa81": ("0_no", "1_yes", "NA_NA"),
            "status": ("0_inpatient", "1_outpatient"),
            "type": ("0_no SOD", "1_type 1", "2_type 2", "3_type 3"),
            "rx": ("0_placebo", "1_indomethacin"),
        },
    ),
    "infant_pneumonia": Table(
        name="infant_pneumonia",
        label="Infant Pneumonia",
        title="3,470 infants and how old each was when pneumonia struck",
        licence="GPL-3 or later",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/KMsurv/pneumon.html",
        classes=(),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/KMsurv/pneumon.csv",
        header=True,
        fields=_INFANT_PNEUMONIA_FIELDS,
    ),
    "intcal20_curve": Table(
        name="intcal20_curve",
        label="The IntCal20 Radiocarbon Curve",
        title="9,501 points of the curve radiocarbon dates are calibrated against",
        licence="GPL-3 or later",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/folio/intcal20.html",
        classes=(),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/folio/intcal20.csv",
        header=True,
        fields=_INTCAL20_CURVE_FIELDS,
    ),
    "interaction_triptych": Table(
        name="interaction_triptych",
        label="The Interaction Triptych",
        title="2,700 points of the three sets that show an interaction three ways",
        licence="MIT",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/quartets/interaction_triptych.html",
        classes=(),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/quartets/interaction_triptych.csv",
        header=True,
        fields=_INTERACTION_TRIPTYCH_FIELDS,
        codes={
            "dataset": (
                "(1) Ideal case",
                "(2) Floor effect, no latent interaction",
                "(3) Smaller correlation at largest slope",
            ),
            "moderator": ("high", "low", "medium"),
        },
    ),
    "iron_age_fibulae": Table(
        name="iron_age_fibulae",
        label="Iron Age Fibulae",
        title="30 brooches from an iron age cemetery, measured every way",
        licence="GPL-2 or later",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/archdata/Fibulae.html",
        classes=(),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/archdata/Fibulae.csv",
        header=True,
        text_size=9,
        fields=_IRON_AGE_FIBULAE_FIELDS,
    ),
    "iron_age_graves": Table(
        name="iron_age_graves",
        label="Early Iron Age Graves",
        title="52 graves in a Yorkshire cemetery and the goods found in each",
        licence="GPL-2 or later",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/archdata/EIAGraves.html",
        classes=(),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/archdata/EIAGraves.csv",
        header=True,
        fields=_IRON_AGE_GRAVES_FIELDS,
    ),
    "jevons_guesses": Table(
        name="jevons_guesses",
        label="Jevons on Guessing Numbers",
        title="50 guesses at how many beans had been thrown and how far each was out",
        licence="GPL",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/HistData/Jevons.html",
        classes=(),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/HistData/Jevons.csv",
        header=True,
        fields=_JEVONS_GUESSES_FIELDS,
    ),
    "kidney_transplant": Table(
        name="kidney_transplant",
        label="Kidney Transplant Survival",
        title="863 kidney transplant patients and how long each lived after surgery",
        licence="GPL-3 or later",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/KMsurv/kidtran.html",
        classes=(),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/KMsurv/kidtran.csv",
        header=True,
        fields=_KIDNEY_TRANSPLANT_FIELDS,
    ),
    "kommos_pottery": Table(
        name="kommos_pottery",
        label="Kommos Pottery Chemistry",
        title="88 pots from Bronze Age Crete assayed and sorted by ware",
        licence="GPL-3 or later",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/folio/kommos.html",
        classes=("cj", "ej", "sna", "tsj"),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/folio/kommos.csv",
        header=True,
        text_size=5,
        fields=_KOMMOS_POTTERY_FIELDS,
        labels={"CJ": 0, "EJ": 1, "SNA": 2, "TSJ": 3},
        codes={
            "date_code": (
                "LM IA Final",
                "LM IB",
                "LM IB Early",
                "LM II",
                "LM IIIA",
                "LM IIIA1",
                "LM IIIA2",
                "LM IIIA2 Early",
                "LM IIIB",
                "historic levels",
            ),
        },
    ),
    "laryngoscope_trial": Table(
        name="laryngoscope_trial",
        label="Two Laryngoscopes Compared",
        title="99 intubations and how long each took from start to finish",
        licence="MIT",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/medicaldata/laryngoscope.html",
        classes=(),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/medicaldata/laryngoscope.csv",
        header=True,
        fields=_LARYNGOSCOPE_TRIAL_FIELDS,
    ),
    "larynx_cancer": Table(
        name="larynx_cancer",
        label="Larynx Cancer Survival",
        title="90 men with cancer of the larynx and how long each lived",
        licence="GPL-3 or later",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/KMsurv/larynx.html",
        classes=(),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/KMsurv/larynx.csv",
        header=True,
        fields=_LARYNX_CANCER_FIELDS,
    ),
    "law_dome_gases": Table(
        name="law_dome_gases",
        label="Law Dome Greenhouse Gases",
        title="2,004 years of greenhouse gas read out of Antarctic ice",
        licence="GPL-3 or later",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/folio/law2006.html",
        classes=(),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/folio/law2006.csv",
        header=True,
        fields=_LAW_DOME_GASES_FIELDS,
    ),
    "letters_to_politicians": Table(
        name="letters_to_politicians",
        label="Letters to Black Politicians",
        title="5,593 American legislators written to and which of them wrote back",
        licence="MIT",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/causaldata/black_politicians.html",
        classes=("no_reply", "replied"),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/causaldata/black_politicians.csv",
        header=True,
        fields=_LETTERS_TO_POLITICIANS_FIELDS,
        labels={"0": 0, "1": 1},
    ),
    "licorice_gargle": Table(
        name="licorice_gargle",
        label="Liquorice Gargle before Surgery",
        title="235 patients gargling before surgery and how sore each throat was after",
        licence="MIT",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/medicaldata/licorice_gargle.html",
        classes=(),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/medicaldata/licorice_gargle.csv",
        header=True,
        fields=_LICORICE_GARGLE_FIELDS,
    ),
    "london_cholera_districts": Table(
        name="london_cholera_districts",
        label="London Cholera by District",
        title="38 London districts and how many in each died of cholera in 1849",
        licence="GPL",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/HistData/Cholera.html",
        classes=(),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/HistData/Cholera.csv",
        header=True,
        text_size=44,
        fields=_LONDON_CHOLERA_DISTRICTS_FIELDS,
        codes={
            "region": ("Central", "Kent", "North", "South", "West"),
            "water": ("Battersea", "Kew", "New River"),
        },
    ),
    "long_stay_patients": Table(
        name="long_stay_patients",
        label="Long Stay Hospital Patients",
        title="768 hospital admissions and whether the patient ended up stranded",
        licence="MIT",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/MLDataR/long_stayers.html",
        classes=("not_stranded", "stranded"),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/MLDataR/long_stayers.csv",
        header=True,
        text_size=10,
        fields=_LONG_STAY_PATIENTS_FIELDS,
        labels={"Not Stranded": 0, "Stranded": 1},
        codes={
            "frailty_index": (
                "Activity Limitation",
                "Fall patient history",
                "Mobility problems",
                "No index item",
            ),
        },
    ),
    "macdonell_criminals": Table(
        name="macdonell_criminals",
        label="Macdonell's Criminals",
        title="924 pairings of height and finger length among three thousand criminals",
        licence="GPL",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/HistData/Macdonell.html",
        classes=(),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/HistData/Macdonell.csv",
        header=True,
        fields=_MACDONELL_CRIMINALS_FIELDS,
    ),
    "medicare_stays": Table(
        name="medicare_stays",
        label="Medicare Hospital Stays",
        title="1,495 Medicare patients in Arizona and how many days each stayed",
        licence="GPL-2",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/COUNT/medpar.html",
        classes=(),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/COUNT/medpar.csv",
        header=True,
        fields=_MEDICARE_STAYS_FIELDS,
    ),
    "medieval_glass": Table(
        name="medieval_glass",
        label="Medieval Glass from France",
        title="398 pieces of medieval glass assayed for what it was made of",
        licence="GPL-3 or later",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/folio/verre.html",
        classes=(),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/folio/verre.csv",
        header=True,
        text_size=10,
        fields=_MEDIEVAL_GLASS_FIELDS,
        codes={
            "site": (
                "ANG",
                "BER",
                "BIN",
                "CHE",
                "CHL",
                "CHM",
                "CNL",
                "MEA",
                "MET",
                "MIT",
                "OMO",
                "ORL",
                "PAI",
                "POI",
                "ROU",
            ),
            "type": (
                "Aiguière",
                "Aiguière?",
                "Apothecary jar",
                "Apothecary jar?",
                "Bottle",
                "Bottle?",
                "Case bottle",
                "Dist. aparatus",
                "Flask",
                "Flask?",
                "Flattened flask",
                "Footed cup",
                "Goblet",
                "Goblet?",
                "Gourde",
                "Lid",
                "Pitcher?",
                "Plate",
                "Tumbler",
                "Vase",
                "Window glass",
                "Work dropping",
            ),
            "age": (
                "10-12",
                "11-12",
                "13",
                "13-14",
                "13?",
                "14",
                "14-15",
                "15",
                "15-16",
                "15?",
                "16",
                "16-17",
                "17",
                "9-10?",
            ),
            "periode": ("I", "I?", "II", "II-III?", "III", "IV"),
            "tint": (
                "B",
                "B-CL",
                "CL",
                "CL*b",
                "CL*w",
                "CL?*w",
                "CLgy",
                "I*w",
                "Marbled",
                "Millefiori",
                "PB*b",
                "PGE",
                "PGE*b",
                "PGE*b*r",
                "PGE*bl",
                "PGE*w",
                "PGE-B",
                "PGY-B",
                "R",
                "W",
                "W*b*av",
                "W*b*r",
            ),
        },
    ),
    "mesolithic_tools": Table(
        name="mesolithic_tools",
        label="Mesolithic Tool Counts",
        title="33 Mesolithic assemblages and the tools counted in each",
        licence="GPL-2 or later",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/archdata/Mesolithic.html",
        classes=(),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/archdata/Mesolithic.csv",
        header=True,
        fields=_MESOLITHIC_TOOLS_FIELDS,
    ),
    "michelsberg_pottery": Table(
        name="michelsberg_pottery",
        label="Michelsberg Pottery",
        title="109 Neolithic assemblages and which Michelsberg phase each belongs to",
        licence="GPL-2 or later",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/archdata/Michelsberg.html",
        classes=("i", "i_ii", "ii", "ii_iii", "iii", "iii_v", "iii_iv", "iv", "iv_v", "munz", "v"),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/archdata/Michelsberg.csv",
        header=True,
        text_size=22,
        fields=_MICHELSBERG_POTTERY_FIELDS,
        labels={
            "I": 0,
            "I/II": 1,
            "II": 2,
            "II/III": 3,
            "III": 4,
            "III-V": 5,
            "III/IV": 6,
            "IV": 7,
            "IV/V": 8,
            "Munz": 9,
            "V": 10,
        },
    ),
    "minard_troops": Table(
        name="minard_troops",
        label="Minard's March on Moscow",
        title="51 points along Napoleon's march and how many men were still alive",
        licence="GPL",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/HistData/Minard.troops.html",
        classes=(),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/HistData/Minard.troops.csv",
        header=True,
        fields=_MINARD_TROOPS_FIELDS,
        codes={
            "direction": ("A", "R"),
        },
    ),
    "mississippi_pottery": Table(
        name="mississippi_pottery",
        label="Mississippi Pottery Types",
        title="20 sites on the Mississippi and the pottery types found at each",
        licence="GPL-3 or later",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/folio/mississippi.html",
        classes=(),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/folio/mississippi.csv",
        header=True,
        text_size=11,
        fields=_MISSISSIPPI_POTTERY_FIELDS,
    ),
    "ngrip_ice_core": Table(
        name="ngrip_ice_core",
        label="The NGRIP Greenland Ice Core",
        title="6,114 readings of oxygen isotopes down a Greenland ice core",
        licence="GPL-3 or later",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/folio/ngrip2010.html",
        classes=(),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/folio/ngrip2010.csv",
        header=True,
        fields=_NGRIP_ICE_CORE_FIELDS,
    ),
    "nightingale_mortality": Table(
        name="nightingale_mortality",
        label="Nightingale's Crimean Mortality",
        title="24 months of the Crimean war and what the British army died of",
        licence="GPL",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/HistData/Nightingale.html",
        classes=(),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/HistData/Nightingale.csv",
        header=True,
        dates="%Y-%m-%d",
        fields=_NIGHTINGALE_MORTALITY_FIELDS,
        codes={
            "month": (
                "Apr",
                "Aug",
                "Dec",
                "Feb",
                "Jan",
                "Jul",
                "Jun",
                "Mar",
                "May",
                "Nov",
                "Oct",
                "Sep",
            ),
        },
    ),
    "olympic_running": Table(
        name="olympic_running",
        label="Olympic Running Finals",
        title="312 Olympic running finals and how fast each was won",
        licence="GPL-3",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/tsibbledata/olympic_running.html",
        classes=(),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/tsibbledata/olympic_running.csv",
        header=True,
        fields=_OLYMPIC_RUNNING_FIELDS,
        codes={
            "sex": ("men", "women"),
        },
    ),
    "organ_donations": Table(
        name="organ_donations",
        label="Organ Donor Registration",
        title="162 state-quarters of organ donor registration in America",
        licence="MIT",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/causaldata/organ_donations.html",
        classes=(),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/causaldata/organ_donations.csv",
        header=True,
        text_size=20,
        fields=_ORGAN_DONATIONS_FIELDS,
        codes={
            "quarter": ("Q12011", "Q12012", "Q22011", "Q32011", "Q42010", "Q42011"),
        },
    ),
    "oxford_pottery": Table(
        name="oxford_pottery",
        label="Oxford and New Forest Pottery",
        title="30 Romano-British sites and the share of pottery from the Oxford kilns",
        licence="GPL-2 or later",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/archdata/OxfordPots.html",
        classes=(),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/archdata/OxfordPots.csv",
        header=True,
        text_size=20,
        fields=_OXFORD_POTTERY_FIELDS,
    ),
    "ozone_and_weather": Table(
        name="ozone_and_weather",
        label="Ozone and the Weather",
        title="111 summer days in New York and how much ozone hung in the air",
        licence="GPL-2 or later",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/lattice/environmental.html",
        classes=(),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/lattice/environmental.csv",
        header=True,
        fields=_OZONE_AND_WEATHER_FIELDS,
    ),
    "paris_registrations": Table(
        name="paris_registrations",
        label="Parisian Registrations",
        title="516 months of nineteenth-century Paris and how many women registered",
        licence="GPL",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/HistData/Prostitutes.html",
        classes=(),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/HistData/Prostitutes.csv",
        header=True,
        dates="%Y-%m-%d",
        fields=_PARIS_REGISTRATIONS_FIELDS,
        codes={
            "month": (
                "Apr",
                "Aug",
                "Dec",
                "Feb",
                "Jan",
                "Jul",
                "Jun",
                "Mar",
                "May",
                "Nov",
                "Oct",
                "Sep",
            ),
        },
    ),
    "pearson_lee_heights": Table(
        name="pearson_lee_heights",
        label="Pearson and Lee's Family Heights",
        title="746 pairings of parent and child height in Edwardian families",
        licence="GPL",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/HistData/PearsonLee.html",
        classes=(),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/HistData/PearsonLee.csv",
        header=True,
        fields=_PEARSON_LEE_HEIGHTS_FIELDS,
        codes={
            "gp": ("fd", "fs", "md", "ms"),
            "par": ("Father", "Mother"),
            "chl": ("Daughter", "Son"),
        },
    ),
    "plant_carbon_isotopes": Table(
        name="plant_carbon_isotopes",
        label="Carbon Isotopes in Plants",
        title="155 plants and whether each fixes carbon the C3 way or the C4 way",
        licence="GPL-3 or later",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/folio/vegetation.html",
        classes=("c3", "c4"),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/folio/vegetation.csv",
        header=True,
        text_size=26,
        fields=_PLANT_CARBON_ISOTOPES_FIELDS,
        labels={"C3": 0, "C4": 1},
        codes={
            "country": ("Argentina", "Kenya", "Mongolia", "Zaire"),
        },
    ),
    "plant_traits": Table(
        name="plant_traits",
        label="Life History Traits of Plants",
        title="136 plants of north-west France and the traits each one carries",
        licence="GPL-2 or later",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/cluster/plantTraits.html",
        classes=(),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/cluster/plantTraits.csv",
        header=True,
        text_size=5,
        fields=_PLANT_TRAITS_FIELDS,
    ),
    "playfair_wheat": Table(
        name="playfair_wheat",
        label="Playfair's Wheat and Wages",
        title="53 years of the price of wheat set against a labourer's wage",
        licence="GPL",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/HistData/Wheat.html",
        classes=(),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/HistData/Wheat.csv",
        header=True,
        fields=_PLAYFAIR_WHEAT_FIELDS,
    ),
    "portal_rodents": Table(
        name="portal_rodents",
        label="Portal Project Rodent Survey",
        title="35,549 animals trapped in the Arizona desert and what each turned out to be",
        licence="CC0",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/ratdat/complete.html",
        classes=("bird", "rabbit", "reptile", "rodent", "not_recorded"),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/ratdat/complete.csv",
        header=True,
        text_size=16,
        fields=_PORTAL_RODENTS_FIELDS,
        labels={"Bird": 0, "Rabbit": 1, "Reptile": 2, "Rodent": 3, "": 4},
        codes={
            "sex": ("F", "M"),
            "plot_type": (
                "Control",
                "Long-term Krat Exclosure",
                "Rodent Exclosure",
                "Short-term Krat Exclosure",
                "Spectab exclosure",
            ),
        },
    ),
    "portal_species": Table(
        name="portal_species",
        label="Portal Project Species List",
        title="54 species trapped in the Arizona desert and what kind of animal each is",
        licence="CC0",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/ratdat/species.html",
        classes=("bird", "rabbit", "reptile", "rodent"),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/ratdat/species.csv",
        header=True,
        text_size=16,
        fields=_PORTAL_SPECIES_FIELDS,
        labels={"Bird": 0, "Rabbit": 1, "Reptile": 2, "Rodent": 3},
    ),
    "prediabetes": Table(
        name="prediabetes",
        label="From Prediabetes to Diabetes",
        title="3,059 patients with prediabetes and how long each took to develop diabetes",
        licence="MIT",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/MLDataR/PreDiabetes.html",
        classes=(),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/MLDataR/PreDiabetes.csv",
        header=True,
        fields=_PREDIABETES_FIELDS,
    ),
    "prostate_survival": Table(
        name="prostate_survival",
        label="Prostate Cancer Survival",
        title="14,294 men with prostate cancer and how long each lived after diagnosis",
        licence="CC0",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/asaur/prostateSurvival.html",
        classes=(),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/asaur/prostateSurvival.csv",
        header=True,
        fields=_PROSTATE_SURVIVAL_FIELDS,
        codes={
            "grade": ("mode", "poor"),
            "stage": ("T1ab", "T1c", "T2"),
            "agegroup": ("66-69", "70-74", "75-79", "80+"),
        },
    ),
    "prussian_horse_kicks": Table(
        name="prussian_horse_kicks",
        label="Deaths by Horse Kick",
        title="280 corps-years of the Prussian army and how many horses kicked dead",
        licence="GPL-2",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/vcd/VonBort.html",
        classes=(),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/vcd/VonBort.csv",
        header=True,
        fields=_PRUSSIAN_HORSE_KICKS_FIELDS,
        codes={
            "corps": (
                "G",
                "I",
                "II",
                "III",
                "IV",
                "IX",
                "V",
                "VI",
                "VII",
                "VIII",
                "X",
                "XI",
                "XIV",
                "XV",
            ),
            "fisher": ("no", "yes"),
        },
    ),
    "rashomon_quartet": Table(
        name="rashomon_quartet",
        label="The Rashomon Quartet",
        title="2,000 points of the set four different models fit equally well",
        licence="MIT",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/quartets/rashomon_quartet.html",
        classes=(),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/quartets/rashomon_quartet.csv",
        header=True,
        fields=_RASHOMON_QUARTET_FIELDS,
        codes={
            "split": ("test", "train"),
        },
    ),
    "repeat_victimisation": Table(
        name="repeat_victimisation",
        label="Repeat Victimisation",
        title="64 pairings of a first crime and a second against the same person",
        licence="GPL-2",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/vcd/RepVict.html",
        classes=(),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/vcd/RepVict.csv",
        header=True,
        fields=_REPEAT_VICTIMISATION_FIELDS,
        codes={
            "first_victimization": (
                "Assault",
                "Auto Theft",
                "Burglary",
                "Household Larceny",
                "Personal Larcency",
                "Pickpocket",
                "Rape",
                "Robbery",
            ),
        },
    ),
    "republican_vote_share": Table(
        name="republican_vote_share",
        label="Republican Vote by State",
        title="50 American states and the Republican share at every election since 1856",
        licence="GPL-2 or later",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/cluster/votes.repub.html",
        classes=(),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/cluster/votes.repub.csv",
        header=True,
        text_size=14,
        fields=_REPUBLICAN_VOTE_SHARE_FIELDS,
    ),
    "restaurant_inspections": Table(
        name="restaurant_inspections",
        label="Restaurant Inspections",
        title="27,178 restaurant inspections and what each one scored",
        licence="MIT",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/causaldata/restaurant_inspections.html",
        classes=(),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/causaldata/restaurant_inspections.csv",
        header=True,
        text_size=72,
        fields=_RESTAURANT_INSPECTIONS_FIELDS,
        codes={
            "weekend": ("FALSE", "TRUE"),
        },
    ),
    "rice_farmer_insurance": Table(
        name="rice_farmer_insurance",
        label="Weather Insurance for Rice Farmers",
        title="1,410 Chinese rice farmers and whether each took the insurance offered",
        licence="MIT",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/causaldata/social_insure.html",
        classes=("declined", "took_it_up"),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/causaldata/social_insure.csv",
        header=True,
        text_size=22,
        fields=_RICE_FARMER_INSURANCE_FIELDS,
        labels={"0": 0, "1": 1},
    ),
    "rochdale_women": Table(
        name="rochdale_women",
        label="Women's Employment in Rochdale",
        title="256 groupings of Rochdale women by whether each held a job",
        licence="GPL-2",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/vcd/Rochdale.html",
        classes=(),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/vcd/Rochdale.csv",
        header=True,
        fields=_ROCHDALE_WOMEN_FIELDS,
        codes={
            "econactive": ("no", "yes"),
            "age": ("<38", ">38"),
        },
    ),
    "roman_street_networks": Table(
        name="roman_street_networks",
        label="Roman City Street Networks",
        title="125 Roman cities and how much of each was given over to streets",
        licence="GPL-3 or later",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/folio/cities.html",
        classes=(),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/folio/cities.csv",
        header=True,
        text_size=26,
        fields=_ROMAN_STREET_NETWORKS_FIELDS,
    ),
    "romano_british_glass": Table(
        name="romano_british_glass",
        label="Romano-British Glass",
        title="105 pieces of Roman glass assayed and traced to the town that made it",
        licence="GPL-2 or later",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/archdata/RBGlass1.html",
        classes=("leicester", "mancetter"),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/archdata/RBGlass1.csv",
        header=True,
        fields=_ROMANO_BRITISH_GLASS_FIELDS,
        labels={"Leicester": 0, "Mancetter": 1},
    ),
    "romano_british_pottery": Table(
        name="romano_british_pottery",
        label="Romano-British Pottery",
        title="48 Roman pots assayed and traced to the kiln that fired them",
        licence="GPL-2 or later",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/archdata/RBPottery.html",
        classes=("ashley_rails", "caldicot", "gloucester", "islands_thorns", "llanedeyrn"),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/archdata/RBPottery.csv",
        header=True,
        text_size=3,
        fields=_ROMANO_BRITISH_POTTERY_FIELDS,
        labels={
            "Ashley Rails": 0,
            "Caldicot": 1,
            "Gloucester": 2,
            "Islands Thorns": 3,
            "Llanedeyrn": 4,
        },
        codes={
            "region": ("Gloucester", "New Forest", "Wales"),
        },
    ),
    "ruspini_points": Table(
        name="ruspini_points",
        label="Ruspini's Clustering Points",
        title="75 points in the plane that fall into four clusters",
        licence="GPL-2 or later",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/cluster/ruspini.html",
        classes=(),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/cluster/ruspini.csv",
        header=True,
        fields=_RUSPINI_POINTS_FIELDS,
    ),
    "sea_level_reconstruction": Table(
        name="sea_level_reconstruction",
        label="Sea Level over Eight Hundred Thousand Years",
        title="799 points of sea level reconstructed across the last ice ages",
        licence="GPL-3 or later",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/folio/spratt2016.html",
        classes=(),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/folio/spratt2016.csv",
        header=True,
        fields=_SEA_LEVEL_RECONSTRUCTION_FIELDS,
    ),
    "ship_damage": Table(
        name="ship_damage",
        label="Damage to Cargo Ships",
        title="40 groupings of cargo ships and how many came to harm",
        licence="GPL-2",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/COUNT/ships.html",
        classes=(),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/COUNT/ships.csv",
        header=True,
        fields=_SHIP_DAMAGE_FIELDS,
    ),
    "singapore_car_claims": Table(
        name="singapore_car_claims",
        label="Singapore Automobile Claims",
        title="7,483 Singaporean car policies and how many claims each one made",
        licence="GPL-2",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/insuranceData/SingaporeAuto.html",
        classes=(),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/insuranceData/SingaporeAuto.csv",
        header=True,
        fields=_SINGAPORE_CAR_CLAIMS_FIELDS,
        codes={
            "sexinsured": ("F", "M", "U"),
            "vehicletype": ("A", "G", "M", "P", "Q", "S", "T", "W", "Z"),
        },
    ),
    "smartpill_motility": Table(
        name="smartpill_motility",
        label="SmartPill Gut Motility",
        title="95 readings from a pill swallowed to time the gut",
        licence="MIT",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/medicaldata/smartpill.html",
        classes=(),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/medicaldata/smartpill.csv",
        header=True,
        fields=_SMARTPILL_MOTILITY_FIELDS,
    ),
    "smoking_cessation": Table(
        name="smoking_cessation",
        label="Smoking Cessation Trial",
        title="125 smokers given a patch or a combination and how long each held out",
        licence="CC0",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/asaur/pharmacoSmoking.html",
        classes=(),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/asaur/pharmacoSmoking.csv",
        header=True,
        fields=_SMOKING_CESSATION_FIELDS,
        codes={
            "grp": ("combination", "patchOnly"),
            "gender": ("Female", "Male"),
            "race": ("black", "hispanic", "other", "white"),
            "employment": ("ft", "other", "pt"),
            "levelsmoking": ("heavy", "light"),
            "agegroup2": ("21-49", "50+"),
            "agegroup4": ("21-34", "35-49", "50-64", "65+"),
        },
    ),
    "snodgrass_houses": Table(
        name="snodgrass_houses",
        label="Snodgrass House Pits",
        title="91 house pits at Snodgrass and whether each stood inside the wall",
        licence="GPL-2 or later",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/archdata/Snodgrass.html",
        classes=("inside", "outside"),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/archdata/Snodgrass.csv",
        header=True,
        text_size=3,
        fields=_SNODGRASS_HOUSES_FIELDS,
        labels={"Inside": 0, "Outside": 1},
    ),
    "snow_cholera_deaths": Table(
        name="snow_cholera_deaths",
        label="Snow's Cholera Deaths",
        title="578 deaths in the Broad Street outbreak, each placed on Snow's map",
        licence="GPL",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/HistData/Snow.deaths.html",
        classes=(),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/HistData/Snow.deaths.csv",
        header=True,
        fields=_SNOW_CHOLERA_DEATHS_FIELDS,
    ),
    "std_reinfection": Table(
        name="std_reinfection",
        label="Sexually Transmitted Disease Reinfection",
        title="877 patients and how long each went before a second infection",
        licence="GPL-3 or later",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/KMsurv/std.html",
        classes=(),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/KMsurv/std.csv",
        header=True,
        fields=_STD_REINFECTION_FIELDS,
        codes={
            "race": ("B", "W"),
            "marital": ("D", "M", "S"),
        },
    ),
    "stone_age_sites": Table(
        name="stone_age_sites",
        label="Early Stone Age Sites",
        title="43 Danish stone age sites and the tools found at each",
        licence="GPL-2 or later",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/archdata/ESASites.html",
        classes=(),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/archdata/ESASites.csv",
        header=True,
        fields=_STONE_AGE_SITES_FIELDS,
    ),
    "streptomycin_tuberculosis": Table(
        name="streptomycin_tuberculosis",
        label="Streptomycin for Tuberculosis",
        title="107 patients in the first randomised trial and whether each improved",
        licence="MIT",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/medicaldata/strep_tb.html",
        classes=("no_better", "improved"),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/medicaldata/strep_tb.csv",
        header=True,
        fields=_STREPTOMYCIN_TUBERCULOSIS_FIELDS,
        labels={"FALSE": 0, "TRUE": 1},
        codes={
            "arm": ("Control", "Streptomycin"),
            "gender": ("F", "M"),
            "baseline_condition": ("1_Good", "2_Fair", "3_Poor"),
            "baseline_temp": ("1_98-98.9F", "2_99-99.9F", "3_100-100.9F", "4_100F+"),
            "baseline_esr": ("2_11-20", "3_21-50", "4_51+"),
            "baseline_cavitation": ("no", "yes"),
            "strep_resistance": ("1_sens_0-8", "2_mod_8-99", "3_resist_100+"),
            "radiologic_6m": (
                "1_Death",
                "2_Considerable_deterioration",
                "3_Moderate_deterioration",
                "4_No_change",
                "5_Moderate_improvement",
                "6_Considerable_improvement",
            ),
        },
    ),
    "stroke_classification": Table(
        name="stroke_classification",
        label="Stroke Classification",
        title="5,110 patients and whether each went on to have a stroke",
        licence="MIT",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/MLDataR/stroke_classification.html",
        classes=("no_stroke", "stroke"),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/MLDataR/stroke_classification.csv",
        header=True,
        fields=_STROKE_CLASSIFICATION_FIELDS,
        labels={"0": 0, "1": 1},
        codes={
            "gender": ("Female", "Male", "Other"),
        },
    ),
    "supported_work_programme": Table(
        name="supported_work_programme",
        label="The National Supported Work Programme",
        title="445 people in a job training trial and what each earned afterwards",
        licence="MIT",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/causaldata/nsw_mixtape.html",
        classes=(),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/causaldata/nsw_mixtape.csv",
        header=True,
        fields=_SUPPORTED_WORK_PROGRAMME_FIELDS,
        codes={
            "data_id": ("Dehejia-Wahba Sample",),
        },
    ),
    "supraclavicular_block": Table(
        name="supraclavicular_block",
        label="Supraclavicular Nerve Block",
        title="103 nerve blocks and how long each took to numb the arm",
        licence="MIT",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/medicaldata/supraclavicular.html",
        classes=(),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/medicaldata/supraclavicular.csv",
        header=True,
        fields=_SUPRACLAVICULAR_BLOCK_FIELDS,
    ),
    "swedish_motorcycles": Table(
        name="swedish_motorcycles",
        label="Swedish Motorcycle Insurance",
        title="64,548 Swedish motorcycle policies and what each claimed",
        licence="GPL-2",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/insuranceData/dataOhlsson.html",
        classes=(),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/insuranceData/dataOhlsson.csv",
        header=True,
        fields=_SWEDISH_MOTORCYCLES_FIELDS,
        codes={
            "kon": ("K", "M"),
        },
    ),
    "texas_prisons": Table(
        name="texas_prisons",
        label="Texas Prison Construction",
        title="816 state-years of prison building and who was locked up in them",
        licence="MIT",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/causaldata/texas.html",
        classes=(),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/causaldata/texas.csv",
        header=True,
        text_size=20,
        fields=_TEXAS_PRISONS_FIELDS,
    ),
    "tongue_cancer": Table(
        name="tongue_cancer",
        label="Tongue Cancer Survival",
        title="80 tongue cancer patients and how long each lived after diagnosis",
        licence="GPL-3 or later",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/KMsurv/tongue.html",
        classes=(),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/KMsurv/tongue.csv",
        header=True,
        fields=_TONGUE_CANCER_FIELDS,
    ),
    "trial_of_the_pyx": Table(
        name="trial_of_the_pyx",
        label="The Trial of the Pyx",
        title="72 weighings of coin from the royal mint and how far each strayed",
        licence="GPL",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/HistData/Pyx.html",
        classes=(),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/HistData/Pyx.csv",
        header=True,
        fields=_TRIAL_OF_THE_PYX_FIELDS,
        codes={
            "bags": ("1 and 2", "10", "3", "4", "5", "6", "7", "8", "9"),
            "group": ("above std", "below std", "near std"),
            "deviation": (
                "(-.1 to 0)",
                "(-.2 to -.l)",
                "(-R to -.2)",
                "(.1 to .2)",
                "(.2 to R)",
                "(0 to .l)",
                "Above R",
                "Below -R",
            ),
        },
    ),
    "us_regional_mortality": Table(
        name="us_regional_mortality",
        label="Mortality by American Region",
        title="400 death rates by region, cause, sex and town or country",
        licence="GPL-2 or later",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/lattice/USRegionalMortality.html",
        classes=(),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/lattice/USRegionalMortality.csv",
        header=True,
        fields=_US_REGIONAL_MORTALITY_FIELDS,
        codes={
            "region": (
                "HHS Region 01",
                "HHS Region 02",
                "HHS Region 03",
                "HHS Region 04",
                "HHS Region 05",
                "HHS Region 06",
                "HHS Region 07",
                "HHS Region 08",
                "HHS Region 09",
                "HHS Region 10",
            ),
            "status": ("Rural", "Urban"),
            "sex": ("Female", "Male"),
            "cause": (
                "Alzheimers",
                "Cancer",
                "Cerebrovascular diseases",
                "Diabetes",
                "Flu and pneumonia",
                "Heart disease",
                "Lower respiratory",
                "Nephritis",
                "Suicide",
                "Unintentional injuries",
            ),
        },
    ),
    "victorian_electricity": Table(
        name="victorian_electricity",
        label="Victorian Electricity Demand",
        title="52,608 half-hours of electricity demand in Victoria, and how warm it was",
        licence="GPL-3",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/tsibbledata/vic_elec.html",
        classes=(),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/tsibbledata/vic_elec.csv",
        header=True,
        dates="%Y-%m-%dT%H:%M:%SZ",
        text_size=10,
        fields=_VICTORIAN_ELECTRICITY_FIELDS,
        codes={
            "holiday": ("FALSE", "TRUE"),
        },
    ),
    "virgil_dactyls": Table(
        name="virgil_dactyls",
        label="Dactyls in Virgil's Aeneid",
        title="60 counts of how often each foot of a hexameter line was a dactyl",
        licence="GPL",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/HistData/Dactyl.html",
        classes=(),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/HistData/Dactyl.csv",
        header=True,
        fields=_VIRGIL_DACTYLS_FIELDS,
        codes={
            "lines": (
                "11:15",
                "16:20",
                "1:5",
                "21:25",
                "26:30",
                "31:35",
                "36:40",
                "41:45",
                "46:50",
                "51:55",
                "56:60",
                "61:65",
                "66:70",
                "6:10",
                "71:75",
            ),
        },
    ),
    "woodland_birds": Table(
        name="woodland_birds",
        label="Birds of Three Woods",
        title="35 bird species and how many of each were counted in three woods",
        licence="GPL-3 or later",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/folio/birds.html",
        classes=(),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/folio/birds.csv",
        header=True,
        text_size=18,
        fields=_WOODLAND_BIRDS_FIELDS,
    ),
    "workers_compensation": Table(
        name="workers_compensation",
        label="Workers Compensation Losses",
        title="847 workers compensation losses, by class of work and by year",
        licence="GPL-2",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/insuranceData/WorkersComp.html",
        classes=(),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/insuranceData/WorkersComp.csv",
        header=True,
        fields=_WORKERS_COMPENSATION_FIELDS,
    ),
    "xclara_clusters": Table(
        name="xclara_clusters",
        label="Three Well-Separated Clusters",
        title="3,000 points in the plane that fall into three clusters",
        licence="GPL-2 or later",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/cluster/xclara.html",
        classes=(),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/cluster/xclara.csv",
        header=True,
        fields=_XCLARA_CLUSTERS_FIELDS,
    ),
    "yule_pauperism": Table(
        name="yule_pauperism",
        label="Yule on Pauperism",
        title="32 English districts and how poor relief moved with pauperism",
        licence="MIT",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/causaldata/yule.html",
        classes=(),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/causaldata/yule.csv",
        header=True,
        text_size=19,
        fields=_YULE_PAUPERISM_FIELDS,
    ),
    "zuni_pottery": Table(
        name="zuni_pottery",
        label="Zuni Pottery Counts",
        title="420 rooms of a Zuni pueblo and the wares found in each",
        licence="GPL-3 or later",
        source="https://vincentarelbundock.github.io/Rdatasets/doc/folio/zuni.html",
        classes=(),
        url="https://vincentarelbundock.github.io/Rdatasets/csv/folio/zuni.csv",
        header=True,
        text_size=7,
        fields=_ZUNI_POTTERY_FIELDS,
    ),
    "fossil_electricity_share": Table(
        name="fossil_electricity_share",
        label="Fossil Share of Electricity",
        title="7,182 country-years and the share of electricity burnt out of fossil fuels",
        licence="CC BY 4.0",
        source="https://ourworldindata.org/grapher/share-electricity-fossil-fuels",
        classes=(),
        url="https://ourworldindata.org/grapher/share-electricity-fossil-fuels.csv?v=1&csvType=full&useColumnShortNames=true",
        header=True,
        text_size=35,
        fields=_FOSSIL_ELECTRICITY_SHARE_FIELDS,
    ),
    "nuclear_electricity_share": Table(
        name="nuclear_electricity_share",
        label="Nuclear Share of Electricity",
        title="7,718 country-years and the share of electricity split out of atoms",
        licence="CC BY 4.0",
        source="https://ourworldindata.org/grapher/share-electricity-nuclear",
        classes=(),
        url="https://ourworldindata.org/grapher/share-electricity-nuclear.csv?v=1&csvType=full&useColumnShortNames=true",
        header=True,
        text_size=35,
        fields=_NUCLEAR_ELECTRICITY_SHARE_FIELDS,
    ),
    "wind_electricity_share": Table(
        name="wind_electricity_share",
        label="Wind Share of Electricity",
        title="7,661 country-years and the share of electricity taken from the wind",
        licence="CC BY 4.0",
        source="https://ourworldindata.org/grapher/share-electricity-wind",
        classes=(),
        url="https://ourworldindata.org/grapher/share-electricity-wind.csv?v=1&csvType=full&useColumnShortNames=true",
        header=True,
        text_size=35,
        fields=_WIND_ELECTRICITY_SHARE_FIELDS,
    ),
    "solar_electricity_share": Table(
        name="solar_electricity_share",
        label="Solar Share of Electricity",
        title="7,869 country-years and the share of electricity taken from the sun",
        licence="CC BY 4.0",
        source="https://ourworldindata.org/grapher/share-electricity-solar",
        classes=(),
        url="https://ourworldindata.org/grapher/share-electricity-solar.csv?v=1&csvType=full&useColumnShortNames=true",
        header=True,
        text_size=35,
        fields=_SOLAR_ELECTRICITY_SHARE_FIELDS,
    ),
    "hydro_electricity_share": Table(
        name="hydro_electricity_share",
        label="Hydro Share of Electricity",
        title="7,777 country-years and the share of electricity taken from falling water",
        licence="CC BY 4.0",
        source="https://ourworldindata.org/grapher/share-electricity-hydro",
        classes=(),
        url="https://ourworldindata.org/grapher/share-electricity-hydro.csv?v=1&csvType=full&useColumnShortNames=true",
        header=True,
        text_size=35,
        fields=_HYDRO_ELECTRICITY_SHARE_FIELDS,
    ),
    "electricity_per_person": Table(
        name="electricity_per_person",
        label="Electricity Generated per Person",
        title="7,071 country-years of electricity generated for every person living there",
        licence="CC BY 4.0",
        source="https://ourworldindata.org/grapher/per-capita-electricity-generation",
        classes=(),
        url="https://ourworldindata.org/grapher/per-capita-electricity-generation.csv?v=1&csvType=full&useColumnShortNames=true",
        header=True,
        text_size=32,
        fields=_ELECTRICITY_PER_PERSON_FIELDS,
    ),
    "electricity_demand": Table(
        name="electricity_demand",
        label="Electricity Demand",
        title="6,378 country-years of electricity asked for, in terawatt-hours",
        licence="CC BY 4.0",
        source="https://ourworldindata.org/grapher/electricity-demand",
        classes=(),
        url="https://ourworldindata.org/grapher/electricity-demand.csv?v=1&csvType=full&useColumnShortNames=true",
        header=True,
        text_size=35,
        fields=_ELECTRICITY_DEMAND_FIELDS,
    ),
    "fossil_fuel_energy": Table(
        name="fossil_fuel_energy",
        label="Fossil Fuel Consumption",
        title="11,989 country-years of energy taken from fossil fuels, in terawatt-hours",
        licence="CC BY 4.0",
        source="https://ourworldindata.org/grapher/fossil-fuel-primary-energy",
        classes=(),
        url="https://ourworldindata.org/grapher/fossil-fuel-primary-energy.csv?v=1&csvType=full&useColumnShortNames=true",
        header=True,
        text_size=48,
        fields=_FOSSIL_FUEL_ENERGY_FIELDS,
    ),
    "electricity_carbon_intensity": Table(
        name="electricity_carbon_intensity",
        label="Carbon Intensity of Electricity",
        title="6,332 country-years and the carbon a kilowatt-hour of electricity cost",
        licence="CC BY 4.0",
        source="https://ourworldindata.org/grapher/carbon-intensity-electricity",
        classes=(),
        url="https://ourworldindata.org/grapher/carbon-intensity-electricity.csv?v=1&csvType=full&useColumnShortNames=true",
        header=True,
        text_size=48,
        fields=_ELECTRICITY_CARBON_INTENSITY_FIELDS,
    ),
    "coal_production": Table(
        name="coal_production",
        label="Coal Production",
        title="17,032 country-years of coal dug up, counted in terawatt-hours",
        licence="CC BY 4.0",
        source="https://ourworldindata.org/grapher/coal-production-by-country",
        classes=(),
        url="https://ourworldindata.org/grapher/coal-production-by-country.csv?v=1&csvType=full&useColumnShortNames=true",
        header=True,
        text_size=37,
        fields=_COAL_PRODUCTION_FIELDS,
    ),
    "oil_production": Table(
        name="oil_production",
        label="Oil Production",
        title="17,992 country-years of oil pumped, counted in terawatt-hours",
        licence="CC BY 4.0",
        source="https://ourworldindata.org/grapher/oil-production-by-country",
        classes=(),
        url="https://ourworldindata.org/grapher/oil-production-by-country.csv?v=1&csvType=full&useColumnShortNames=true",
        header=True,
        text_size=37,
        fields=_OIL_PRODUCTION_FIELDS,
    ),
    "gas_production": Table(
        name="gas_production",
        label="Gas Production",
        title="17,251 country-years of gas drawn, counted in terawatt-hours",
        licence="CC BY 4.0",
        source="https://ourworldindata.org/grapher/gas-production-by-country",
        classes=(),
        url="https://ourworldindata.org/grapher/gas-production-by-country.csv?v=1&csvType=full&useColumnShortNames=true",
        header=True,
        text_size=37,
        fields=_GAS_PRODUCTION_FIELDS,
    ),
    "consumption_co2_emissions": Table(
        name="consumption_co2_emissions",
        label="Consumption-Based CO2 Emissions",
        title="5,053 country-years of carbon dioxide emitted for what each country used",
        licence="CC BY 4.0",
        source="https://ourworldindata.org/grapher/consumption-co2-emissions",
        classes=(),
        url="https://ourworldindata.org/grapher/consumption-co2-emissions.csv?v=1&csvType=full&useColumnShortNames=true",
        header=True,
        text_size=29,
        fields=_CONSUMPTION_CO2_EMISSIONS_FIELDS,
    ),
    "methane_emissions": Table(
        name="methane_emissions",
        label="Methane Emissions",
        title="38,150 country-years of methane let go, weighed as carbon dioxide",
        licence="CC BY 4.0",
        source="https://ourworldindata.org/grapher/methane-emissions",
        classes=(),
        url="https://ourworldindata.org/grapher/methane-emissions.csv?v=1&csvType=full&useColumnShortNames=true",
        header=True,
        text_size=40,
        fields=_METHANE_EMISSIONS_FIELDS,
    ),
    "nitrous_oxide_emissions": Table(
        name="nitrous_oxide_emissions",
        label="Nitrous Oxide Emissions",
        title="38,500 country-years of nitrous oxide let go, weighed as carbon dioxide",
        licence="CC BY 4.0",
        source="https://ourworldindata.org/grapher/nitrous-oxide-emissions",
        classes=(),
        url="https://ourworldindata.org/grapher/nitrous-oxide-emissions.csv?v=1&csvType=full&useColumnShortNames=true",
        header=True,
        text_size=40,
        fields=_NITROUS_OXIDE_EMISSIONS_FIELDS,
    ),
    "greenhouse_gas_emissions": Table(
        name="greenhouse_gas_emissions",
        label="Greenhouse Gas Emissions",
        title="38,150 country-years of every greenhouse gas together, weighed as carbon dioxide",
        licence="CC BY 4.0",
        source="https://ourworldindata.org/grapher/total-ghg-emissions",
        classes=(),
        url="https://ourworldindata.org/grapher/total-ghg-emissions.csv?v=1&csvType=full&useColumnShortNames=true",
        header=True,
        text_size=40,
        fields=_GREENHOUSE_GAS_EMISSIONS_FIELDS,
    ),
    "temperature_anomaly": Table(
        name="temperature_anomaly",
        label="Global Temperature Anomaly",
        title="531 yearly readings of how far the air has warmed since the 1860s",
        licence="CC BY 4.0",
        source="https://ourworldindata.org/grapher/temperature-anomaly",
        classes=(),
        url="https://ourworldindata.org/grapher/temperature-anomaly.csv?v=1&csvType=full&useColumnShortNames=true",
        header=True,
        text_size=19,
        fields=_TEMPERATURE_ANOMALY_FIELDS,
    ),
    "sea_surface_temperature": Table(
        name="sea_surface_temperature",
        label="Sea Surface Temperature Anomaly",
        title="531 yearly readings of how far the sea surface has warmed since the 1860s",
        licence="CC BY 4.0",
        source="https://ourworldindata.org/grapher/sea-surface-temperature-anomaly",
        classes=(),
        url="https://ourworldindata.org/grapher/sea-surface-temperature-anomaly.csv?v=1&csvType=full&useColumnShortNames=true",
        header=True,
        text_size=19,
        fields=_SEA_SURFACE_TEMPERATURE_FIELDS,
    ),
    "ice_sheet_mass": Table(
        name="ice_sheet_mass",
        label="Ice Sheet Mass Balance",
        title="384 monthly weighings of the ice lost from Greenland and Antarctica",
        licence="CC BY 4.0",
        source="https://ourworldindata.org/grapher/ice-sheet-mass-balance",
        classes=(),
        url="https://ourworldindata.org/grapher/ice-sheet-mass-balance.csv?v=1&csvType=full&useColumnShortNames=true",
        header=True,
        text_size=10,
        fields=_ICE_SHEET_MASS_FIELDS,
    ),
    "annual_precipitation": Table(
        name="annual_precipitation",
        label="Annual Precipitation",
        title="16,770 country-years and how much rain and snow fell on each",
        licence="CC BY 4.0",
        source="https://ourworldindata.org/grapher/average-precipitation-per-year",
        classes=(),
        url="https://ourworldindata.org/grapher/average-precipitation-per-year.csv?v=1&csvType=full&useColumnShortNames=true",
        header=True,
        text_size=44,
        fields=_ANNUAL_PRECIPITATION_FIELDS,
    ),
    "forest_cover": Table(
        name="forest_cover",
        label="Share of Land under Forest",
        title="8,078 country-years and the share of the land each still keeps under trees",
        licence="CC BY 4.0",
        source="https://ourworldindata.org/grapher/forest-area-as-share-of-land-area",
        classes=(),
        url="https://ourworldindata.org/grapher/forest-area-as-share-of-land-area.csv?v=1&csvType=full&useColumnShortNames=true",
        header=True,
        text_size=46,
        fields=_FOREST_COVER_FIELDS,
    ),
    "agricultural_land": Table(
        name="agricultural_land",
        label="Share of Land Farmed",
        title="12,940 country-years and the share of the land each gives over to farming",
        licence="CC BY 4.0",
        source="https://ourworldindata.org/grapher/share-of-land-area-used-for-agriculture",
        classes=(),
        url="https://ourworldindata.org/grapher/share-of-land-area-used-for-agriculture.csv?v=1&csvType=full&useColumnShortNames=true",
        header=True,
        text_size=56,
        fields=_AGRICULTURAL_LAND_FIELDS,
    ),
    "fertilizer_use": Table(
        name="fertilizer_use",
        label="Fertiliser Use",
        title="12,606 country-years of fertiliser spread on every hectare of cropland",
        licence="CC BY 4.0",
        source="https://ourworldindata.org/grapher/fertilizer-use-per-hectare-of-cropland",
        classes=(),
        url="https://ourworldindata.org/grapher/fertilizer-use-per-hectare-of-cropland.csv?v=1&csvType=full&useColumnShortNames=true",
        header=True,
        text_size=45,
        fields=_FERTILIZER_USE_FIELDS,
    ),
    "pesticide_use": Table(
        name="pesticide_use",
        label="Pesticide Use",
        title="8,225 country-years of pesticide sprayed on the fields, in tonnes",
        licence="CC BY 4.0",
        source="https://ourworldindata.org/grapher/pesticide-use-tonnes",
        classes=(),
        url="https://ourworldindata.org/grapher/pesticide-use-tonnes.csv?v=1&csvType=full&useColumnShortNames=true",
        header=True,
        text_size=45,
        fields=_PESTICIDE_USE_FIELDS,
    ),
    "wheat_yields": Table(
        name="wheat_yields",
        label="Wheat Yields",
        title="9,799 country-years of wheat harvested, in tonnes a hectare",
        licence="CC BY 4.0",
        source="https://ourworldindata.org/grapher/wheat-yields",
        classes=(),
        url="https://ourworldindata.org/grapher/wheat-yields.csv?v=1&csvType=full&useColumnShortNames=true",
        header=True,
        text_size=45,
        fields=_WHEAT_YIELDS_FIELDS,
    ),
    "maize_yields": Table(
        name="maize_yields",
        label="Maize Yields",
        title="12,478 country-years of maize harvested, in tonnes a hectare",
        licence="CC BY 4.0",
        source="https://ourworldindata.org/grapher/maize-yields",
        classes=(),
        url="https://ourworldindata.org/grapher/maize-yields.csv?v=1&csvType=full&useColumnShortNames=true",
        header=True,
        text_size=45,
        fields=_MAIZE_YIELDS_FIELDS,
    ),
    "rice_yields": Table(
        name="rice_yields",
        label="Rice Yields",
        title="9,934 country-years of rice harvested, in tonnes a hectare",
        licence="CC BY 4.0",
        source="https://ourworldindata.org/grapher/rice-yields",
        classes=(),
        url="https://ourworldindata.org/grapher/rice-yields.csv?v=1&csvType=full&useColumnShortNames=true",
        header=True,
        text_size=45,
        fields=_RICE_YIELDS_FIELDS,
    ),
    "cereal_production": Table(
        name="cereal_production",
        label="Cereal Production",
        title="13,538 country-years of cereal brought in, in tonnes",
        licence="CC BY 4.0",
        source="https://ourworldindata.org/grapher/cereal-production",
        classes=(),
        url="https://ourworldindata.org/grapher/cereal-production.csv?v=1&csvType=full&useColumnShortNames=true",
        header=True,
        text_size=45,
        fields=_CEREAL_PRODUCTION_FIELDS,
    ),
    "cattle_numbers": Table(
        name="cattle_numbers",
        label="Number of Cattle",
        title="14,468 country-years and how many head of cattle stood in each",
        licence="CC BY 4.0",
        source="https://ourworldindata.org/grapher/cattle-livestock-count-heads",
        classes=(),
        url="https://ourworldindata.org/grapher/cattle-livestock-count-heads.csv?v=1&csvType=full&useColumnShortNames=true",
        header=True,
        text_size=45,
        fields=_CATTLE_NUMBERS_FIELDS,
    ),
    "fish_consumption": Table(
        name="fish_consumption",
        label="Fish and Seafood Eaten",
        title="13,220 country-years of fish and seafood eaten for every person living there",
        licence="CC BY 4.0",
        source="https://ourworldindata.org/grapher/fish-and-seafood-consumption-per-capita",
        classes=(),
        url="https://ourworldindata.org/grapher/fish-and-seafood-consumption-per-capita.csv?v=1&csvType=full&useColumnShortNames=true",
        header=True,
        text_size=45,
        fields=_FISH_CONSUMPTION_FIELDS,
    ),
    "gdp_per_capita_growth": Table(
        name="gdp_per_capita_growth",
        label="Growth of Output per Person",
        title="12,246 country-years and how fast output per person grew or shrank",
        licence="CC BY 4.0",
        source="https://ourworldindata.org/grapher/gdp-per-capita-growth",
        classes=(),
        url="https://ourworldindata.org/grapher/gdp-per-capita-growth.csv?v=1&csvType=full&useColumnShortNames=true",
        header=True,
        text_size=56,
        fields=_GDP_PER_CAPITA_GROWTH_FIELDS,
    ),
    "trade_share_of_gdp": Table(
        name="trade_share_of_gdp",
        label="Trade as a Share of Output",
        title="9,739 country-years and how much of what each made was traded",
        licence="CC BY 4.0",
        source="https://ourworldindata.org/grapher/trade-as-share-of-gdp",
        classes=(),
        url="https://ourworldindata.org/grapher/trade-as-share-of-gdp.csv?v=1&csvType=full&useColumnShortNames=true",
        header=True,
        text_size=56,
        fields=_TRADE_SHARE_OF_GDP_FIELDS,
    ),
    "foreign_direct_investment": Table(
        name="foreign_direct_investment",
        label="Foreign Direct Investment",
        title="10,031 country-years and how much foreign money came in, against output",
        licence="CC BY 4.0",
        source="https://ourworldindata.org/grapher/foreign-direct-investment-net-inflows-of-gdp",
        classes=(),
        url="https://ourworldindata.org/grapher/foreign-direct-investment-net-inflows-of-gdp.csv?v=1&csvType=full&useColumnShortNames=true",
        header=True,
        text_size=56,
        fields=_FOREIGN_DIRECT_INVESTMENT_FIELDS,
    ),
    "labour_force_participation": Table(
        name="labour_force_participation",
        label="Labour Force Participation",
        title="7,186 country-years and the share of grown-ups working or looking for work",
        licence="CC BY 4.0",
        source="https://ourworldindata.org/grapher/labor-force-participation-rate",
        classes=(),
        url="https://ourworldindata.org/grapher/labor-force-participation-rate.csv?v=1&csvType=full&useColumnShortNames=true",
        header=True,
        text_size=56,
        fields=_LABOUR_FORCE_PARTICIPATION_FIELDS,
    ),
    "world_population": Table(
        name="world_population",
        label="Population",
        title="58,824 country-years and how many people lived in each, back to 10,000 BC",
        licence="CC BY 4.0",
        source="https://ourworldindata.org/grapher/population",
        classes=(),
        url="https://ourworldindata.org/grapher/population.csv?v=1&csvType=full&useColumnShortNames=true",
        header=True,
        text_size=36,
        fields=_WORLD_POPULATION_FIELDS,
    ),
    "birth_rate": Table(
        name="birth_rate",
        label="Birth Rate",
        title="18,722 country-years and how many were born for every thousand living",
        licence="CC BY 4.0",
        source="https://ourworldindata.org/grapher/crude-birth-rate",
        classes=(),
        url="https://ourworldindata.org/grapher/crude-birth-rate.csv?v=1&csvType=full&useColumnShortNames=true",
        header=True,
        text_size=59,
        fields=_BIRTH_RATE_FIELDS,
    ),
    "maternal_mortality": Table(
        name="maternal_mortality",
        label="Maternal Mortality",
        title="9,264 country-years and how many mothers died for every hundred thousand born",
        licence="CC BY 4.0",
        source="https://ourworldindata.org/grapher/maternal-mortality",
        classes=(),
        url="https://ourworldindata.org/grapher/maternal-mortality.csv?v=1&csvType=full&useColumnShortNames=true",
        header=True,
        text_size=54,
        fields=_MATERNAL_MORTALITY_FIELDS,
    ),
    "international_migrants": Table(
        name="international_migrants",
        label="International Migrants",
        title="2,176 country-years and how many people living in each were born elsewhere",
        licence="CC BY 4.0",
        source="https://ourworldindata.org/grapher/migrant-stock-total",
        classes=(),
        url="https://ourworldindata.org/grapher/migrant-stock-total.csv?v=1&csvType=full&useColumnShortNames=true",
        header=True,
        text_size=39,
        fields=_INTERNATIONAL_MIGRANTS_FIELDS,
    ),
    "broadband_subscriptions": Table(
        name="broadband_subscriptions",
        label="Landline Broadband Subscriptions",
        title="4,590 country-years and how many broadband lines each had per hundred people",
        licence="CC BY 4.0",
        source="https://ourworldindata.org/grapher/fixed-broadband-subscriptions-per-100-people",
        classes=(),
        url="https://ourworldindata.org/grapher/fixed-broadband-subscriptions-per-100-people.csv?v=1&csvType=full&useColumnShortNames=true",
        header=True,
        text_size=56,
        fields=_BROADBAND_SUBSCRIPTIONS_FIELDS,
    ),
}


def _uci_table(item: Mapping[str, Any]) -> Table:
    """One generated UCI normalized-CSV declaration as a table converter."""
    return Table(
        name=item["name"],
        label=item["label"],
        title=item["title"],
        licence="CC BY 4.0",
        source=item["source"],
        classes=tuple(item["classes"]),
        creators=tuple(item.get("creators", ())),
        origin=item.get("origin", ""),
        repository=item.get("repository", "UCI Machine Learning Repository"),
        citation=item.get("citation", ""),
        url=item["url"],
        header=True,
        text_size=item["text_size"],
        fields=tuple(tuple(field) for field in item["fields"]),
        labels=item["labels"],
        codes={name: tuple(values) for name, values in item["codes"].items()},
        continuations=bool(item.get("continuations", False)),
    )


def _add_uci_tables(
    registry: dict[str, Images | CIFAR | Audio | Matrix | Table | Large],
    items: Sequence[Mapping[str, Any]],
) -> None:
    """Add generated tables, refusing to let one shadow a curated converter."""
    for item in items:
        spec = _uci_table(item)
        if spec.name in registry:
            raise ValueError(f"generated UCI dataset {spec.name!r} duplicates an existing one")
        registry[spec.name] = spec


_add_uci_tables(DATASETS, UCI_TABLES)


def _uci_large(item: Mapping[str, Any]) -> Large:
    """One curated, file-backed UCI archive declaration."""
    return Large(
        name=item["name"],
        label=item["label"],
        title=item["title"],
        licence="CC BY 4.0",
        source=item["source"],
        classes=tuple(item["classes"]),
        splits=tuple(item.get("splits", ("all",))),
        basket_size=int(item.get("basket_size", TABLE_BASKET)),
        modality=item.get("modality", "tabular"),
        task=item.get("task", "machine learning"),
        creators=tuple(item.get("creators", ())),
        origin=item.get("origin", ""),
        repository=item.get("repository", "UCI Machine Learning Repository"),
        citation=item.get("citation", ""),
        url=item["url"],
        source_bytes=item["source_bytes"],
        converter=item["converter"],
        requires=tuple(item.get("requires", ())),
        transformation_note=item["transformation"],
        split_files=bool(item.get("split_files", False)),
    )


def _add_uci_large(
    registry: dict[str, Images | CIFAR | Audio | Matrix | Table | Large],
    items: Sequence[Mapping[str, Any]],
) -> None:
    """Add the curated large sources without shadowing another converter."""
    for item in items:
        spec = _uci_large(item)
        if spec.name in registry:
            raise ValueError(f"large UCI dataset {spec.name!r} duplicates an existing one")
        registry[spec.name] = spec


_add_uci_large(DATASETS, UCI_LARGE)


def _open_large(item: Mapping[str, Any]) -> Large:
    """One explicitly licensed, file-backed archive outside UCI."""
    return Large(
        name=item["name"],
        label=item["label"],
        title=item["title"],
        licence=item["licence"],
        source=item["source"],
        classes=tuple(item["classes"]),
        splits=tuple(item.get("splits", ("all",))),
        basket_size=int(item.get("basket_size", TABLE_BASKET)),
        modality=item["modality"],
        task=item["task"],
        creators=tuple(item.get("creators", ())),
        publisher=item.get("publisher", ""),
        origin=item.get("origin", ""),
        repository=item.get("repository", ""),
        mirrors=tuple(tuple(mirror) for mirror in item.get("mirrors", ())),
        citation=item.get("citation", ""),
        url=item.get("url", ""),
        source_bytes=item["source_bytes"],
        converter=item["converter"],
        requires=tuple(item.get("requires", ())),
        transformation_note=item["transformation"],
        layout_note=item.get("layout", ""),
        sources=item.get("sources", {}),
        source_sizes=item.get("source_sizes", {}),
        cache_names=item.get("cache_names", {}),
        split_files=bool(item.get("split_files", False)),
    )


def _add_open_large(
    registry: dict[str, Images | CIFAR | Audio | Matrix | Table | Large],
    items: Sequence[Mapping[str, Any]],
) -> None:
    """Add open archives without allowing them to shadow an existing name."""
    for item in items:
        spec = _open_large(item)
        if spec.name in registry:
            raise ValueError(f"open large dataset {spec.name!r} duplicates an existing one")
        registry[spec.name] = spec


_add_open_large(DATASETS, OPEN_LARGE)


def _hub_large(item: Mapping[str, Any]) -> Large:
    """One explicitly licensed, complete Hub Parquet conversion."""
    return Large(
        name=item["name"],
        label=item["label"],
        title=item["title"],
        licence=item["licence"],
        source=item["source"],
        classes=(),
        splits=tuple(item["splits"]),
        modality=item["modality"],
        task=item["task"],
        source_bytes=item["source_bytes"],
        converter=f"open:hub:{item['name']}",
        requires=tuple(item["requires"]),
        transformation_note=item["transformation"],
        layout_note=item.get("layout", "one rows TTree per publisher split"),
        sources=item["sources"],
        source_sizes=item["source_sizes"],
        enforce_source_sizes=bool(item.get("enforce_source_sizes", False)),
        split_files=bool(item.get("split_files", False)),
    )


def _add_hub_open(
    registry: dict[str, Images | CIFAR | Audio | Matrix | Table | Large],
    items: Sequence[Mapping[str, Any]],
) -> None:
    """Add generated Hub datasets without allowing one to shadow a converter."""
    for item in items:
        spec = _hub_large(item)
        if spec.name in registry:
            raise ValueError(f"open Hub dataset {spec.name!r} duplicates an existing one")
        registry[spec.name] = spec


_add_hub_open(DATASETS, HUB_OPEN)


def _apply_uci_attribution(
    registry: dict[str, Images | CIFAR | Audio | Matrix | Table | Large],
) -> None:
    """Attach UCI's fixed creator/DOI snapshot without calling its API at runtime."""
    marker = "archive.ics.uci.edu/dataset/"
    for name, spec in tuple(registry.items()):
        if marker not in spec.source:
            continue
        uci_id = int(spec.source.partition(marker)[2].partition("/")[0])
        record = UCI_ATTRIBUTION[uci_id]
        registry[name] = replace(
            spec,
            creators=tuple(record["creators"]),
            origin=record["origin"],
            repository="UCI Machine Learning Repository",
            citation=record["citation"],
        )


_apply_uci_attribution(DATASETS)


def _spec(name: str) -> Images | CIFAR | Audio | Matrix | Table | Large:
    """The dataset called ``name``, or a refusal naming the ones there are."""
    spec = DATASETS.get(name)
    if spec is None:
        raise ValueError(f"the datasets here are {', '.join(DATASETS)}, not {name!r}")
    return spec


#: Licence families under which a converted file may be passed on, matched at
#: the front of the statement. Copyleft is here on purpose: the GPL and its
#: relatives permit redistribution, they just oblige the licence to travel
#: with the data - and every file converted here carries its licence in its
#: ``about`` key, so it does.
_SHAREABLE = (
    "cc0",
    "cc by",
    "mit",
    "apache",
    "afl",
    "boost",
    "bsd",
    "bsl",
    "cdla",
    "ecl",
    "etalab",
    "isc",
    "mpl",
    "ncsa",
    "epl",
    "gpl",
    "agpl",
    "lgpl",
    "artistic",
    "odc-by",
    "odbl",
    "pddl",
    "postgresql",
    "unlicense",
    "wtfpl",
    "zlib",
)


def redistributable(licence: str) -> bool:
    """Whether a file converted under ``licence`` may be served to others.

        >>> redistributable("CC BY 4.0")
        True
        >>> redistributable("no formal licence; the tech report asks to be cited")
        False

    This answers the one question a public mirror has to ask - may this be
    passed on at all - and nothing subtler: attribution, share-alike and
    copyleft obligations still apply, and they are met by the licence
    statement every converted file carries. A ``NoDerivatives`` or
    ``NonCommercial`` clause, or no licence at all, is a no; so is anything
    this function has not read, because "nobody said you could not" is not a
    licence.
    """
    text = licence.lower()
    if "no formal licence" in text or "-nc" in text or "-nd" in text:
        return False
    if text.startswith(_SHAREABLE):
        return True
    return "public domain" in text or "us federal government" in text


def describe(name: str | None = None) -> str:
    """What each dataset is, what its licence says, and where it comes from.

    >>> print(describe("iris"))
    iris           150 iris flowers measured four ways, 3 species
                   3 classes, CC BY 4.0
                   https://archive.ics.uci.edu/dataset/53/iris
    """
    chosen = DATASETS.values() if name is None else [_spec(name)]
    return "\n".join(
        f"{spec.name:<14} {spec.title}\n"
        f"{'':14} {spec.sorting()}, {spec.licence}\n"
        f"{'':14} {spec.source}"
        for spec in chosen
    )


def convert(
    name: str,
    target: Any,
    *,
    split: str | None = None,
    parts: Mapping[str, Any] | None = None,
    base: str | None = None,
    prefix: str | None = None,
    compression: str | None = "zlib",
    basket_size: int | None = None,
    source_cache: str | os.PathLike[str] | None = None,
    allow_oversize: bool = False,
    progress: Callable[[int], None] | None = None,
    config: Any = None,
) -> dict[str, int]:
    """Write one dataset into a ROOT file, a tree per class; say what went where.

        >>> from xrd.root import datasets                    # doctest: +SKIP
        >>> datasets.convert("iris", "iris.root")            # doctest: +SKIP
        {'setosa': 50, 'versicolor': 50, 'virginica': 50}

    ``target`` is where the file goes - a path, a URL, an open binary file, or
    a :class:`~.writer.WritableFile` already open, which is how several splits
    end up in one file. The data is downloaded from wherever the dataset is
    published unless ``parts`` supplies it: a mapping of the roles in
    :meth:`Images.urls` and its siblings to bytes, a path or a URL. ``base``
    redirects the downloads at a mirror of your own. Sources at or above the
    default two-gigabyte ceiling require ``allow_oversize=True``; this is an
    explicit storage-capacity decision and does not weaken source-size checks.
    ``progress``, when supplied, receives the number of rows written in the
    current split at bounded intervals and once more when that split finishes.

    Each tree is named for its split and its class - ``train_frog`` - with the
    split left off when the dataset has only one. Beside them goes an
    ``about`` key holding its origin, canonical licence terms and a summary of
    the transformation, so the file keeps saying where it came from and what
    was done to it.
    """
    spec = _spec(name)
    _check_source_ceiling(spec, parts, allow_oversize)
    split = _conversion_split(spec, split)
    urls = _conversion_urls(spec, split, base)
    supplied = dict(parts or {})
    temporary: list[Path] = []
    try:
        raw = _conversion_sources(spec, split, urls, supplied, source_cache, config, temporary)
        classes, columns, rows = spec.rows(raw, split)
        prefix = _conversion_prefix(spec, split, prefix)
        return _write_conversion(
            target,
            spec,
            split,
            prefix,
            classes,
            columns,
            rows,
            compression,
            basket_size,
            progress,
            config,
        )
    finally:
        _remove_conversion_sources(temporary)


def _check_source_ceiling(
    spec: Dataset, parts: Mapping[str, Any] | None, allow_oversize: bool
) -> None:
    """Require an explicit capacity decision for oversized remote sources."""
    if parts is None and not allow_oversize and not spec.within_source_ceiling():
        raise ValueError(
            f"{spec.label} reaches the strict 2 GB per-dataset source ceiling; "
            "only complete source payloads below 2,000,000,000 bytes are converted by "
            "default; pass allow_oversize=True after provisioning sufficient storage"
        )


def _conversion_urls(spec: Dataset, split: str, base: str | None) -> dict[str, str]:
    """Source URLs, optionally redirected through a caller's mirror."""
    urls = spec.urls(split)
    if base is None:
        return urls
    return {role: _mirror_source(base, url) for role, url in urls.items()}


def _conversion_prefix(spec: Dataset, split: str, prefix: str | None) -> str:
    """Default tree prefix: the split only when a dataset has several."""
    if prefix is not None:
        return prefix
    return split if len(spec.splits) > 1 else ""


def _remove_conversion_sources(paths: Sequence[Path]) -> None:
    """Remove only temporary downloads; retained cache files are not listed."""
    for path in paths:
        path.unlink(missing_ok=True)


def _conversion_split(spec: Dataset, split: str | None) -> str:
    """Resolve the default split and reject one the dataset does not publish."""
    chosen = spec.splits[0] if split is None else split
    if chosen not in spec.splits:
        raise ValueError(
            f"the splits {spec.label} comes in are {' and '.join(spec.splits)}, not {chosen!r}"
        )
    return chosen


def _conversion_sources(
    spec: Dataset,
    split: str,
    urls: Mapping[str, str],
    supplied: Mapping[str, Any],
    source_cache: str | os.PathLike[str] | None,
    config: Any,
    temporary: list[Path],
) -> dict[str, Any]:
    """Fetch byte-backed sources or retain paths for a streaming reader."""
    if not spec.file_backed():
        return {role: fetch(supplied.get(role, url), config=config) for role, url in urls.items()}
    cache = Path(source_cache) if source_cache is not None else None
    raw: dict[str, Any] = {}
    for role, url in urls.items():
        path, remove = _fetch_file(
            supplied.get(role, url),
            cache=cache,
            name=_conversion_cache_name(spec, split, role, len(urls)),
            expected=spec.expected_source_bytes(role),
            config=config,
        )
        raw[role] = path
        if remove:
            temporary.append(path)
    return raw


def _conversion_cache_name(spec: Dataset, split: str, role: str, sources: int) -> str:
    """Stable retained-source name for a single archive or one of its shards."""
    if isinstance(spec, Large) and role in spec.cache_names:
        return spec.cache_names[role]
    if sources == 1:
        return spec.name
    if isinstance(spec, Large):
        return f"{spec.name}-{role}"
    return f"{spec.name}-{split}-{role}"


def _write_conversion(
    target: Any,
    spec: Dataset,
    split: str,
    prefix: str,
    classes: Sequence[str],
    columns: dict[str, Any],
    rows: Rows,
    compression: str | None,
    basket_size: int | None,
    progress: Callable[[int], None] | None,
    config: Any,
) -> dict[str, int]:
    """Fill and finish the output trees, closing only a writer opened here."""
    given = isinstance(target, WritableFile)
    out = target if given else create(target, compression=compression, config=config)
    try:
        trees = _conversion_trees(out, spec, split, prefix, classes, columns, basket_size)
        _fill_conversion(trees, rows, spec, classes, progress)
        out.write(_about_name(prefix), spec.about(split))
        return {tree.name: len(tree) for tree in trees.values()}
    finally:
        _close_conversion(out, given)


def _conversion_trees(
    out: Any,
    spec: Dataset,
    split: str,
    prefix: str,
    classes: Sequence[str],
    columns: dict[str, Any],
    basket_size: int | None,
) -> dict[int, Any]:
    """Create the output tree for each declared class."""
    size = spec.basket_size if basket_size is None else basket_size
    return {
        at: out.tree(
            f"{prefix}_{cls}" if prefix else cls,
            columns,
            title=spec.entry_title(split, cls),
            basket_size=size,
        )
        for at, cls in enumerate(classes)
    }


def _fill_conversion(
    trees: Mapping[int, Any],
    rows: Rows,
    spec: Dataset,
    classes: Sequence[str],
    progress: Callable[[int], None] | None,
) -> None:
    """Route every row to its declared class tree."""
    written = 0
    for at, row in rows:
        tree = trees.get(at)
        if tree is None:
            raise ValueError(
                f"row {row['index']} of {spec.label} is labelled {at}, and it has "
                f"{len(classes)} classes"
            )
        tree.fill(**row)
        written += 1
        if progress is not None and written % 1024 == 0:
            progress(written)
    if progress is not None:
        progress(written)


def _about_name(prefix: str) -> str:
    return f"{prefix}_about" if prefix else "about"


def _close_conversion(out: Any, given: bool) -> None:
    if not given:
        out.close()
