#!/usr/bin/env python3
"""Select open, scalar Parquet datasets from the Hugging Face Hub.

The Hub index, dataset cards and dataset-viewer endpoints are queried at
generation time.  Runtime imports never touch the network: the resulting
module fixes the selected repository, licence, converted Parquet URLs, byte
counts, splits and feature schema so a later build either sees exactly that
shape or fails loudly.

Only an explicit, recognized licence is admitted.  ``public``, a missing
licence, gated repositories, incomplete viewer conversions and nested media
features are deliberately refused rather than interpreted optimistically.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import keyword
import pprint
import re
import time
import urllib.parse
import urllib.request
from collections import Counter
from pathlib import Path
from typing import Any

AGENT = "PyXRootDClient open Hub dataset inventory"
HUB = "https://huggingface.co/api/datasets"
VIEWER = "https://datasets-server.huggingface.co"

# Normalized output statement, canonical URL and whether this is a permissive
# (rather than reciprocal/copyleft) licence.  Every one permits redistribution
# and adaptation, subject to its own notice/share-alike requirements.
OPEN_LICENCES: dict[str, tuple[str, str, bool]] = {
    "afl-3.0": ("AFL-3.0", "https://spdx.org/licenses/AFL-3.0.html", True),
    "apache-2.0": ("Apache-2.0", "https://www.apache.org/licenses/LICENSE-2.0", True),
    "artistic-2.0": (
        "Artistic-2.0",
        "https://opensource.org/license/artistic-2-0/",
        True,
    ),
    "bsl-1.0": ("BSL-1.0", "https://www.boost.org/LICENSE_1_0.txt", True),
    "bsd-2-clause": (
        "BSD-2-Clause",
        "https://opensource.org/license/bsd-2-clause/",
        True,
    ),
    "bsd-3-clause": (
        "BSD-3-Clause",
        "https://opensource.org/license/bsd-3-clause/",
        True,
    ),
    "cc-by-2.0": ("CC BY 2.0", "https://creativecommons.org/licenses/by/2.0/", True),
    "cc-by-2.5": ("CC BY 2.5", "https://creativecommons.org/licenses/by/2.5/", True),
    "cc-by-3.0": (
        "CC BY 3.0",
        "https://creativecommons.org/licenses/by/3.0/",
        True,
    ),
    "cc-by-4.0": (
        "CC BY 4.0",
        "https://creativecommons.org/licenses/by/4.0/",
        True,
    ),
    "cc-by-sa-3.0": (
        "CC BY-SA 3.0",
        "https://creativecommons.org/licenses/by-sa/3.0/",
        False,
    ),
    "cc-by-sa-4.0": (
        "CC BY-SA 4.0",
        "https://creativecommons.org/licenses/by-sa/4.0/",
        False,
    ),
    "cc0-1.0": (
        "CC0",
        "https://creativecommons.org/publicdomain/zero/1.0/",
        True,
    ),
    "cdla-permissive-1.0": (
        "CDLA-Permissive-1.0",
        "https://spdx.org/licenses/CDLA-Permissive-1.0.html",
        True,
    ),
    "cdla-permissive-2.0": (
        "CDLA-Permissive-2.0",
        "https://cdla.dev/permissive-2-0/",
        True,
    ),
    "ecl-2.0": ("ECL-2.0", "https://opensource.org/license/ecl-2-0/", True),
    "epl-2.0": ("EPL-2.0", "https://www.eclipse.org/legal/epl-2.0/", False),
    "etalab-2.0": (
        "Etalab Open Licence 2.0",
        "https://www.etalab.gouv.fr/licence-ouverte-open-licence/",
        True,
    ),
    "isc": ("ISC", "https://opensource.org/license/isc-license-txt/", True),
    "mit": ("MIT", "https://opensource.org/license/mit/", True),
    "mpl-2.0": ("MPL-2.0", "https://www.mozilla.org/MPL/2.0/", False),
    "ncsa": ("NCSA", "https://spdx.org/licenses/NCSA.html", True),
    "odc-by": (
        "ODC-By-1.0",
        "https://opendatacommons.org/licenses/by/1-0/",
        True,
    ),
    "odbl": ("ODbL-1.0", "https://opendatacommons.org/licenses/odbl/1-0/", False),
    "pddl": ("PDDL-1.0", "https://opendatacommons.org/licenses/pddl/1-0/", True),
    "postgresql": (
        "PostgreSQL",
        "https://www.postgresql.org/about/licence/",
        True,
    ),
    "unlicense": ("Unlicense", "https://unlicense.org/", True),
    "wtfpl": ("WTFPL", "https://spdx.org/licenses/WTFPL.html", True),
    "zlib": ("Zlib", "https://www.zlib.net/zlib_license.html", True),
}

SIZE_CATEGORIES = ("n<1K", "1K<n<10K", "10K<n<100K", "100K<n<1M")
VALUE_DTYPES = {
    "bool",
    "float16",
    "float32",
    "float64",
    "int8",
    "int16",
    "int32",
    "int64",
    "string",
    "uint8",
    "uint16",
    "uint32",
}
TARGET_WORDS = (
    "label",
    "labels",
    "class",
    "category",
    "target",
    "outcome",
    "score",
    "rating",
    "y",
)


def _json(url: str) -> Any:
    for attempt in range(4):
        request = urllib.request.Request(url, headers={"User-Agent": AGENT})
        try:
            with urllib.request.urlopen(request, timeout=90) as response:
                return json.load(response)
        except OSError:
            if attempt == 3:
                raise
            time.sleep(1 + attempt)
    raise AssertionError("the retry loop always returns or raises")


def _query(base: str, parameters: list[tuple[str, str]]) -> Any:
    return _json(f"{base}?{urllib.parse.urlencode(parameters)}")


def _identifier(value: str, *, fallback: str) -> str:
    made = re.sub(r"[^0-9A-Za-z]+", "_", value.strip()).strip("_").lower()
    if not made:
        made = fallback
    if made[0].isdigit():
        made = f"v_{made}"
    if keyword.iskeyword(made) or made in {"index", "label", "target"}:
        made = f"source_{made}"
    return made


def _unique(values: list[str]) -> list[str]:
    counts: dict[str, int] = {}
    result = []
    for value in values:
        counts[value] = counts.get(value, 0) + 1
        result.append(value if counts[value] == 1 else f"{value}_{counts[value]}")
    return result


def _licence(card: dict[str, Any]) -> str | None:
    value = card.get("license")
    choices = value if isinstance(value, list) else [value]
    normalized = [str(item).strip().lower() for item in choices if item]
    return next((item for item in normalized if item in OPEN_LICENCES), None)


def _candidate_rows(licence: str, size: str, per_query: int) -> list[dict[str, Any]]:
    return _query(
        HUB,
        [
            ("filter", f"license:{licence}"),
            ("filter", f"size_categories:{size}"),
            ("sort", "downloads"),
            ("direction", "-1"),
            ("limit", str(per_query)),
            ("full", "true"),
        ],
    )


def _admissible_candidate(item: dict[str, Any], licence: str) -> bool:
    card = item.get("cardData") or {}
    return bool(
        item.get("id")
        and item.get("gated") is False
        and not item.get("private")
        and not item.get("disabled")
        and _licence(card) == licence
    )


def _remember_candidate(found: dict[str, dict[str, Any]], item: dict[str, Any]) -> None:
    previous = found.get(item["id"])
    if previous is None or int(item.get("downloads", 0)) > int(previous.get("downloads", 0)):
        found[item["id"]] = item


def _candidates(per_query: int) -> list[dict[str, Any]]:
    found: dict[str, dict[str, Any]] = {}
    for licence in OPEN_LICENCES:
        for size in SIZE_CATEGORIES:
            for item in _candidate_rows(licence, size, per_query):
                if _admissible_candidate(item, licence):
                    _remember_candidate(found, item)
    return sorted(
        found.values(),
        key=lambda item: (-int(item.get("downloads", 0)), item["id"].casefold()),
    )


def _class_feature(kind: dict[str, Any], source: str, at: int) -> dict[str, Any] | None:
    names = kind.get("names")
    if not isinstance(names, list) or not 2 <= len(names) <= 100:
        return None
    return {
        "source": source,
        "branch": _identifier(source, fallback=f"field_{at}"),
        "dtype": "classlabel",
        "classes": [str(name) for name in names],
    }


def _value_feature(kind: dict[str, Any], source: str, at: int) -> dict[str, Any] | None:
    dtype = str(kind.get("dtype", "")).lower()
    if dtype not in VALUE_DTYPES:
        return None
    return {
        "source": source,
        "branch": _identifier(source, fallback=f"field_{at}"),
        "dtype": dtype,
    }


def _feature(feature: dict[str, Any], at: int) -> dict[str, Any] | None:
    kind = feature.get("type") or {}
    source = str(feature.get("name", "")).strip()
    if not isinstance(kind, dict) or not source:
        return None
    if kind.get("_type") == "ClassLabel":
        return _class_feature(kind, source, at)
    if kind.get("_type") == "Value":
        return _value_feature(kind, source, at)
    return None


def _target_word(feature: dict[str, Any]) -> int:
    name = feature["source"].strip().casefold()
    return TARGET_WORDS.index(name) if name in TARGET_WORDS else len(TARGET_WORDS)


def _indices_with_dtype(features: list[dict[str, Any]], dtype: str) -> list[int]:
    return [at for at, feature in enumerate(features) if feature["dtype"] == dtype]


def _numeric_indices(features: list[dict[str, Any]]) -> list[int]:
    excluded = {"string", "bool"}
    return [at for at, feature in enumerate(features) if feature["dtype"] not in excluded]


def _target(features: list[dict[str, Any]]) -> int | None:
    labelled = _indices_with_dtype(features, "classlabel")
    if labelled:
        return min(labelled, key=lambda at: (_target_word(features[at]), -at))
    numeric = _numeric_indices(features)
    named = [at for at in numeric if _target_word(features[at]) < len(TARGET_WORDS)]
    candidates = named or numeric
    return candidates[-1] if candidates else None


def _parquet_inventory(
    repo: str, max_source_bytes: int
) -> tuple[tuple[list[dict[str, Any]], list[str], str, int] | None, str]:
    parquet, reason = _parquet_response(repo)
    if parquet is None:
        return None, reason
    return _validated_inventory(parquet, max_source_bytes)


def _parquet_response(repo: str) -> tuple[dict[str, Any] | None, str]:
    try:
        parquet = _query(f"{VIEWER}/parquet", [("dataset", repo)])
    except OSError as exc:
        return None, f"parquet API: {exc}"
    return parquet, ""


def _validated_inventory(
    parquet: dict[str, Any], max_source_bytes: int
) -> tuple[tuple[list[dict[str, Any]], list[str], str, int] | None, str]:
    if _conversion_incomplete(parquet):
        return None, "incomplete Parquet conversion"
    files = parquet.get("parquet_files") or []
    configs = {file.get("config") for file in files}
    if not _one_configuration(files, configs):
        return None, "not exactly one converted configuration"
    splits = sorted({str(file.get("split")) for file in files})
    if not _supported_shards(files, splits):
        return None, "unsupported split or shard count"
    total = _source_size(files, max_source_bytes)
    if total is None:
        return None, "source size"
    return (files, splits, str(next(iter(configs))), total), ""


def _conversion_incomplete(parquet: dict[str, Any]) -> bool:
    return any(parquet.get(state) for state in ("pending", "failed", "partial"))


def _one_configuration(files: list[dict[str, Any]], configs: set[Any]) -> bool:
    return bool(files) and len(configs) == 1


def _supported_shards(files: list[dict[str, Any]], splits: list[str]) -> bool:
    return 1 <= len(splits) <= 10 and len(files) <= 64


def _source_size(files: list[dict[str, Any]], maximum: int) -> int | None:
    total = sum(int(file.get("size") or 0) for file in files)
    return total if 0 < total < maximum else None


def _preview_features(
    repo: str, config: str, split: str
) -> tuple[list[dict[str, Any]] | None, str]:
    try:
        preview = _query(
            f"{VIEWER}/first-rows",
            [("dataset", repo), ("config", config), ("split", split)],
        )
    except OSError as exc:
        return None, f"schema API: {exc}"
    raw_features = preview.get("features") or []
    features = [_feature(feature, at) for at, feature in enumerate(raw_features)]
    if not _scalar_features(features):
        return None, "nested or unsupported feature type"
    typed = [feature for feature in features if feature is not None]
    _unique_branches(typed)
    return typed, ""


def _scalar_features(features: list[dict[str, Any] | None]) -> bool:
    return bool(features) and all(feature is not None for feature in features)


def _unique_branches(features: list[dict[str, Any]]) -> None:
    branches = _unique([feature["branch"] for feature in features])
    for feature, branch in zip(features, branches):
        feature["branch"] = branch


def _source_maps(
    files: list[dict[str, Any]], splits: list[str]
) -> tuple[dict[str, str], dict[str, int], dict[str, list[str]]]:
    roles: dict[str, str] = {}
    sizes: dict[str, int] = {}
    split_roles: dict[str, list[str]] = {split: [] for split in splits}
    ordered = sorted(files, key=lambda file: (str(file["split"]), str(file["filename"])))
    per_split: Counter[str] = Counter()
    for file in ordered:
        split = str(file["split"])
        number = per_split[split]
        per_split[split] += 1
        role = f"{_identifier(split, fallback='split')}_{number:03d}"
        roles[role] = str(file["url"])
        sizes[role] = int(file["size"])
        split_roles[split].append(role)
    return roles, sizes, split_roles


def _metadata(item: dict[str, Any]) -> tuple[list[Any], list[str]]:
    card = item.get("cardData") or {}
    task_categories = card.get("task_categories") or []
    if isinstance(task_categories, str):
        task_categories = [task_categories]
    modalities = [
        tag.removeprefix("modality:")
        for tag in item.get("tags", [])
        if str(tag).startswith("modality:")
    ]
    return task_categories, modalities


def _declaration(
    item: dict[str, Any],
    licence_slug: str,
    files: list[dict[str, Any]],
    splits: list[str],
    total: int,
    typed: list[dict[str, Any]],
    target: int,
) -> dict[str, Any]:
    repo = item["id"]
    roles, sizes, split_roles = _source_maps(files, splits)
    statement, licence_url, permissive = OPEN_LICENCES[licence_slug]
    task_categories, modalities = _metadata(item)
    target_name = typed[target]["source"]
    name = f"hub_{_identifier(repo, fallback='dataset')}"
    task = ", ".join(str(category) for category in task_categories)
    if not task:
        task = "classification" if typed[target]["dtype"] == "classlabel" else "regression"
    return {
        "name": name,
        "label": repo,
        "title": (
            f"{repo}: {len(typed)} published scalar fields with "
            f"{target_name} designated as the teaching target"
        ),
        "licence": statement,
        "licence_url": licence_url,
        "licence_slug": licence_slug,
        "permissive": permissive,
        "source": f"https://huggingface.co/datasets/{repo}",
        "revision": item.get("sha", ""),
        "source_bytes": total,
        "sources": roles,
        "source_sizes": sizes,
        "splits": splits,
        "split_roles": split_roles,
        "features": typed,
        "target": target,
        "requires": ["pyarrow"],
        "modality": ", ".join(modalities) if modalities else "tabular",
        "task": task,
        "downloads": int(item.get("downloads", 0)),
        "transformation": (
            "read the Hub dataset viewer's complete Parquet conversion one record batch at "
            "a time; preserved every scalar source field and official split without scaling; "
            "encoded ClassLabel metadata as integers, retained text with explicit byte "
            f"lengths, and exposed {target_name} as the label or numeric target in one ROOT "
            "TTree per split"
        ),
    }


def _inspect(item: dict[str, Any], max_source_bytes: int) -> tuple[dict[str, Any] | None, str]:
    repo = item["id"]
    card = item.get("cardData") or {}
    licence_slug = _licence(card)
    if licence_slug is None:
        return None, "licence"
    inventory, reason = _parquet_inventory(repo, max_source_bytes)
    if inventory is None:
        return None, reason
    files, splits, config, total = inventory

    first_split = splits[0]
    typed, reason = _preview_features(repo, config, first_split)
    if typed is None:
        return None, reason
    target = _target(typed)
    if target is None or len(typed) < 2:
        return None, "no scalar prediction target"
    return _declaration(item, licence_slug, files, splits, total, typed, target), ""


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("module", type=Path)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--count", type=int, default=500)
    parser.add_argument("--per-query", type=int, default=180)
    parser.add_argument("--probe", type=int, default=10_000)
    parser.add_argument("--jobs", type=int, default=24)
    parser.add_argument("--max-source-bytes", type=int, default=2_000_000_000)
    return parser.parse_args()


def _probe(
    candidates: list[dict[str, Any]], jobs: int, max_source_bytes: int
) -> tuple[list[dict[str, Any]], Counter[str]]:
    accepted: list[dict[str, Any]] = []
    refused: Counter[str] = Counter()
    with concurrent.futures.ThreadPoolExecutor(max_workers=jobs) as pool:
        running = {pool.submit(_inspect, item, max_source_bytes): item["id"] for item in candidates}
        for future in concurrent.futures.as_completed(running):
            try:
                record, reason = future.result()
            except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
                refused[f"request or metadata error: {type(exc).__name__}"] += 1
                continue
            if record is None:
                refused[reason] += 1
            else:
                accepted.append(record)
    return accepted, refused


def _choose(
    accepted: list[dict[str, Any]],
    candidates: list[dict[str, Any]],
    count: int,
    refused: Counter[str],
) -> list[dict[str, Any]]:
    accepted.sort(key=lambda item: (item["source_bytes"], -item["downloads"], item["name"]))
    if len(accepted) < count:
        raise ValueError(
            f"only {len(accepted)} of {len(candidates)} probes were admissible; "
            f"increase --per-query or --probe; refusals were {dict(refused)}"
        )
    selected = accepted[:count]
    names = [item["name"] for item in selected]
    if len(names) != len(set(names)):
        raise ValueError("the selected Hub repository names do not normalize uniquely")
    return selected


def _write_module(path: Path, selected: list[dict[str, Any]]) -> None:
    rendered = pprint.pformat(tuple(selected), width=100, sort_dicts=False)
    path.write_text(
        '"""Generated declarations for explicitly licensed Hub Parquet datasets."""\n\n'
        "# Generated source URLs and repository names may exceed the style line length.\n"
        "# ruff: noqa: E501\n\n"
        "from __future__ import annotations\n\n"
        "from typing import Any\n\n"
        f"HUB_OPEN: tuple[dict[str, Any], ...] = {rendered}\n"
    )


def _manifest_document(
    candidates: list[dict[str, Any]],
    accepted: list[dict[str, Any]],
    selected: list[dict[str, Any]],
    refused: Counter[str],
) -> dict[str, Any]:
    source_total = sum(item["source_bytes"] for item in selected)
    return {
        "format": 1,
        "sources": {
            "hub": HUB,
            "parquet": f"{VIEWER}/parquet",
            "schema": f"{VIEWER}/first-rows",
        },
        "policy": (
            "explicit recognized open licence; public and ungated; complete single-config "
            "Parquet conversion; scalar schema; numeric or ClassLabel teaching target"
        ),
        "statistics": {
            "candidates": len(candidates),
            "accepted_before_ranking": len(accepted),
            "selected": len(selected),
            "source_bytes": source_total,
            "permissive": sum(bool(item["permissive"]) for item in selected),
            "licences": dict(sorted(Counter(item["licence"] for item in selected).items())),
            "refused": dict(sorted(refused.items())),
        },
        "datasets": [
            {
                key: item[key]
                for key in (
                    "name",
                    "label",
                    "source",
                    "revision",
                    "licence",
                    "licence_url",
                    "permissive",
                    "source_bytes",
                    "splits",
                    "task",
                    "modality",
                )
            }
            for item in selected
        ],
    }


def main() -> int:
    args = _arguments()

    candidates = _candidates(args.per_query)[: args.probe]
    accepted, refused = _probe(candidates, args.jobs, args.max_source_bytes)
    selected = _choose(accepted, candidates, args.count, refused)
    _write_module(args.module, selected)
    document = _manifest_document(candidates, accepted, selected, refused)
    args.manifest.write_text(json.dumps(document, indent=2) + "\n")
    source_total = document["statistics"]["source_bytes"]
    print(
        f"wrote {len(selected)} datasets ({source_total} bytes) "
        f"to {args.module} and {args.manifest}; {len(accepted)} admissible probes"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
