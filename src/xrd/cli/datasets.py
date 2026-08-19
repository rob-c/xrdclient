"""``xrd-datasets`` - a machine-learning datasets site, built and checked.

    $ xrd-datasets build /srv/datasets --only "mnist*" --only iris
    $ xrd-datasets verify /srv/datasets
    $ xrd-datasets site /srv/datasets --base-url https://data.example.org

``build`` converts every dataset whose licence allows redistribution into a
ROOT file under one directory - see :mod:`xrd.root.datasets` for what is on
offer - and writes an ``index.json`` beside them saying what each file is,
where it came from, and what its checksum is. ``verify`` reopens every file
and refuses to bless a directory that no longer matches its index. ``site``
puts a browsable page and ready-to-serve nginx and BriX configuration next
to the files, so the directory can go on the web as it stands.

The index is what makes the directory a catalogue: point ``XRD_CATALOGUE``
at wherever it is served and ``xrd.ml.load("mnist")`` finds the file by
name, on any machine, over whichever protocol the site speaks.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import fnmatch
import json
import string
import sys
from collections.abc import Callable, Iterator, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..config import Config
from ..crypto import checksum_file
from ..errors import XRootDError
from ..root import open_root
from ..root.datasets import DATASETS, convert, redistributable
from ..root.writer import create
from ..types import human_bytes
from . import ERROR, OK, common_flags, config_from, dumps, fail

__all__ = ["main"]

PROGRAM = "xrd-datasets"


# ---------------------------------------------------------------------------
# Choosing
# ---------------------------------------------------------------------------


def _chosen(only: Sequence[str], everything: bool) -> list[str]:
    """The dataset names a command was asked for, licence gate applied.

    ``--only`` narrows by glob; without ``--all``, whatever the licence does
    not allow onto a mirror is left out - loudly, when it was asked for by
    name, because silently skipping what somebody typed is how a build lies.
    """
    matched = [
        name
        for name in sorted(DATASETS)
        if not only or any(fnmatch.fnmatchcase(name, pattern) for pattern in only)
    ]
    if not matched:
        raise ValueError(f"no dataset matches {', '.join(only)}; try `xrd-datasets list`")
    if everything:
        return matched
    allowed = [name for name in matched if redistributable(DATASETS[name].licence)]
    if not allowed:
        withheld = ", ".join(matched)
        raise ValueError(
            f"the licence of {withheld} does not allow redistribution; "
            "--all converts it anyway, for a directory you serve only to yourself"
        )
    return allowed


def _list(args: argparse.Namespace, config: Config) -> int:
    names = _chosen(args.only, True)
    if args.json:
        print(
            dumps(
                [
                    {
                        "name": name,
                        "title": DATASETS[name].title,
                        "licence": DATASETS[name].licence,
                        "redistributable": redistributable(DATASETS[name].licence),
                    }
                    for name in names
                ]
            )
        )
        return OK
    for name in names:
        spec = DATASETS[name]
        gate = "" if redistributable(spec.licence) else "  [not redistributable]"
        print(f"{name:<18} {spec.title}{gate}")
    return OK


# ---------------------------------------------------------------------------
# Building
# ---------------------------------------------------------------------------


def _chunks(path: Path) -> Iterator[bytes]:
    with path.open("rb") as handle:
        while chunk := handle.read(1 << 20):
            yield chunk


def _trees_in(path: Path) -> dict[str, int]:
    """Tree name to row count, read back out of a finished file."""
    with open_root(str(path)) as back:
        return {
            name: back[name].num_entries
            for name in back.keys()
            if name != "about" and not name.endswith("_about")
        }


def _entry(name: str, path: Path, trees: dict[str, int]) -> dict[str, Any]:
    spec = DATASETS[name]
    return {
        "name": name,
        "title": spec.title,
        "licence": spec.licence,
        "redistributable": redistributable(spec.licence),
        "source": spec.source,
        "file": path.name,
        "bytes": path.stat().st_size,
        "adler32": checksum_file("adler32", _chunks(path)),
        "splits": list(spec.splits),
        "trees": trees,
        "rows": sum(trees.values()),
    }


def _convert_one(
    name: str, path: Path, *, base: str | None, compression: str | None, config: Config
) -> dict[str, Any]:
    """One dataset, every split, into one file; the index entry for it."""
    spec = DATASETS[name]
    try:
        with create(str(path), compression=compression, config=config) as out:
            trees: dict[str, int] = {}
            for split in spec.splits:
                trees.update(convert(name, out, split=split, base=base, config=config))
    except BaseException:
        path.unlink(missing_ok=True)  # half a file is worse than none
        raise
    return _entry(name, path, trees)


def _build(args: argparse.Namespace, config: Config) -> int:
    out = Path(args.directory)
    out.mkdir(parents=True, exist_ok=True)
    names = _chosen(args.only, args.all)

    kept = [name for name in names if (out / f"{name}.root").exists() and not args.force]
    todo = [name for name in names if name not in kept]

    entries: dict[str, dict[str, Any]] = {}
    failed: dict[str, str] = {}
    for name in kept:
        # Already on disk from an earlier run: index what is there rather
        # than converting it again. ``--force`` is the fresh start.
        path = out / f"{name}.root"
        entries[name] = _entry(name, path, _trees_in(path))
        if not args.quiet and not args.json:
            print(f"{name}: kept, {human_bytes(entries[name]['bytes'])}")

    def one(name: str) -> dict[str, Any]:
        return _convert_one(
            name,
            out / f"{name}.root",
            base=args.base,
            compression=args.compression,
            config=config,
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=args.jobs) as pool:
        running = {pool.submit(one, name): name for name in todo}
        for future in concurrent.futures.as_completed(running):
            name = running[future]
            try:
                entries[name] = future.result()
            except (XRootDError, OSError, ValueError) as exc:
                failed[name] = str(exc)
                print(f"{PROGRAM}: {name}: {exc}", file=sys.stderr)
            else:
                if not args.quiet and not args.json:
                    made = entries[name]
                    print(f"{name}: {made['rows']} rows, {human_bytes(made['bytes'])}")

    ordered = [entries[name] for name in sorted(entries)]
    _write_index(out, ordered)
    if args.json:
        print(dumps({"converted": sorted(todo), "kept": sorted(kept), "failed": failed}))
    return ERROR if failed else OK


def _write_index(out: Path, entries: list[dict[str, Any]]) -> None:
    document = {
        "format": 1,
        "built": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "datasets": entries,
    }
    (out / "index.json").write_text(json.dumps(document, indent=2) + "\n")
    lines = [f"{made['adler32']}  {made['bytes']:>12}  {made['file']}" for made in entries]
    (out / "MANIFEST").write_text("\n".join(lines) + "\n" if lines else "")


# ---------------------------------------------------------------------------
# Verifying
# ---------------------------------------------------------------------------


def _verify(args: argparse.Namespace, config: Config) -> int:
    out = Path(args.directory)
    index = json.loads((out / "index.json").read_text())
    problems: dict[str, str] = {}
    for made in index["datasets"]:
        name = made["name"]
        path = out / made["file"]
        if not path.exists():
            problems[name] = "the file is missing"
            continue
        size = path.stat().st_size
        if size != made["bytes"]:
            problems[name] = f"{size} bytes on disk, {made['bytes']} in the index"
            continue
        if checksum_file("adler32", _chunks(path)) != made["adler32"]:
            problems[name] = "the checksum does not match the index"
            continue
        trees = _trees_in(path)
        if trees != made["trees"]:
            problems[name] = "the trees do not match the index"
    if args.json:
        print(dumps({"checked": len(index["datasets"]), "problems": problems}))
    else:
        for name, what in problems.items():
            print(f"{name}: {what}", file=sys.stderr)
        if not args.quiet:
            fine = len(index["datasets"]) - len(problems)
            print(f"{fine} of {len(index['datasets'])} files match the index")
    return ERROR if problems else OK


# ---------------------------------------------------------------------------
# The site
# ---------------------------------------------------------------------------


def _root_url(base: str, given: str | None) -> str:
    """Where the same directory answers to ``root://``.

    A site nearly always serves both planes off one host, so the default is
    that host with the scheme swapped; ``--root-url`` is for the deployment
    where the native protocol lives somewhere else, behind its own name or
    port.
    """
    if given:
        return given.rstrip("/")
    host = base.split("://", 1)[-1].split("/", 1)[0]
    return f"root://{host}"


def _site(args: argparse.Namespace, config: Config) -> int:
    out = Path(args.directory)
    index = json.loads((out / "index.json").read_text())
    base = args.base_url.rstrip("/") if args.base_url else "https://data.example.org"
    values = {
        "title": args.title,
        "base_url": base,
        "root_url": _root_url(base, args.root_url),
        "built": index["built"],
        "count": str(len(index["datasets"])),
        "plural": "" if len(index["datasets"]) == 1 else "s",
        # ``</`` would end the page's own script block early if a title ever
        # contained it; JSON does not need the slash, so it goes.
        "payload": json.dumps(index["datasets"]).replace("</", "<\\/"),
        "root": str(out.resolve()),
    }
    written = []
    for filename, template in _SITE_FILES.items():
        (out / filename).write_text(string.Template(template).substitute(values))
        written.append(filename)
    if args.json:
        print(dumps({"written": written}))
    elif not args.quiet:
        print(f"wrote {', '.join(written)} in {out}")
    return OK


_PAGE = """\
<!doctype html>
<html lang="en">
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>$title</title>
<style>
  :root { color-scheme: light dark; --line: #8884; }
  body { font: 16px/1.5 system-ui, sans-serif; max-width: 60rem;
         margin: 2rem auto; padding: 0 1rem; }
  h1 { font-size: 1.6rem; }
  code, pre { font: 0.85rem/1.5 ui-monospace, monospace; }
  pre { border: 1px solid var(--line); border-radius: 6px; padding: 0.8rem;
        overflow-x: auto; }
  input { font: inherit; width: 100%; padding: 0.4rem 0.6rem; margin: 1rem 0;
          border: 1px solid var(--line); border-radius: 6px;
          background: transparent; color: inherit; }
  table { border-collapse: collapse; width: 100%; }
  th, td { text-align: left; padding: 0.35rem 0.6rem;
           border-bottom: 1px solid var(--line); vertical-align: top; }
  td.n { text-align: right; white-space: nowrap; }
  .muted { opacity: 0.65; font-size: 0.85rem; }
  .lede { font-size: 1.05rem; }
  h2 { font-size: 1.15rem; margin-top: 2rem; }
  .ways td { vertical-align: top; }
  .ways td:first-child { white-space: nowrap; font-weight: 600; }
  .ways pre { margin: 0.4rem 0 0; }
</style>
<body>
<h1>$title</h1>
<p class="lede">$count dataset$plural, converted to ROOT files and streamed over
<b>XRootD</b> &mdash; the high-performance data-access protocol built in
high-energy physics, and the one the OSG and the WLCG move petabytes of
physics data over every day, across hundreds of sites worldwide. This is that
machinery pointed at machine-learning data: the same protocol, the same
client, the same wide-area performance.</p>

<p>Stream one straight into a training loop. Nothing is downloaded first:</p>
<pre>pip install xrd
export XRD_CATALOGUE=$base_url

python -c '
import xrd.ml
data = xrd.ml.load("mnist")
for images, labels in data.train.batches(256):
    ...'</pre>

<p>A minibatch is a read of the baskets it needs and nothing else, so the loop
starts at the first batch rather than at the end of a download, and a dataset
larger than the machine is not a problem. Reading the same data over and over,
or from further away than you would like? <code>xrd.ml.load("mnist",
cache=True)</code> pulls the file once into <code>~/.cache/xrd</code>, checks
it against this catalogue, and reads from your own disk ever after.</p>

<h2>Three ways to the same bytes</h2>
<table class="ways">
<tbody>
<tr><td><code>root://</code></td>
    <td>The native protocol: parallel, resumable, vector reads, checksums on
    the wire. Public and read-only.
    <pre>xrd.ml.load("$root_url//mnist.root")</pre></td></tr>
<tr><td><code>https://</code></td>
    <td>Range requests, for anything that speaks HTTP &mdash; a browser, a
    notebook, <code>curl</code>, a batch node behind a proxy.
    <pre>xrd.ml.load("$base_url/mnist.root")</pre></td></tr>
<tr><td>browser</td>
    <td>Every name in the table below is a link. Click one and you have the
    file; nothing here needs a login, an account or a token.</td></tr>
</tbody>
</table>

<p class="muted">Served by <a href="https://github.com/rob-c/PyXRootDClient"
>PyXRootDClient</a> against a BriX-Cache endpoint, read-only on every plane.
Each file carries its licence and source in its <code>about</code> key; the
same is recorded in <a href="index.json">index.json</a>, which is how the name
lookup above works. Built $built.</p>
<input id="q" type="search" placeholder="filter by name, title or licence"
       aria-label="filter">
<table>
<thead><tr><th>name</th><th>what it is</th><th>rows</th><th>size</th>
<th>licence</th></tr></thead>
<tbody id="rows"></tbody>
</table>
<script>
const DATASETS = $payload;
const human = n => {
  for (const unit of ["B", "KiB", "MiB", "GiB"]) {
    if (n < 1024 || unit === "GiB") return n.toFixed(unit === "B" ? 0 : 1) + " " + unit;
    n /= 1024;
  }
};
const rows = document.getElementById("rows");
const show = wanted => {
  rows.textContent = "";
  for (const d of DATASETS) {
    const text = (d.name + " " + d.title + " " + d.licence).toLowerCase();
    if (wanted && !text.includes(wanted)) continue;
    const tr = rows.insertRow();
    const link = document.createElement("a");
    link.href = d.file;
    link.textContent = d.name;
    tr.insertCell().appendChild(link);
    tr.insertCell().textContent = d.title;
    const count = tr.insertCell();
    count.textContent = d.rows.toLocaleString();
    count.className = "n";
    const size = tr.insertCell();
    size.textContent = human(d.bytes);
    size.className = "n";
    tr.insertCell().textContent = d.licence;
  }
};
document.getElementById("q").addEventListener("input",
  event => show(event.target.value.trim().toLowerCase()));
show("");
</script>
</body>
</html>
"""

_NGINX = """\
# $title - static hosting for any stock nginx.
#
# Drop this into /etc/nginx/conf.d/ (adjust the port and server_name) and the
# directory serves as it stands: the page, the index, and the files, with the
# range requests xrd.ml relies on handled by nginx itself.
server {
    listen 8080;
    server_name _;

    root $root;
    index index.html;

    location / {
        # Training loops read from browsers, notebooks and batch nodes alike.
        add_header Access-Control-Allow-Origin *;
        # ROOT files are already compressed; recompressing wastes the CPU.
        gzip off;
    }
}
"""

_BRIX = """\
# $title - the same directory over root://, WebDAV and S3, via BriX.
#
# A complete configuration for a BriX-built nginx (nginx-xrootd). Read-only
# on every plane: nothing here takes a write. Start it with
#     nginx -c $root/brix.conf
# or install xrd-datasets.service alongside this file.
worker_processes auto;
daemon off;
error_log stderr info;
pid /run/xrd-datasets-nginx.pid;

events { worker_connections 1024; }

http {
    access_log off;

    # The browsable page and plain HTTP downloads.
    server {
        listen 8080;
        root $root;
        index index.html;
        location / {
            add_header Access-Control-Allow-Origin *;
            gzip off;
        }
    }

    # WebDAV, for davs:// clients and anything that PROPFINDs.
    server {
        listen 8008;
        location / {
            brix_webdav          on;
            brix_webdav_auth     none;
            brix_storage_backend posix:$root;
        }
    }
}

stream {
    # The native protocol: root://host:1094//mnist.root
    server {
        listen 1094;
        brix_root            on;
        brix_export          $root;
        brix_storage_backend posix:$root;
        brix_auth            none;
    }
}
"""

_UNIT = """\
# $title - serve the datasets directory with BriX.
#
#     cp xrd-datasets.service /etc/systemd/system/
#     systemctl enable --now xrd-datasets
[Unit]
Description=$title
After=network-online.target
Wants=network-online.target

[Service]
ExecStart=/usr/sbin/nginx -c $root/brix.conf
Restart=on-failure
# Read-only serving deserves a read-only view of the machine.
ProtectSystem=strict
ReadOnlyPaths=$root
ReadWritePaths=/run
NoNewPrivileges=yes

[Install]
WantedBy=multi-user.target
"""

_README = """\
# $title

$count machine-learning dataset$plural, converted to ROOT files by
`xrd-datasets build`, indexed in `index.json`, and checksummed in
`MANIFEST`. Every file says what it is and what its licence is in its own
`about` key, so it keeps saying so wherever it is copied.

They are served over XRootD, the data-access protocol high-energy physics
built for globally distributed analysis and runs across the OSG and the
WLCG. Training data streams the same way the physics does.

## Use it

    pip install xrd
    export XRD_CATALOGUE=$base_url

    python -c 'import xrd.ml; print(xrd.ml.load("iris"))'

Any dataset name in the table on the site works; `index.json` is the
catalogue that resolves it. A whole URL works too, on either plane:

    xrd.ml.load("$root_url//iris.root")     # native, streamed
    xrd.ml.load("$base_url/iris.root")      # HTTP ranges

Nothing is downloaded: a minibatch reads the baskets it needs. To keep a
local copy anyway - the same data read many times, or a slow link -

    xrd.ml.load("iris", cache=True)

pulls it once into `~/.cache/xrd`, checks it against this catalogue, and
reads from disk after that.

## Serve it

* `nginx.conf` - static hosting on any stock nginx, port 8080.
* `brix.conf` - the same directory over root:// (1094), WebDAV (8008) and
  plain HTTP (8080) with a BriX-built nginx.
* `xrd-datasets.service` - the systemd unit that runs the BriX flavour.

## Keep it honest

    xrd-datasets verify $root

reopens every file and compares it, byte for byte, with `index.json`.
Rebuild and redeploy with `xrd-datasets build` + `xrd-datasets site`; a
file that has not changed is kept, not reconverted.

Built $built.
"""

#: What ``site`` writes, next to the files it describes.
_SITE_FILES = {
    "index.html": _PAGE,
    "nginx.conf": _NGINX,
    "brix.conf": _BRIX,
    "xrd-datasets.service": _UNIT,
    "README.md": _README,
}


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=PROGRAM,
        description="Build, check and publish a directory of ML datasets as ROOT files.",
    )
    subs = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    def command(name: str, handler: Callable[..., int], help_text: str) -> argparse.ArgumentParser:
        sub = subs.add_parser(name, help=help_text, description=help_text)
        sub.set_defaults(handler=handler)
        common_flags(sub)
        return sub

    def gates(sub: argparse.ArgumentParser) -> None:
        sub.add_argument(
            "--only",
            action="append",
            default=[],
            metavar="GLOB",
            help="just the datasets matching this (repeatable)",
        )
        sub.add_argument(
            "--all",
            action="store_true",
            help="include datasets whose licence does not allow redistribution",
        )

    listing = command("list", _list, "name every dataset this tool can build")
    gates(listing)

    build = command("build", _build, "convert the datasets and write the index")
    build.add_argument("directory", help="where the files and index.json go")
    gates(build)
    build.add_argument("-j", "--jobs", type=int, default=4, help="conversions in flight at once")
    build.add_argument(
        "-f", "--force", action="store_true", help="reconvert files that are already there"
    )
    build.add_argument(
        "--base", metavar="URL", help="fetch the source data from this mirror instead"
    )
    build.add_argument(
        "--compression",
        default="zlib",
        metavar="NAME",
        help="zlib, lzma, lz4, zstd or none (default zlib)",
    )

    verify = command("verify", _verify, "check every file against the index")
    verify.add_argument("directory", help="a directory that build wrote")

    site = command("site", _site, "write the page and the server configuration")
    site.add_argument("directory", help="a directory that build wrote")
    site.add_argument(
        "--base-url", metavar="URL", help="where this directory will be served from"
    )
    site.add_argument(
        "--root-url",
        metavar="URL",
        help="the public root:// endpoint (default: the base URL's host)",
    )
    site.add_argument(
        "--title", default="Open datasets, as ROOT files", help="what the page calls itself"
    )

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if getattr(args, "compression", None) == "none":
        args.compression = None
    config = config_from(args)
    try:
        return int(args.handler(args, config))
    except (XRootDError, OSError, ValueError) as exc:
        return fail(PROGRAM, exc)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
