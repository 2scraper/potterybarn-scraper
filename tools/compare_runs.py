#!/usr/bin/env python3
"""Compare the iterations `autosolve_probe.py` produced, and judge them.

Three questions, reported separately because they fail differently:

* **Consistency** — do repeats of the SAME target agree?
* **Correctness** — do the rows satisfy invariants that hold whatever the
  site did? Checks against arithmetic and the site's own structure, not a
  golden file, so they stay valid as the catalogue changes.
* **The ladder** — what did the challenge machinery actually do?

    python3 tools/compare_runs.py --in live/autosolve
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter, defaultdict

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

# Fields that describe the product or SKU rather than the moment: identical
# for the same sku across runs minutes apart.
STABLE = {
    "category": ("sku", "product_id", "title", "url", "currency", "leader_sku",
                 "pip_type"),
    "product": ("sku", "product_id", "title", "url", "currency", "options",
                "product_title", "category_hierarchy"),
}
AVAILABILITY = ("ON_HAND", "BACK_ORDERED", "NLA")


def load(prefix):
    with open(prefix + ".jsonl", encoding="utf-8") as handle:
        records = [json.loads(line) for line in handle if line.strip()]
    with open(prefix + ".rows.json", encoding="utf-8") as handle:
        rows = json.load(handle)
    return records, rows


def invariants(records, rows_by_iteration):
    """[(name, offenders, detail)]."""
    checks = []
    by_mode = defaultdict(list)
    for record in records:
        by_mode[record["mode"]].extend(rows_by_iteration.get(str(record["iteration"]), []))
    listing, skus = by_mode.get("category", []), by_mode.get("product", [])
    every = listing + skus

    def add(name, bad):
        checks.append((name, len(bad), str(bad[:2])[:60] if bad else ""))

    add("every row has a sku", [r for r in every if not r.get("sku")])
    add("every row has a product_id", [r for r in every if not r.get("product_id")])
    add("a listing row's sku is its product id, or it names its sub-group",
        [r["sku"] for r in listing if r.get("sku") != r.get("product_id")
         and r.get("sku") != r.get("sub_group_id")])
    add("a listing row's url PATH ends in its product id",
        [r["sku"] for r in listing
         if not (r.get("url") or "").split("?")[0].rstrip("/").endswith(
             "/" + (r.get("product_id") or "?"))])
    add("a SKU row's sku is NOT its product id",
        [r["sku"] for r in skus if r.get("sku") == r.get("product_id")])
    add("a SKU row's url ends in its product id",
        [r["sku"] for r in skus
         if not (r.get("url") or "").rstrip("/").endswith("/" + (r.get("product_id") or "?"))])
    add("a priced row states a currency",
        [r["sku"] for r in every if r.get("price") is not None and not r.get("currency")])
    add("currency is USD where stated",
        [r["sku"] for r in every if r.get("currency") not in (None, "USD")])
    add("no price is zero or negative",
        [r["sku"] for r in every if r.get("price") is not None and r["price"] <= 0])
    add("prices are dollars, not cents (no sofa under $100)",
        [r["sku"] for r in every if "sofa" in (r.get("product_id") or "")
         and r.get("price") is not None and r["price"] < 100])
    add("a listing row's price never exceeds its price_max",
        [r["sku"] for r in listing if r.get("price") is not None
         and r.get("price_max") is not None and r["price"] > r["price_max"]])
    add("original_price is never at or below price",
        [r["sku"] for r in every if r.get("original_price") is not None
         and r.get("price") is not None and r["original_price"] <= r["price"]])
    add("discount_pct agrees with the two prices it came from",
        [r["sku"] for r in skus if r.get("discount_pct") is not None
         and abs(r["discount_pct"] - round((r["original_price"] - r["price"])
                                           / r["original_price"] * 100, 2)) > 0.011])
    add("availability is one of ON_HAND / BACK_ORDERED / NLA",
        [r["sku"] for r in skus if r.get("availability") not in AVAILABILITY])
    add("a back-order date appears only on a BACK_ORDERED row",
        [r["sku"] for r in skus if r.get("back_ordered_date")
         and r.get("availability") != "BACK_ORDERED"])
    add("no duplicate sku within one iteration",
        [it for it, rows in rows_by_iteration.items()
         if len({r["sku"] for r in rows}) != len(rows)])
    add("every product iteration's SKU count closes against skuCount",
        [r["iteration"] for r in records if r["mode"] == "product" and r.get("sku_check")
         and not r["sku_check"].get("closes")])
    add("a hub yields zero rows",
        [r["iteration"] for r in records if r["mode"] == "hub" and r["rows"]])

    # Cross-source: a listing row's range and its product page's own
    # aggregate range are two reports of the same fact.
    pdp_range = {}
    for record in records:
        rows = rows_by_iteration.get(str(record["iteration"]), [])
        if record["mode"] == "product" and rows:
            # NLA (no longer available) SKUs keep their last price, and the
            # site's own range leaves them out: York 2026-10-01, all 1,380 SKUs
            # 1679-4199, the 1,235 not NLA 1839-4199 = the listing and the
            # page's aggregatePrice.
            priced = [r["price"] for r in rows if r.get("price") is not None
                      and r.get("availability") != "NLA"]
            if priced:
                pdp_range[rows[0]["product_id"]] = (min(priced), max(priced))
    crossed = [(r["sku"], (r.get("price"), r.get("price_max")), pdp_range[r["sku"]])
               for r in listing if r["sku"] in pdp_range]
    add("listing price range == the min/max of the product's non-NLA SKUs",
        [c for c in crossed if (c[1][0], c[1][1]) != c[2]])
    return checks, len(every), len(crossed)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--in", dest="prefix", default="live/autosolve")
    args = parser.parse_args(argv)
    records, rows_by_iteration = load(args.prefix)

    print("=" * 72)
    print("1. WHAT EACH ITERATION DID")
    print("=" * 72)
    for record in records:
        states = ",".join(s["state"] for s in record["states"]) or "-"
        note = []
        if record["error"]:
            note.append("ERR " + record["error"][:40])
        if record["captcha_events"]:
            note.append("%d captcha event(s)" % len(record["captcha_events"]))
        print("%-3d %-20s %-8s %-18s %6d %7d %s" % (
            record["iteration"], record["target"], record["mode"], states,
            record["rows"], record["ms"], "; ".join(note)))

    print("\n" + "=" * 72)
    print("2. THE LADDER — did auto-solve fire?")
    print("=" * 72)
    armed = sum(1 for r in records if r.get("autosolve_armed"))
    print("iterations with setAutoSolve accepted : %d/%d" % (armed, len(records)))
    events = Counter(e["event"] for r in records for e in r["captcha_events"])
    print("Captcha CDP events observed           : %s" % (dict(events) or "NONE"))
    states = Counter(s["state"] for r in records for s in r["states"])
    print("page states over all iterations       : %s" % dict(states))

    print("\n" + "=" * 72)
    print("3. CONSISTENCY — do repeats of one target agree?")
    print("=" * 72)
    by_target = defaultdict(list)
    for record in records:
        if record["rows"] and not record["error"]:
            by_target[record["target"]].append(record)
    any_repeat = False
    for target, group in sorted(by_target.items()):
        if len(group) < 2:
            continue
        any_repeat = True
        stable = STABLE["category" if group[0]["mode"] == "category" else "product"]
        base = {r["sku"]: r for r in rows_by_iteration[str(group[0]["iteration"])]}
        print("\n%s — %d successful runs" % (target, len(group)))
        for record in group:
            current = {r["sku"]: r for r in rows_by_iteration[str(record["iteration"])]}
            diffs = sum(1 for sku in set(current) & set(base) for f in stable
                        if current[sku].get(f) != base[sku].get(f))
            print("  run %-3d rows %-5d sku set %-22s stable-field diffs %d" % (
                record["iteration"], record["rows"],
                "IDENTICAL" if set(current) == set(base)
                else "differs by %d" % len(set(current) ^ set(base)), diffs))
    if not any_repeat:
        print("  (no target ran more than once successfully)")

    print("\n" + "=" * 72)
    print("4. CORRECTNESS — invariants")
    print("=" * 72)
    checks, total, crossed = invariants(records, rows_by_iteration)
    width = max(len(name) for name, _, _ in checks)
    failures = 0
    for name, bad, detail in checks:
        failures += bool(bad)
        print("  %s %-*s  %s" % ("ok  " if not bad else "FAIL", width, name,
                                 "" if not bad else "%d offender(s) %s" % (bad, detail)))
    print("\n  %d rows checked, %d listing rows cross-checked against a product "
          "page, %d invariant(s) violated" % (total, crossed, failures))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
