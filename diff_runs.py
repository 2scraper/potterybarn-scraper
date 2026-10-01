#!/usr/bin/env python3
"""
diff_runs.py
-------------
Compares two JSON outputs of this project and reports what changed, keyed
on `sku`.

    python3 diff_runs.py --old sofas.2026-09-24.json --new sofas.2026-09-25.json

Five buckets: added, removed, changed, source_changed (a price difference
that arrives WITH a `price_source` difference — our two readings disagreeing,
not the shelf price moving; ignored by --fail-on-change), and unmatched.

Two row kinds, and it refuses to mix them
-----------------------------------------
A `--mode category` run has one row per PRODUCT and its `sku` is the product
slug; a `--mode product` run has one row per SKU and its `sku` is the SKU id
(see output_writer). A diff across the two would report every row as added
and removed, so a mode mismatch in the sidecars is refused. The tracked
fields differ per mode for the same reason.

Not tracked, each for a reason: `scraped_at` (the clock), `page` and
`row_index` (a result's position moves when the site re-ranks the grid; it
is not a change to the product), `image_url` (a re-shot image is not a price
or stock event).
"""


import argparse
import json
import re
import sys
from typing import Dict, List, Tuple

from output_writer import UNIQUE_BY_SKU_MODES

TRACKED_FIELDS_BY_MODE = {
    # A product's range, its markdown, and how it is sold.
    "category": ("price", "currency", "price_max", "original_price",
                 "original_price_max", "max_discount_pct", "title",
                 "leader_sku", "price_type", "flags"),
    # One SKU's price, markdown and stock.
    "product": ("price", "currency", "original_price", "discount_pct",
                "availability", "back_ordered_date", "sellable", "title"),
}
TRACKED_FIELDS = TRACKED_FIELDS_BY_MODE["category"]


def _load(path: str) -> List[dict]:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _by_sku(rows: List[dict]) -> Tuple[Dict[str, dict], int]:
    indexed = {}
    unmatchable = 0
    for r in rows:
        sku = r.get("sku")
        if sku is None:
            unmatchable += 1
            continue
        if sku in indexed:
            unmatchable += 1
            continue
        indexed[sku] = r
    return indexed, unmatchable


def diff_rows(old: List[dict], new: List[dict], tracked=TRACKED_FIELDS) -> dict:
    old_by_sku, old_unmatchable = _by_sku(old)
    new_by_sku, new_unmatchable = _by_sku(new)

    added = [new_by_sku[sku] for sku in new_by_sku.keys() - old_by_sku.keys()]
    removed = [old_by_sku[sku] for sku in old_by_sku.keys() - new_by_sku.keys()]

    changed = []
    source_changed = []
    for sku in old_by_sku.keys() & new_by_sku.keys():
        before, after = old_by_sku[sku], new_by_sku[sku]
        field_changes = {
            field: {"old": before.get(field), "new": after.get(field)}
            for field in tracked
            if before.get(field) != after.get(field)
        }
        if not field_changes:
            continue
        entry = {"sku": sku, "title": after.get("title"),
                 "changes": field_changes}
        # A price difference that arrives WITH a price_source difference is
        # our two instruments disagreeing, not the shelf price moving. It
        # goes in its own bucket and --fail-on-change ignores it.
        if "price" in field_changes and \
                before.get("price_source") != after.get("price_source"):
            entry["price_source"] = {"old": before.get("price_source"),
                                     "new": after.get("price_source")}
            source_changed.append(entry)
        else:
            changed.append(entry)

    return {
        "added": added,
        "removed": removed,
        "changed": changed,
        "source_changed": source_changed,
        "unmatchable_old": old_unmatchable,
        "unmatchable_new": new_unmatchable,
    }


def _print_summary(result: dict) -> None:
    print(f"[+] {len(result['added'])} added, {len(result['removed'])} removed, "
          f"{len(result['changed'])} changed, "
          f"{len(result.get('source_changed', []))} read differently.")
    for r in result["added"]:
        print(f"  + {r.get('sku')}  {r.get('title')}  {r.get('price')} {r.get('currency')}")
    for r in result["removed"]:
        print(f"  - {r.get('sku')}  {r.get('title')}  {r.get('price')} {r.get('currency')}")
    for c in result["changed"]:
        deltas = ", ".join(f"{f}: {v['old']!r} -> {v['new']!r}" for f, v in c["changes"].items())
        print(f"  ~ {c['sku']}  {c['title']}  {deltas}")
    for c in result.get("source_changed", []):
        src = c.get("price_source", {})
        deltas = ", ".join(f"{f}: {v['old']!r} -> {v['new']!r}"
                           for f, v in c["changes"].items())
        print(f"  ? {c['sku']}  {c['title']}  {deltas}  "
              f"[read differently: {src.get('old')!r} -> {src.get('new')!r}, "
              f"not counted as a price change]")
    unmatchable = result["unmatchable_old"] + result["unmatchable_new"]
    if unmatchable:
        print(f"[!] {unmatchable} row(s) across both files had no sku or a "
              f"duplicate sku, and could not be matched across runs.")


def _run_status(path: str):
    meta_path = re.sub(r"\.json$", "", path) + ".meta.json"
    try:
        with open(meta_path, "r", encoding="utf-8") as f:
            meta = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None, None
    return meta.get("status"), meta


def _check_comparable(args) -> bool:
    """Refuse a diff that has no evidence it compares like with like.

    FAILS CLOSED. The first version skipped a file whose `.meta.json` was
    missing, so a listing (Product) file and a SKU (SkuRow) file with no
    sidecars were compared and reported "12 added, 24 removed" (audit,
    2026-10-01). Now a missing or unreadable sidecar is a refusal, and so is
    any disagreement in what the two runs were: mode, schema, source, the
    listing group, the currency. A partial run is refused because its
    unfetched pages would read as delistings. `--force` overrides all of it.
    """
    problems = []
    metas = {}
    for label, path in (("--old", args.old), ("--new", args.new)):
        status, meta = _run_status(path)
        if meta is None:
            problems.append(
                f"{label} ({path}) has no readable .meta.json beside it, so "
                f"nothing says what kind of run it was.")
            continue
        metas[label] = meta
        mode = meta.get("mode")
        if mode not in UNIQUE_BY_SKU_MODES:
            problems.append(f"{label} ({path}) is a {mode!r} run, which this tool "
                            f"does not know how to diff.")
        if status != "complete":
            problems.append(
                f"{label} ({path}) was a {status!r} run — stopped after "
                f"{meta.get('pages_completed')} of {meta.get('pages_requested')} "
                f"page(s), reason {meta.get('stop_reason')!r}")
    if len(metas) == 2:
        old, new = metas["--old"], metas["--new"]
        for key, what in (("mode", "modes"), ("schema_version", "row schemas"),
                          ("source", "sources")):
            if old.get(key) != new.get(key):
                problems.append(f"the two runs have different {what} "
                                f"({old.get(key)!r} vs {new.get(key)!r}).")
        for key in ("group_id", "currency"):
            a, b = (old.get("scope") or {}).get(key), (new.get("scope") or {}).get(key)
            if a != b:
                problems.append(f"the two runs cover a different {key} ({a!r} vs {b!r}).")
    if not problems:
        return True
    print("[!] Refusing to diff these two runs:")
    for line in problems:
        print(f"      {line}")
    print("    Re-run the side in question, or pass --force to compare anyway "
          "(added/removed will then describe the difference between the runs, "
          "not the catalogue).")
    return False


def parse_args():
    p = argparse.ArgumentParser(
        description="Diff two potterybarn-scraper JSON outputs by sku.")
    p.add_argument("--old", required=True, help="Earlier run's JSON output.")
    p.add_argument("--new", required=True, help="Later run's JSON output.")
    p.add_argument("--out", default=None,
                   help="Write the full diff as JSON to this path too.")
    p.add_argument("--fail-on-change", action="store_true",
                   help="Exit 1 if anything was added, removed or changed — "
                        "for a cron job that should only notify on a real diff.")
    p.add_argument("--force", action="store_true",
                   help="Diff even when a run's .meta.json says it was "
                        "partial or failed, or the modes differ.")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    if not args.force and not _check_comparable(args):
        return 2

    try:
        old = _load(args.old)
        new = _load(args.new)
    except (OSError, json.JSONDecodeError) as e:
        print(f"[!] Could not read one of the input files: {e}")
        return 2

    mode = None
    status, meta = _run_status(args.new)
    if meta:
        mode = meta.get("mode")
    result = diff_rows(old, new, TRACKED_FIELDS_BY_MODE.get(mode, TRACKED_FIELDS))
    _print_summary(result)

    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        print(f"[+] Full diff written to {args.out}")

    if args.fail_on_change and (result["added"] or result["removed"] or result["changed"]):
        return 1
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(1)
