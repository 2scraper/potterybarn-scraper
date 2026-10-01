#!/usr/bin/env python3
"""Cut the committed samples from REAL runs — never write them by hand.

    python3 api_scraper.py --group-id sofa --all-pages --out live/sofa_api
    python3 http_scraper.py --url .../delaney-marble-end-table/ \
        --url .../york-slope-arm-deep-slipcovered-sofa-collection/ --out live/pdp_http
    python3 tools/cut_samples.py

sample_output.* is the first 24 listing rows (one API page); sample_skus.json
is every SKU of the simple product and the first 8 of the guided one. Each
sidecar is the real run's own, so it records how many rows the run had.
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
import output_writer as W  # noqa: E402


def load(prefix):
    rows = json.load(open(os.path.join(HERE, prefix + ".json"), encoding="utf-8"))
    meta = json.load(open(os.path.join(HERE, prefix + ".meta.json"), encoding="utf-8"))
    return rows, meta


def main():
    rows, meta = load("live/sofa_api")
    listing = [W.Product(**r) for r in rows[:24]]
    W.write_json(listing, os.path.join(HERE, "sample_output.json"))
    W.write_csv(listing, os.path.join(HERE, "sample_output.csv"), row_cls=W.Product)
    json.dump(meta, open(os.path.join(HERE, "sample_output.meta.json"), "w"), indent=2)
    rows, meta = load("live/pdp_http")
    by_product = {}
    for r in rows:
        by_product.setdefault(r["product_id"], []).append(r)
    cut = []
    for product_id, group in by_product.items():
        cut += group if len(group) <= 8 else group[:8]
    W.write_json([W.SkuRow(**r) for r in cut], os.path.join(HERE, "sample_skus.json"))
    json.dump(meta, open(os.path.join(HERE, "sample_skus.meta.json"), "w"), indent=2)
    print("sample_output: %d rows, sample_skus: %d rows" % (len(listing), len(cut)))


if __name__ == "__main__":
    main()
