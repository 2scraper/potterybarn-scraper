#!/usr/bin/env python3
"""potterybarn-scraper — the offline suite.

One file of plain functions with fixtures cut from real captures (see
fixtures/README.md). `tests/test_smoke.py` wraps it as one pytest test.

It passes with NO engine library installed: every engine import is guarded
and records a skip, and the engines import their driver at MODULE level
(asserted below), so "skipped" really means "absent".

    python3 smoke_test.py
"""

from __future__ import annotations

import argparse
import ast
import base64
import dataclasses
import gzip
import io
import json
import os
import re
import subprocess
import sys
import tempfile
from contextlib import redirect_stdout, redirect_stderr
from typing import List
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURES = os.path.join(HERE, "fixtures")
sys.path.insert(0, HERE)

import output_writer as W  # noqa: E402
import product_parser as P  # noqa: E402

PASSES: List[str] = []
FAILURES: List[str] = []
SKIPS: List[str] = []


def check(name, condition, detail=""):
    if condition:
        PASSES.append(name)
        print("PASS %s" % name)
    else:
        FAILURES.append("%s %s" % (name, detail))
        print("FAIL %s %s" % (name, detail))


def eq(name, got, want):
    check(name, got == want, "(got %r, want %r)" % (got, want))


def fixture(name):
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as handle:
        return handle.read()


def quiet(fn, *a, **kw):
    """Run `fn` with stdout/stderr captured; return (result, captured text)."""
    out = io.StringIO()
    with redirect_stdout(out), redirect_stderr(out):
        result = fn(*a, **kw)
    return result, out.getvalue()


SHIPPED_PY = sorted(f for f in os.listdir(HERE) if f.endswith(".py")) + \
    ["tools/" + f for f in sorted(os.listdir(os.path.join(HERE, "tools"))) if f.endswith(".py")]
ENGINES = ("playwright_scraper", "puppeteer_scraper", "selenium_scraper")


# ---------------------------------------------------------------------------
# Page states
# ---------------------------------------------------------------------------

def test_page_states():
    eq("state: simple product page", P.detect_page_state(fixture("product_delaney.html"), status=200), "product")
    eq("state: guided product page", P.detect_page_state(fixture("product_york_guided.html"), status=200), "product")
    eq("state: leaf listing", P.detect_page_state(fixture("listing_sofa.html"), status=200), "listing")
    eq("state: hub", P.detect_page_state(fixture("hub_furniture.html"), status=200), "hub")
    eq("state: branded 403 with status", P.detect_page_state(fixture("blocked_403_1326.html"), status=403), "blocked")
    eq("state: branded 403 with NO status (selenium, pyppeteer)",
       P.detect_page_state(fixture("blocked_403_1326.html")), "blocked")
    eq("state: a redirect to the UK store is a refusal, whatever the body",
       P.detect_page_state(fixture("product_delaney.html"), status=200,
                           url="https://www.potterybarn.co.uk/"), "geo_redirect")
    eq("state: an EMPTY document is our plumbing, not the site (selenium, stripped proxy credential)",
       P.detect_page_state("<html><head></head><body></body></html>"), "transport_error")
    eq("state: ...but an empty body under a 403 is still the site refusing",
       P.detect_page_state("", status=403), "blocked")
    eq("state: a short page with no state is not a served page",
       P.detect_page_state("<html><body>hello</body></html>", status=200), "blocked")
    # The 2Captcha Scraper API answered 200 with a 137 KB prerendered page and
    # no state for two product URLs (2026-09-24). Big, 200, and not a product.
    eq("state: a large 200 with no state is empty, never product",
       P.detect_page_state("<html>" + "x" * 20000 + "</html>", status=200), "empty")


def test_extension_strip_is_load_bearing():
    """The Scraping Browser's OWN 403 page, as captured 2026-09-24, carries the
    auto-solve extension's hunter scripts, and they mention cf-turnstile —
    which IS in CAPTCHA_MARKERS. Without the strip the refusal reads as a
    captcha and goes to the paid solver."""
    html = fixture("blocked_403_scraping_browser.html")
    check("SB 403 fixture really carries the extension scripts", "chrome-extension://" in html)
    check("...and the injected text names cf-turnstile", "cf-turnstile" in html.lower())
    eq("with the strip it is a refusal", P.detect_page_state(html, status=403), "blocked")
    with mock.patch.object(P, "strip_extension_scripts", lambda t: t):
        eq("without the strip it would be misread as a captcha", P.detect_page_state(html), "captcha")


def test_sitekey_in_config_does_not_trigger_the_solver():
    import captcha_solver
    html = fixture("product_delaney.html")
    check("the fixture really carries the form's sitekey in its config",
          "6LcLh0EaAAAAAOQaeOFfqYnYj75R9o0CMqurqCdY" in html)
    check("...and no rendered widget is found on it", captcha_solver.detect_rendered_widget(html) is None)
    eq("...and the page classifies as a product, not a captcha",
       P.detect_page_state(html, status=200), "product")
    widget = '<div class="g-recaptcha" data-sitekey="6LcLh0EaAAAAAOQaeOFfqYnYj75R9o0CMqurqCdY"></div>'
    eq("a rendered checkbox is typed v2, not v3",
       captcha_solver.detect_rendered_widget(widget).kind, "recaptcha_v2")
    eq("an invisible widget is typed v2-invisible",
       captcha_solver.detect_rendered_widget(widget.replace("></div>", ' data-size="invisible"></div>')).kind,
       "recaptcha_v2_invisible")
    check("the solver entry point the bridge calls EXISTS",
          callable(getattr(captcha_solver, "solve_on_page", None)))

    class NoInject:
        name = "stub"
    with mock.patch.object(captcha_solver, "solve_recaptcha") as paid:
        result = captcha_solver.solve_on_page(NoInject(), widget, url="u", api_key="k")
    check("an engine that cannot inject is never charged", result is False and not paid.called)


# ---------------------------------------------------------------------------
# Product pages -> SKU rows
# ---------------------------------------------------------------------------

def test_simple_product_values():
    rows, facts = P.parse_product(fixture("product_delaney.html"),
                                  url="https://www.potterybarn.com/products/delaney-marble-end-table/")
    eq("delaney: 4 SKUs", len(rows), 4)
    eq("delaney: inline subsets", facts["subsets_source"], "inline")
    check("delaney: sku count closes", facts["sku_check"]["closes"], facts["sku_check"])
    by = {r.sku: r for r in rows}
    r = by.get("2235076")
    check("delaney: SKU 2235076 present", r is not None)
    if r:
        eq("delaney 2235076 price (selling)", r.price, 349.0)
        eq("delaney 2235076 original (regular)", r.original_price, 449.0)
        eq("delaney 2235076 discount computed", r.discount_pct, 22.27)
        eq("delaney 2235076 currency from the page", r.currency, "USD")
        eq("delaney 2235076 availability", r.availability, "ON_HAND")
        eq("delaney 2235076 options", r.options, {"Finish": "Bronze & Banswara Marble",
                                                 "Quantity": "Individual"})
        eq("delaney 2235076 title unescaped", r.title, 'Delaney 14" Marble End Table, Bronze')
        eq("delaney 2235076 product_id", r.product_id, "delaney-marble-end-table")
        eq("delaney 2235076 hierarchy", r.category_hierarchy, ["Furniture", "End & Side Tables"])
        eq("delaney 2235076 image", r.image_url,
           "https://assets.pbimgs.com/pbimgs/rk/images/dp/wcm/202629/0220/img68c.jpg")
        eq("delaney 2235076 price_source", r.price_source, "pdp-state")
    eq("delaney: the four real (selling, regular) pairs",
       sorted((r.price, r.original_price) for r in rows),
       [(349.0, 449.0), (349.0, 499.0), (698.0, 898.0), (698.0, 998.0)])
    back = [r for r in rows if r.availability == "BACK_ORDERED"]
    eq("delaney: two SKUs back-ordered, each with its date",
       sorted((r.sku, r.back_ordered_date) for r in back),
       [("4647188", "2026-11-23"), ("5641851", "2026-11-23")])


def test_guided_product_is_read_from_the_compressed_value():
    html = fixture("product_york_guided.html")
    details = P.product_details(P.initial_state(html))
    eq("york: inline subsets are EMPTY in the served page", details.get("subsets"), [])
    check("york: the compressed value is present", len(details["subsetsCompressedValue"]) > 100000)
    rows, facts = P.parse_product(html)
    eq("york: 1,367 SKUs decoded", len(rows), 1367)
    eq("york: 93 of them NLA", facts["sku_check"]["nla"], 93)
    eq("york: skuCount stated 1,274", facts["sku_check"]["sku_count_stated"], 1274)
    eq("york: displayable-or-not-NLA counts 1,274", facts["sku_check"]["counted"], 1274)
    check("york: the count closes", facts["sku_check"]["closes"])
    eq("york: source", facts["subsets_source"], "compressed")
    selling = [r.price for r in rows if r.availability != "NLA"]
    eq("york: min over non-NLA SKUs == the page's aggregate low", min(selling),
       facts["aggregate_low_selling"])
    eq("york: max over non-NLA SKUs == the page's aggregate high", max(selling),
       facts["aggregate_high_selling"])
    eq("york: every row carries the compressed price_source",
       {r.price_source for r in rows}, {"pdp-compressed"})
    eq("york: no duplicate SKU", len({r.sku for r in rows}), len(rows))
    check("york: every SKU has an availability", all(r.availability for r in rows))
    first = next(r for r in rows if r.sku == "1632")
    eq("york 1632 options",
       first.options.get("Furniture Size"), 'Sofa 81"')
    eq("york 1632 price", first.price, 2999.0)
    check("york 1632 width parsed as a number", first.width_in == 81.0, first.width_in)


def test_a_partial_decode_fails_the_count_check():
    html = fixture("product_york_guided.html")
    rows, facts = P.parse_product(html)
    details = P.product_details(P.initial_state(html))
    raw = [s for sub in P.subsets_of(details)[0] for s in sub["definitions"]["skus"].values()]
    check_result = P.sku_count_check(raw[:-10], details)
    check("10 SKUs short no longer closes", check_result["closes"] is False)
    check("the full list does close", facts["sku_check"]["closes"])


def test_sku_count_rule_counts_a_displayable_nla_sku():
    """Measured 2026-10-01 on `colton-object-white`: one SKU, NLA, still
    displayable, skuCount 1. "rows - NLA" said 0 and called a complete page
    incomplete."""
    sku = {"availability": {"displayable": True}, "inventory": {"availability": "NLA"}}
    gone = {"availability": {"displayable": False}, "inventory": {"availability": "NLA"}}
    live = {"availability": {"displayable": False}, "inventory": {"availability": "ON_HAND"}}
    eq("displayable NLA counts", P.counted_sku(sku), True)
    eq("non-displayable NLA does not", P.counted_sku(gone), False)
    eq("a live SKU counts even if not displayable", P.counted_sku(live), True)
    eq("colton's shape closes", P.sku_count_check([sku], {"skuCount": 1})["closes"], True)


def test_brotli_missing_is_loud_not_empty():
    html = fixture("product_york_guided.html")
    with mock.patch.object(P, "brotli", None):
        try:
            P.parse_product(html)
            raised = False
        except P.DecodeError:
            raised = True
    check("without brotli a guided page RAISES rather than returning 0 rows", raised)
    with mock.patch.object(P, "brotli", None):
        rows, _ = P.parse_product(fixture("product_delaney.html"))
    eq("...while a simple page still parses without it", len(rows), 4)


def test_state_scanner_is_string_aware():
    html = '<script>window.__INITIAL_STATE__={"a":"} { \\" }","b":{"c":1}};</script>'
    eq("braces and escaped quotes inside strings", P.initial_state(html), {"a": '} { " }', "b": {"c": 1}})
    eq("no state -> None", P.initial_state("<html></html>"), None)
    eq("truncated state -> None", P.initial_state('<script>window.__INITIAL_STATE__={"a":1'), None)


def test_ui_state_is_not_a_product_fact():
    state = {"product": {"isSidePanelDrawerOpen": True, "productDetails": {}}}
    eq("a productDetails with no groupId is not a product", P.product_details(state), None)


# ---------------------------------------------------------------------------
# Listing API -> Product rows
# ---------------------------------------------------------------------------

def test_listing_values():
    payload = json.loads(fixture("browse_sofa_p1.json"))
    rows = P.parse_browse_response(payload, currency="USD", category="furniture/sofa")
    eq("browse: 6 results in the trimmed fixture", len(rows), 6)
    eq("browse: total_num_results", P.browse_total(payload), 434)
    eq("browse: group", P.browse_group(payload),
       {"group_id": "sofa", "display_name": "Sofas", "count": 434, "parents": ["All", "Furniture"]})
    york = rows[0]
    eq("york row: sku is the product slug", york.sku, "york-slope-arm-deep-slipcovered-sofa-collection")
    eq("york row: not a sub-group", york.sub_group_id, None)
    eq("york row: product_id equals it", york.product_id, york.sku)
    eq("york row: leader_sku kept apart", york.leader_sku, "865462")
    check("york row: leader_sku never stands in for sku", york.leader_sku != york.sku)
    eq("york row: price is lowestPrice, in dollars", york.price, 1679.0)
    eq("york row: price_max is highestPrice", york.price_max, 4199.0)
    eq("york row: currency", york.currency, "USD")
    eq("york row: url", york.url,
       "https://www.potterybarn.com/products/york-slope-arm-deep-slipcovered-sofa-collection/")
    eq("york row: title unescaped", york.title, 'York Slope Arm Deep Seat Slipcovered Sofa (60"-108")')
    eq("york row: price_source", york.price_source, "constructor-browse")
    eq("york row: row_index", york.row_index, 0)
    check("no listing row has an original at or below its price",
          all(r.original_price is None or r.original_price > r.price for r in rows))


def test_sub_group_results():
    """15 of 144 results over three categories are a SLICE of a product."""
    import http_scraper
    payload = json.loads(fixture("browse_dining_benches_subgroups.json"))
    rows = P.parse_browse_response(payload, currency="USD")
    a, b, plain = rows
    eq("slice 1: sku is the slice's own id", a.sku, "aldon-dining-bench-SPAF-finish-russet-oak-wood-finish")
    eq("slice 1: product_id is the URL PATH's slug", a.product_id, "aldon-dining-bench")
    eq("slice 1: sub_group_id names the slice", a.sub_group_id, a.sku)
    eq("slice 2: the same product", b.product_id, "aldon-dining-bench")
    check("two slices, two distinct row keys", a.sku != b.sku)
    eq("a plain product has no sub_group_id", plain.sub_group_id, None)
    eq("a plain product: sku == product_id", plain.sku, plain.product_id)
    tmp = tempfile.mkdtemp()
    listing = os.path.join(tmp, "l.json")
    W.write_json(rows, listing)
    args = http_scraper.parse_args(["--from-listing", listing])
    eq("--from-listing fetches each product page ONCE, without ?subGroupId",
       http_scraper.targets_from_args(args),
       ["https://www.potterybarn.com/products/aldon-dining-bench/",
        "https://www.potterybarn.com/products/woodbury-fully-slipcovered-banquette/"])


def test_listing_matches_its_own_product_page():
    """Two reports of one fact: the API's range for York and York's PDP."""
    payload = json.loads(fixture("browse_sofa_p1.json"))
    york = P.parse_browse_response(payload, currency="USD")[0]
    _, facts = P.parse_product(fixture("product_york_guided.html"))
    eq("API lowestPrice == PDP aggregate lowSellingPrice", york.price, facts["aggregate_low_selling"])
    eq("API highestPrice == PDP aggregate highSellingPrice", york.price_max, facts["aggregate_high_selling"])


def test_currency_is_never_invented():
    payload = json.loads(fixture("browse_sofa_p1.json"))
    rows = P.parse_browse_response(payload, currency=None)
    check("no stated currency -> None on every row", all(r.currency is None for r in rows))
    row = P.product_from_result({"data": {"id": "x", "lowestPrice": None}}, currency="USD")
    eq("no price -> no currency either", row.currency, None)
    eq("the fallback key's currency is a MEASURED fact, recorded per key",
       P.KNOWN_KEY_CURRENCY, {P.FALLBACK_CONSTRUCTOR_KEY: "USD"})


def test_hostile_result_shapes():
    eq("a result with no data", P.product_from_result({"value": "x"}, currency="USD"), None)
    eq("a result with no id", P.product_from_result({"data": {"lowestPrice": 5}}, currency="USD"), None)
    eq("a non-dict result", P.product_from_result("x", currency="USD"), None)
    eq("an empty payload", P.parse_browse_response({}, currency="USD"), [])
    eq("a bool total is not a count", P.browse_total({"response": {"total_num_results": True}}), None)


def test_listing_context():
    eq("leaf context", P.listing_context(fixture("listing_sofa.html")),
       {"group_id": "sofa", "key": "key_w3v8XC1kGR9REv46", "currency": "USD"})
    eq("hub has no group id", P.listing_context(fixture("hub_furniture.html"))["group_id"], None)


# ---------------------------------------------------------------------------
# URLs and robots
# ---------------------------------------------------------------------------

def test_url_helpers():
    eq("slug", P.slug_from_url("https://www.potterybarn.com/products/delaney-marble-end-table/"),
       "delaney-marble-end-table")
    eq("a slug ending in digits is kept whole",
       P.slug_from_url("https://www.potterybarn.com/products/open-box-cline-swivel-counter-stool-14341192/"),
       "open-box-cline-swivel-counter-stool-14341192")
    eq("a category is not a product", P.slug_from_url("https://www.potterybarn.com/shop/furniture/sofa/"), None)
    eq("category label", P.category_from_url("https://www.potterybarn.com/shop/furniture/sofa/"),
       "furniture/sofa")
    eq("browse url", P.browse_url("sofa"), "https://ac.cnstrc.com/browse/group_id/sofa")
    ok, reason = P.supported_host("https://www.potterybarn.co.uk/products/x/")
    check("the UK store is refused WITH the reason", not ok and "separate storefront" in reason, reason)
    ok, reason = P.supported_host("https://example.com/")
    check("another host is refused", not ok and "not a www.potterybarn.com URL" in reason)


def test_robots_snapshot():
    base = "https://www.potterybarn.com"
    cases = {
        "/products/delaney-marble-end-table/": True,
        "/shop/furniture/sofa/": True,
        "/shop/furniture/sofa/?N=4294967": False,
        "/shop/a+b+c/": False,
        "/shop/apartment-sofas/": False,
        "/shop/furniture/apartment-furniture/": True,
        "/checkout/": False,
        "/products/x/quicklook": False,
        "/llms.txt": True,
    }
    for path, want in cases.items():
        eq("robots %s" % path, P.robots_allows(base + path), want)
    eq("the embedded snapshot IS robots.snapshot.txt",
       P.ROBOTS_SNAPSHOT_TEXT, open(os.path.join(HERE, "robots.snapshot.txt")).read())
    eq("an empty rule set fails CLOSED", P.robots_allows(base + "/products/x/", robots_text=""), False)


# ---------------------------------------------------------------------------
# Output contract
# ---------------------------------------------------------------------------

def test_schema_prefix_and_exit_codes():
    prefix = list(W.FAMILY_PREFIX)
    eq("Product carries the family prefix in order",
       [f.name for f in dataclasses.fields(W.Product)][:len(prefix)], prefix)
    eq("SkuRow carries the same prefix, byte-identical",
       [f.name for f in dataclasses.fields(W.SkuRow)][:len(prefix)], prefix)
    check("both carry product_id", "product_id" in {f.name for f in dataclasses.fields(W.Product)}
          and "product_id" in {f.name for f in dataclasses.fields(W.SkuRow)})
    eq("row class by mode", (W.ROW_CLASS_BY_MODE["category"], W.ROW_CLASS_BY_MODE["product"]),
       (W.Product, W.SkuRow))
    eq("exit codes", (W.EXIT_BLOCKED, W.EXIT_NO_PRODUCTS, W.EXIT_REMOTE_API_ERROR, W.EXIT_PARTIAL),
       (3, 4, 5, 6))
    removed = {"rating", "review_count", "description"}
    check("no always-null rating/review/description column",
          not removed & ({f.name for f in dataclasses.fields(W.Product)}
                         | {f.name for f in dataclasses.fields(W.SkuRow)}))


def _run(rows, **kw):
    tmp = tempfile.mkdtemp()
    out = os.path.join(tmp, "o")
    args = dict(blocked=False, stop_reason="completed", pages_requested=1,
                pages_completed=1, start_url="s", final_url="f", mode="category")
    args.update(kw)
    rc, _ = quiet(W.finish_run, rows, out, "both", False, **args)
    return rc, out


def test_finish_run_exit_codes():
    row = W.Product(sku="a", product_id="a")
    rc, out = _run([row])
    eq("complete -> 0", rc, 0)
    rc, out = _run([], blocked=True, stop_reason="blocked", pages_completed=0)
    eq("blocked, nothing -> 3", rc, 3)
    check("a blocked run writes no dataset", not os.path.exists(out + ".json"))
    check("...but always writes the attempt sidecar", os.path.exists(out + W.ATTEMPT_META_SUFFIX))
    rc, _ = _run([], stop_reason="transport_error", pages_completed=0, transport_error=True)
    eq("our plumbing failed, nothing -> 5, never 3", rc, 5)
    rc, _ = _run([], stop_reason="completed", pages_completed=0)
    eq("nothing found -> 4", rc, 4)
    rc, _ = _run([row], stop_reason="blocked", pages_requested=3)
    eq("some rows then stopped -> 6", rc, 6)
    rc, out = _run([W.SkuRow(sku="1", product_id="p", options={"A": "b | c"})], mode="product")
    meta = json.load(open(out + ".meta.json"))
    eq("sidecar records the mode", meta["mode"], "product")
    eq("sidecar records the row class", meta["row_class"], "SkuRow")
    header = open(out + ".csv").readline().strip().split(",")
    eq("SKU CSV header is SkuRow's fields", header, [f.name for f in dataclasses.fields(W.SkuRow)])
    csv_text = open(out + ".csv").read()
    check("a dict cell is JSON, so ' | ' inside a value cannot corrupt it",
          '"{""A"": ""b | c""}"' in csv_text, csv_text[-80:])


def test_empty_result_does_not_overwrite():
    tmp = tempfile.mkdtemp()
    out = os.path.join(tmp, "o")
    quiet(W.save, [W.Product(sku="a")], out, "json")
    before = open(out + ".json").read()
    rc, _ = quiet(W.save, [], out, "json")
    eq("zero rows -> 4", rc, 4)
    eq("...and yesterday's file is untouched", open(out + ".json").read(), before)
    quiet(W.save, [], out, "csv", allow_empty=True)
    eq("an empty CSV still has its header", open(out + ".csv").read().splitlines()[0].split(",")[0],
       "source")


def test_dedupe():
    seen = set()
    first = W.dedupe_by_sku([W.Product(sku="a"), W.Product(sku="b")], seen)
    second = W.dedupe_by_sku([W.Product(sku="b"), W.Product(sku=None), W.Product(sku="c")], seen)
    eq("page 2 keeps only new ids and the keyless row", [r.sku for r in second], [None, "c"])
    eq("page 1 intact", [r.sku for r in first], ["a", "b"])


def test_diff_runs_fails_closed_without_sidecars():
    """Audit, 2026-10-01: a Product file and a SkuRow file with no .meta.json
    were compared, '12 added, 24 removed', exit 0."""
    import diff_runs
    tmp = tempfile.mkdtemp()
    a, b = os.path.join(tmp, "a.json"), os.path.join(tmp, "b.json")
    json.dump(json.load(open(os.path.join(HERE, "sample_output.json"))), open(a, "w"))
    json.dump(json.load(open(os.path.join(HERE, "sample_skus.json"))), open(b, "w"))
    with mock.patch.object(sys, "argv", ["diff_runs.py", "--old", a, "--new", b]):
        rc, text = quiet(diff_runs.main)
    eq("no sidecars -> refused, exit 2", rc, 2)
    check("...and says why", "no readable .meta.json" in text)
    meta = {"status": "complete", "mode": "category", "source": "potterybarn.com",
            "schema_version": "x", "scope": {"group_id": "sofa", "currency": "USD"}}
    json.dump(meta, open(os.path.join(tmp, "a.meta.json"), "w"))
    json.dump(dict(meta, scope={"group_id": "dining-benches", "currency": "USD"}),
              open(os.path.join(tmp, "b.meta.json"), "w"))
    ok, text = quiet(diff_runs._check_comparable, argparse.Namespace(old=a, new=b))
    check("two different listing groups are refused", ok is False and "group_id" in text)
    json.dump(dict(meta, schema_version="y"), open(os.path.join(tmp, "b.meta.json"), "w"))
    ok, text = quiet(diff_runs._check_comparable, argparse.Namespace(old=a, new=b))
    check("two different row schemas are refused", ok is False and "row schemas" in text)


def test_diff_runs_refuses_mixed_modes():
    import diff_runs
    tmp = tempfile.mkdtemp()
    paths = []
    for name, mode in (("old", "category"), ("new", "product")):
        path = os.path.join(tmp, name + ".json")
        json.dump([], open(path, "w"))
        json.dump({"status": "complete", "mode": mode}, open(os.path.join(tmp, name + ".meta.json"), "w"))
        paths.append(path)
    ok, _ = quiet(diff_runs._check_comparable, argparse.Namespace(old=paths[0], new=paths[1]))
    check("a listing run and a SKU run are not compared", ok is False)
    result = diff_runs.diff_rows(
        [{"sku": "1", "availability": "ON_HAND", "price": 5}],
        [{"sku": "1", "availability": "BACK_ORDERED", "price": 5}],
        diff_runs.TRACKED_FIELDS_BY_MODE["product"])
    eq("a SKU going on back-order is a change", len(result["changed"]), 1)


# ---------------------------------------------------------------------------
# Listing API client
# ---------------------------------------------------------------------------

class _Resp:
    def __init__(self, status, payload, headers=None):
        self.status_code = status
        self._payload = payload
        self.headers = headers or {}

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


def _browse_payload(ids, total):
    return {"response": {"total_num_results": total, "groups": [],
                         "results": [{"data": {"id": i, "lowestPrice": 10, "highestPrice": 20}}
                                     for i in ids]}}


class _FakeSession:
    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    def get(self, url, params=None, **kw):
        self.calls.append(params["page"])
        return self.pages[params["page"] - 1] if params["page"] <= len(self.pages) \
            else _Resp(200, _browse_payload([], 3))


def _api_args(**kw):
    import api_scraper
    args = api_scraper.parse_args(["--group-id", "g", "--delay", "0", "--retry-delay", "0"])
    for k, v in kw.items():
        setattr(args, k, v)
    return args


def test_api_classifier_and_rate_limit():
    import api_scraper
    eq("429 -> rate_limited", api_scraper.classify_api(429, None, None), "rate_limited")
    eq("503 -> our problem, not a block", api_scraper.classify_api(503, None, None), "transport_error")
    eq("no status -> transport_error", api_scraper.classify_api(None, None, "boom"), "transport_error")
    eq("200 without a response -> empty", api_scraper.classify_api(200, {"x": 1}, None), "empty")
    eq("200 with a response -> api", api_scraper.classify_api(200, {"response": {}}, None), "api")
    eq("plenty left -> no pause", api_scraper.rate_limit_pause({"x-ratelimit-remaining": "150"}), 0.0)
    eq("low -> a slower pace", api_scraper.rate_limit_pause(
        {"x-ratelimit-remaining": "3", "x-ratelimit-reset": "1030"}, now=1000.0),
       api_scraper.RATE_LIMIT_LOW_PACE)
    eq("zero is NOT a wait-for-reset: the API kept answering 200 past zero",
       api_scraper.rate_limit_pause(
           {"x-ratelimit-remaining": "0", "x-ratelimit-reset": "99999"}, now=1000.0),
       api_scraper.RATE_LIMIT_LOW_PACE)


def test_api_pagination_terminates_on_data():
    import api_scraper
    pages = [_Resp(200, _browse_payload(["a", "b"], 3)), _Resp(200, _browse_payload(["c"], 3)),
             _Resp(200, _browse_payload(["c"], 3))]
    fake = _FakeSession(pages)
    with mock.patch.object(api_scraper.requests, "Session", lambda: fake):
        (rows, facts), _ = quiet(api_scraper.scrape, _api_args(all_pages=True))
    eq("three distinct products", [r.sku for r in rows], ["a", "b", "c"])
    eq("stopped on the page that added nothing new", fake.calls, [1, 2, 3])
    check("arithmetic checked after the run", facts.get("arithmetic_closes") is True, facts)
    # A total that says "more" must NOT keep the run going past an empty page.
    fake = _FakeSession([_Resp(200, _browse_payload(["a"], 999))])
    with mock.patch.object(api_scraper.requests, "Session", lambda: fake):
        (rows, facts), _ = quiet(api_scraper.scrape, _api_args(all_pages=True))
    eq("total_num_results never extends a sweep past an empty page: one sweep "
       "plus one recovery sweep, two calls each", fake.calls, [1, 2, 1, 2])
    check("...and the mismatch is recorded, not hidden", facts.get("arithmetic_closes") is False)
    eq("...and the run is not called complete",
       api_scraper._stop_reason(_api_args(all_pages=True), facts), "listing_incomplete")


class _SortingSession:
    """Relevance order repeats 'b' and never returns 'd'; price order is whole."""

    def __init__(self):
        self.calls = []

    def get(self, url, params=None, **kw):
        self.calls.append((params.get("sort_by"), params["page"]))
        if params.get("sort_by") == "lowestPrice":
            pages = [["d", "c"], ["b", "a"]]
        else:
            pages = [["a", "b"], ["b", "c"]]
        ids = pages[params["page"] - 1] if params["page"] <= len(pages) else []
        return _Resp(200, _browse_payload(ids, 4))


def test_api_recovers_what_an_unstable_order_skipped():
    """Measured 2026-09-24: one sweep of `sofa` returned 434 results but only
    415 distinct products, so 19 were never returned."""
    import api_scraper
    fake = _SortingSession()
    with mock.patch.object(api_scraper.requests, "Session", lambda: fake):
        (rows, facts), _ = quiet(api_scraper.scrape, _api_args(all_pages=True))
    eq("the second, price-sorted sweep recovered the missing product",
       sorted(r.sku for r in rows), ["a", "b", "c", "d"])
    eq("recorded in the sidecar", facts["second_sweep_recovered"], 1)
    check("the recovery sweep ran past pages holding only known products",
          ("lowestPrice", 2) in fake.calls)
    eq("the arithmetic now closes", facts["arithmetic_closes"], True)

    class StillShort(_SortingSession):
        def get(self, url, params=None, **kw):
            self.calls.append((params.get("sort_by"), params["page"]))
            ids = [["a", "b"], ["b", "c"]][params["page"] - 1] if params["page"] <= 2 else []
            return _Resp(200, _browse_payload(ids, 4))
    with mock.patch.object(api_scraper.requests, "Session", lambda: StillShort()):
        (rows, facts), _ = quiet(api_scraper.scrape, _api_args(all_pages=True))
    eq("a listing KNOWN to be short is not complete",
       api_scraper._stop_reason(_api_args(all_pages=True), facts), "listing_incomplete")


def test_a_capped_listing_is_never_complete():
    """Audit, 2026-10-01: --all-pages --max-products 10 wrote 10 of 433 as
    `complete`, and still ran the recovery sweep."""
    import api_scraper
    fake = _FakeSession([_Resp(200, _browse_payload(["a", "b", "c"], 433))])
    with mock.patch.object(api_scraper.requests, "Session", lambda: fake):
        (rows, facts), _ = quiet(api_scraper.scrape, _api_args(all_pages=True, max_products=2))
    eq("capped at 2", [r.sku for r in rows], ["a", "b"])
    eq("no recovery sweep after a deliberate cap", fake.calls, [1])
    eq("stop reason", api_scraper._stop_reason(_api_args(all_pages=True, max_products=2), facts),
       "max_products_reached")
    check("...which is not a complete stop reason",
          "max_products_reached" not in W.COMPLETE_STOP_REASONS)
    tmp = tempfile.mkdtemp()
    with mock.patch.object(api_scraper.requests, "Session",
                           lambda: _FakeSession([_Resp(200, _browse_payload(["a", "b", "c"], 433))])):
        rc, _ = quiet(api_scraper.main, ["--group-id", "g", "--all-pages", "--max-products", "2",
                                         "--delay", "0", "--out", tmp + "/o"])
    meta = json.load(open(tmp + "/o.meta.json"))
    eq("a capped run is partial, exit 6", (rc, meta["status"]), (6, "partial"))
    eq("...and its scope says it is not the full catalog",
       (meta["scope"]["max_products"], meta["scope"]["is_full_catalog"]), (2, False))


def test_a_sku_count_mismatch_is_not_complete():
    import http_scraper
    html = fixture("product_delaney.html").replace('"skuCount":4', '"skuCount":5')
    check("fixture edited", '"skuCount":5' in html)

    class S:
        def get(self, url, **kw):
            return http_scraper._Response(200, html, url)
    tmp = tempfile.mkdtemp()
    url = "https://www.potterybarn.com/products/delaney-marble-end-table/"
    with mock.patch.object(http_scraper, "build_session", lambda *a, **k: S()):
        rc, _ = quiet(http_scraper.main, ["--url", url, "--out", tmp + "/a", "--format", "json"])
        meta = json.load(open(tmp + "/a.meta.json"))
        eq("4 read, 5 stated -> partial, exit 6", (rc, meta["status"], meta["stop_reason"]),
           (6, "partial", "sku_count_mismatch"))
        eq("...the rows are still kept", meta["products"], 4)
        rc, _ = quiet(http_scraper.main, ["--url", url, "--out", tmp + "/b", "--format", "json",
                                          "--accept-count-mismatch"])
    eq("--accept-count-mismatch -> complete", rc, 0)


def test_a_partial_listing_is_not_compared_with_the_total():
    """Canary, 2026-09-26: --pages 3, one product repeated on page 2, 71 rows,
    and the first version warned 'collected 71 products but the API reports
    434' about a listing nobody had asked for in full."""
    import api_scraper
    pages = [_Resp(200, _browse_payload(["a", "b"], 434)), _Resp(200, _browse_payload(["b", "c"], 434))]
    fake = _FakeSession(pages)
    with mock.patch.object(api_scraper.requests, "Session", lambda: fake):
        (rows, facts), text = quiet(api_scraper.scrape, _api_args(pages=2))
    eq("three distinct products kept", [r.sku for r in rows], ["a", "b", "c"])
    eq("four results seen, one of them a repeat", (facts["results_seen"], facts["repeated_results"]), (4, 1))
    check("no arithmetic against the total on a partial listing", "arithmetic_closes" not in facts)
    check("no claim that products are missing from the category", "API reports" not in text)
    check("the repeat is reported", "repeated a product" in text)
    eq("still a complete run of what was asked", api_scraper._stop_reason(_api_args(pages=2), facts), "completed")
    eq("the listing was not read to its end", facts["listing_exhausted"], False)


def test_api_429_is_a_refusal_not_an_empty_listing():
    import api_scraper
    fake = _FakeSession([_Resp(429, None)] * 5)
    with mock.patch.object(api_scraper.requests, "Session", lambda: fake), \
            mock.patch.object(api_scraper.time, "sleep", lambda s: None):
        (rows, facts), _ = quiet(api_scraper.scrape, _api_args())
    check("429 marks the run blocked", facts["blocked"] and not facts["transport_error"])
    eq("retried, bounded", len(fake.calls), 3)
    eq("stop reason", api_scraper._stop_reason(_api_args(), facts), "blocked")


def test_api_key_never_printed():
    import api_scraper
    eq("the key is redacted from free text",
       api_scraper._redact("GET https://ac.cnstrc.com/x?key=key_abc123&page=1", "key_abc123"),
       "GET https://ac.cnstrc.com/x?key=key_***&page=1")


def test_url_guess_is_labelled():
    import api_scraper
    args = _api_args(group_id=None, url="https://www.potterybarn.com/shop/furniture/sofa/")
    args.proxy = None
    args.proxy_file = None
    (ctx), text = quiet(api_scraper.resolve_listing, args, lambda m: None)
    eq("guessed group id", ctx["group_id"], "sofa")
    eq("...and labelled as a guess", ctx["group_id_source"], "url-guess")
    check("...and said out loud", "guessing" in text)
    eq("fallback key labelled", ctx["key_source"], "fallback")
    eq("its currency comes from the measured key table", ctx["currency_source"], "known-key")


# ---------------------------------------------------------------------------
# The HTTP transport
# ---------------------------------------------------------------------------

def _completed(stdout, rc=0, stderr=""):
    return subprocess.CompletedProcess(args=[], returncode=rc, stdout=stdout, stderr=stderr)


def test_curl_keeps_the_proxy_off_argv():
    import http_scraper
    seen = {}

    def fake_run(command, input=None, **kw):
        seen["argv"], seen["stdin"] = command, input
        return _completed("<html></html>\n200 https://www.potterybarn.com/ ")
    with mock.patch.object(http_scraper.subprocess, "run", fake_run):
        http_scraper.CurlSession("http://user1234:pass5678@gw.example:1").get(
            "https://www.potterybarn.com/")
    check("no password in argv", not any("pass5678" in a for a in seen["argv"]))
    check("the proxy went in on stdin", "pass5678" in seen["stdin"])
    check("--config - is used", "--config" in seen["argv"] and "-" in seen["argv"])
    check("curl does NOT follow redirects itself", "--location" not in seen["argv"])
    check("the measured header set is sent", any(a.startswith("sec-fetch-mode: navigate")
                                                 for a in seen["argv"]))


def test_curl_never_follows_a_redirect_off_the_store():
    import http_scraper
    calls = []

    def fake_run(command, input=None, **kw):
        calls.append(command[-1])
        return _completed("\n302 %s https://www.potterybarn.co.uk/" % command[-1])
    with mock.patch.object(http_scraper.subprocess, "run", fake_run):
        response = http_scraper.CurlSession(None).get("https://www.potterybarn.com/robots.txt")
    eq("one request, the UK store never fetched", len(calls), 1)
    eq("the redirect target is reported", response.url, "https://www.potterybarn.co.uk/")
    eq("...and classifies as a refusal", P.detect_page_state(response.text, status=response.status_code,
                                                             url=response.url), "geo_redirect")
    calls.clear()
    replies = iter(["\n301 https://www.potterybarn.com/shop/furniture/sofas/ https://www.potterybarn.com/shop/furniture/",
                    "<html></html>\n200 https://www.potterybarn.com/shop/furniture/ "])
    with mock.patch.object(http_scraper.subprocess, "run", lambda c, input=None, **k: (calls.append(c[-1]), _completed(next(replies)))[1]):
        response = http_scraper.CurlSession(None).get("https://www.potterybarn.com/shop/furniture/sofas/")
    eq("an on-store redirect IS followed, and the landing URL kept",
       (len(calls), response.url), (2, "https://www.potterybarn.com/shop/furniture/"))


def test_a_proxy_407_is_not_a_site_block():
    import http_scraper
    with mock.patch.object(http_scraper.subprocess, "run",
                           lambda c, input=None, **k: _completed("", rc=56, stderr="CONNECT tunnel failed, response 407")):
        try:
            http_scraper.CurlSession("http://a:b@h:1").get("https://www.potterybarn.com/")
            kind = None
        except http_scraper.ProxyAuthError:
            kind = "proxy"
    eq("407 raises ProxyAuthError", kind, "proxy")
    with mock.patch.object(http_scraper, "scrape", side_effect=http_scraper.ProxyAuthError("407")):
        rc, _ = quiet(http_scraper.main, ["--url", "https://www.potterybarn.com/products/x/"])
    eq("...and the run exits 5, not 3", rc, 5)


def test_fetch_rotates_on_a_refusal_not_on_a_timeout():
    import http_scraper

    class S:
        def __init__(self, answers):
            self.answers = answers

        def get(self, url, **kw):
            answer = self.answers.pop(0)
            if isinstance(answer, Exception):
                raise answer
            return answer
    refused = http_scraper._Response(403, fixture("blocked_403_1326.html"), "https://www.potterybarn.com/p")
    good = http_scraper._Response(200, fixture("product_delaney.html"), "https://www.potterybarn.com/p")
    rotations = []
    second = S([good])
    result, used = http_scraper.fetch(S([refused]), "u", 5, retries=2, retry_delay=0,
                                      rotate=lambda: (rotations.append(1), (second, "exit2"))[1])
    eq("a 403 rotated once and then succeeded", (len(rotations), result.state, used is second),
       (1, "product", True))
    rotations.clear()
    result, _ = http_scraper.fetch(S([RuntimeError("timed out"), good]), "u", 5, retries=2,
                                   retry_delay=0, rotate=lambda: rotations.append(1))
    eq("a timeout retried the SAME exit", (len(rotations), result.state), (0, "product"))


def test_default_client_is_the_measured_one():
    import http_scraper
    with mock.patch.object(http_scraper, "CURL_AVAILABLE", True):
        eq("curl where present", http_scraper.default_http_client(), "curl")
    with mock.patch.object(http_scraper, "CURL_AVAILABLE", False):
        eq("requests only when curl is absent", http_scraper.default_http_client(), "requests")


def test_bad_usage_exits_2_not_1():
    """`raise SystemExit("refusing ...")` exits 1, the CRASH code (measured
    2026-10-01 on a potterybarn.co.uk URL). Every refusal of a URL is 2."""
    import api_scraper
    import browser_bridge
    import http_scraper
    cases = [
        ("http_scraper, UK store", lambda: http_scraper.main(["--url", "https://www.potterybarn.co.uk/products/x/"])),
        ("http_scraper, a category", lambda: http_scraper.main(["--url", "https://www.potterybarn.com/shop/furniture/sofa/"])),
        ("api_scraper, a product URL", lambda: api_scraper.main(["--url", "https://www.potterybarn.com/products/x/"])),
        ("api_scraper, nothing to fetch", lambda: api_scraper.main([])),
        ("bridge, UK store", lambda: browser_bridge.run(
            _bridge_args(["--url", "https://www.potterybarn.co.uk/products/x/"]), StubDriver([]))),
        ("bridge, a department hub", lambda: browser_bridge.run(
            _bridge_args(["--mode", "category", "--url", "https://www.potterybarn.com/shop/furniture/"]),
            StubDriver([]))),
    ]
    for name, call in cases:
        try:
            _, _ = quiet(call)
            code = 0
        except SystemExit as exc:
            code = exc.code
        eq("%s -> exit 2" % name, code, 2)


def test_a_department_url_is_never_guessed_into_a_broad_group():
    """--no-page on /shop/furniture/ guessed `furniture`: 8,278 results headed
    by a curtain rod, reported as a complete listing (2026-10-01)."""
    import api_scraper
    eq("department path", P.is_department_path("https://www.potterybarn.com/shop/furniture/"), True)
    eq("leaf path", P.is_department_path("https://www.potterybarn.com/shop/furniture/sofa/"), False)
    rc, _ = quiet(api_scraper.main, ["--url", "https://www.potterybarn.com/shop/furniture/", "--no-page"])
    eq("api_scraper on a department URL with no page read -> exit 4 (hub)", rc, 4)


def test_http_scraper_refuses_what_it_should():
    import http_scraper
    eq("a category URL is refused, pointing at api_scraper",
       "api_scraper.py" in (http_scraper.validate_target("https://www.potterybarn.com/shop/furniture/sofa/") or ""), True)
    # `Disallow: */apartment-` reaches product slugs too.
    check("a robots-disallowed product URL is refused",
          "robots" in (http_scraper.validate_target("https://www.potterybarn.com/products/apartment-sofa/") or "x"))
    eq("a product page is accepted",
       http_scraper.validate_target("https://www.potterybarn.com/products/delaney-marble-end-table/"), None)


def test_http_scraper_end_to_end_with_a_decode_failure():
    """A guided page that cannot be decoded is a failed page, not 0 rows of success."""
    import http_scraper
    york = http_scraper._Response(200, fixture("product_york_guided.html"),
                                  "https://www.potterybarn.com/products/york/")
    delaney = http_scraper._Response(200, fixture("product_delaney.html"),
                                     "https://www.potterybarn.com/products/delaney/")

    class S:
        answers = [york, delaney]

        def get(self, url, **kw):
            return self.answers.pop(0)
    tmp = tempfile.mkdtemp()
    with mock.patch.object(http_scraper, "build_session", lambda *a, **k: S()), \
            mock.patch.object(P, "brotli", None):
        rc, _ = quiet(http_scraper.main, [
            "--url", "https://www.potterybarn.com/products/york/",
            "--url", "https://www.potterybarn.com/products/delaney/",
            "--delay", "0", "--out", os.path.join(tmp, "o"), "--format", "json"])
    meta = json.load(open(os.path.join(tmp, "o.meta.json")))
    eq("partial, exit 6", (rc, meta["status"], meta["pages_failed"]), (6, "partial", [1]))
    eq("the simple page's 4 SKUs were kept", meta["products"], 4)


# ---------------------------------------------------------------------------
# Browser bridge, with a stub driver
# ---------------------------------------------------------------------------

class StubDriver:
    name = "stub"

    def __init__(self, documents, statuses=None, fail=None):
        self.documents = list(documents)
        self.statuses = list(statuses or [200] * len(documents))
        self.current = ""
        self.fail = fail
        self.over_cdp = True
        self.started = self.stopped = 0

    def start(self):
        self.started += 1

    def navigate(self, url):
        if self.fail:
            raise self.fail
        self.current = self.documents.pop(0)
        return self.statuses.pop(0)

    def content(self):
        return self.current

    def sleep(self, ms):
        pass

    def stop(self):
        self.stopped += 1


def _bridge_args(argv):
    import browser_bridge
    return browser_bridge.parse_args(browser_bridge.build_parser("t"), argv)


def test_bridge_product_and_refusal():
    import browser_bridge
    tmp = tempfile.mkdtemp()
    url = "https://www.potterybarn.com/products/delaney-marble-end-table/"
    driver = StubDriver([fixture("product_delaney.html")])
    rc, _ = quiet(browser_bridge.run, _bridge_args(["--url", url, "--out", tmp + "/a"]), driver)
    eq("a product page -> exit 0, 4 SKUs", (rc, json.load(open(tmp + "/a.meta.json"))["products"]), (0, 4))
    eq("started once, stopped once", (driver.started, driver.stopped), (1, 1))
    driver = StubDriver([fixture("blocked_403_scraping_browser.html")], [403])
    rc, _ = quiet(browser_bridge.run, _bridge_args(["--url", url, "--out", tmp + "/b"]), driver)
    eq("the branded 403 over CDP -> exit 3", rc, 3)
    driver = StubDriver([], fail=TimeoutError("Page.goto: Timeout 45000ms exceeded"))
    rc, _ = quiet(browser_bridge.run, _bridge_args(["--url", url, "--out", tmp + "/c"]), driver)
    eq("a navigation timeout -> exit 5, not 3", rc, 5)
    driver = StubDriver(["<html><head></head><body></body></html>"], [None])
    rc, _ = quiet(browser_bridge.run, _bridge_args(["--url", url, "--out", tmp + "/d"]), driver)
    eq("an empty document (the proxy refused) -> exit 5, not 3", rc, 5)
    eq("...and the connection is still closed", driver.stopped, 1)


class HydratedDriver(StubDriver):
    """What the Scraping Browser really showed on 2026-10-01: a served body
    that IS the product page, and a DOM from which the state <script> is gone."""

    def __init__(self, body, live_state=None, with_body=True):
        super().__init__(["<html><head><title>Delaney</title></head><body><div id=app>"
                          + "x" * 9000 + "</div></body></html>"])
        self._body, self._live, self._with_body = body, live_state, with_body

    def __getattr__(self, name):
        if name == "response_body" and self._with_body:
            return lambda: self._body
        if name == "state_json" and self._live is not None:
            return lambda: self._live
        raise AttributeError(name)


def test_bridge_reads_a_hydrated_product_page():
    import browser_bridge
    url = "https://www.potterybarn.com/products/delaney-marble-end-table/"
    body = fixture("product_delaney.html")
    eq("the hydrated DOM alone reads as empty", P.detect_page_state(HydratedDriver(body).documents[0]), "empty")
    tmp = tempfile.mkdtemp()
    rc, _ = quiet(browser_bridge.run, _bridge_args(["--url", url, "--out", tmp + "/a"]), HydratedDriver(body))
    meta = json.load(open(tmp + "/a.meta.json"))
    eq("the served body is read: exit 0, 4 SKUs", (rc, meta["products"]), (0, 4))
    eq("...and the sidecar says where the state came from",
       meta["transport_facts"]["state_source"], "response-body")
    live = json.dumps(P.initial_state(body))
    rc, _ = quiet(browser_bridge.run, _bridge_args(["--url", url, "--out", tmp + "/b"]),
                  HydratedDriver(body, live_state=live, with_body=False))
    meta = json.load(open(tmp + "/b.meta.json"))
    eq("no response object (selenium): the live JS state is read",
       (rc, meta["products"], meta["transport_facts"]["state_source"]), (0, 4, "window-state"))


def test_bridge_category_reads_the_api_through_the_browser():
    import browser_bridge
    payload = json.loads(fixture("browse_sofa_p1.json"))
    doc = "<html><head></head><body><pre>%s</pre></body></html>" % (
        json.dumps(payload).replace("&", "&amp;").replace("<", "&lt;"))
    empty = "<html><body><pre>%s</pre></body></html>" % json.dumps(_browse_payload([], 434))
    tmp = tempfile.mkdtemp()
    driver = StubDriver([doc, empty])
    rc, _ = quiet(browser_bridge.run, _bridge_args(
        ["--mode", "category", "--group-id", "sofa", "--pages", "3", "--delay", "0",
         "--out", tmp + "/a"]), driver)
    rows = json.load(open(tmp + "/a.json"))
    eq("the browser path yields the API's rows", (rc, len(rows), rows[0]["price"]), (0, 6, 1679.0))
    eq("json from a <pre> document", browser_bridge.json_from_document(doc)["response"]["total_num_results"], 434)


# ---------------------------------------------------------------------------
# Credentials and secrets
# ---------------------------------------------------------------------------

def test_masking_is_global():
    import proxy_pool
    text = "a ws://u1:p1@h:1 b http://u2:p2@h:2 c"
    masked = proxy_pool.mask_text(text)
    check("every credential masked", "p1" not in masked and "p2" not in masked, masked)
    check("hosts kept", "@h:1" in masked and "@h:2" in masked)


def test_rucaptcha_gateway_mints_sessions():
    import proxy_pool
    url = "http://login-zone-custom-region-us:secret@eu.proxy.rucaptcha.com:2334"
    check("the rucaptcha host is a 2Captcha gateway", proxy_pool.is_2captcha_gateway(url))
    minted = proxy_pool.mint_sessions(url, 3)
    eq("three distinct sessions", len({m for m in minted}), 3)
    check("the region is kept", all("-region-us" in m for m in minted))


def test_env_keys_and_example():
    import env_config
    eq("ENV_KEYS", set(env_config.ENV_KEYS),
       {"TWOCAPTCHA_KEY", "POTTERYBARN_PROXY", "POTTERYBARN_CDP_ENDPOINT", "POTTERYBARN_URL"})
    documented = set(re.findall(r"^([A-Z][A-Z0-9_]+)=", open(os.path.join(HERE, ".env.example")).read(), re.M))
    eq(".env.example documents exactly what is read", documented, set(env_config.ENV_KEYS))
    check(".env.example holds no value", all(v == "" for v in re.findall(
        r"^[A-Z][A-Z0-9_]+=(.*)$", open(os.path.join(HERE, ".env.example")).read(), re.M)))


def test_gitignore_covers_every_env_variant():
    lines = open(os.path.join(HERE, ".gitignore")).read().splitlines()
    for needed in (".env", ".env.*", "!.env.example", "*.bak", "proxylist*.txt"):
        check(".gitignore has %s" % needed, needed in lines)


def test_the_scanner_catches_the_file_that_actually_leaked():
    sys.path.insert(0, os.path.join(HERE, "tools"))
    import scan_secrets as S
    leaked = ("TWOCAPTCHA_KEY=" + "ab" * 16 + "\n"
              "POTTERYBARN_PROXY=http://abcdefgh1-zone-custom-region-us:Zq8xLm2Pq7@eu.proxy.rucaptcha.com:2334\n")
    check("a .env backup is refused by NAME", S.scan_text(".env.user-endpoint.bak", ""))
    check("a *.bak is refused by suffix", S.scan_text("notes.bak", ""))
    check("the contents are refused on their own", len(S.scan_text("anything.txt", leaked)) >= 2)
    check(".env.example is allowed", not S.scan_text(".env.example", open(os.path.join(HERE, ".env.example")).read()))
    check("a documented template is not a finding",
          not S.scan_text("README.md", "ws://{login}-zone-scraping_browser-country-us:{password}@cb.2captcha.com:9222"))


def test_scanner_range_splits_and_fails_closed():
    sys.path.insert(0, os.path.join(HERE, "tools"))
    import scan_secrets as S
    seen = {}

    def fake_git(*args):
        seen.setdefault("calls", []).append(args)
        return ""
    with mock.patch.object(S, "_git", fake_git):
        quiet(S.scan_range, "abc123 --not --remotes")
    eq("the pre-push range reaches rev-list as separate arguments",
       seen["calls"][0], ("rev-list", "abc123", "--not", "--remotes"))
    with mock.patch.object(S.subprocess, "run", lambda *a, **k: _completed("", rc=128, stderr="fatal")):
        rc, _ = quiet(S.main, ["--range", "nope"])
    eq("a git failure is a FAILED scan, never 'clean'", rc, 1)


def test_hooks_are_versioned_and_wired():
    for hook, scope in (("pre-commit", "--staged"), ("pre-push", "--range")):
        text = open(os.path.join(HERE, ".githooks", hook)).read()
        check("%s delegates to scan_secrets.py %s" % (hook, scope),
              "tools/scan_secrets.py" in text and scope in text)
    check("install_hooks uses core.hooksPath",
          "core.hooksPath .githooks" in open(os.path.join(HERE, "tools", "install_hooks.sh")).read())
    workflow = open(os.path.join(HERE, ".github", "workflows", "tests.yml")).read()
    check("CI runs the SAME scanner, not a second implementation",
          "tools/scan_secrets.py" in workflow)


def test_no_json_version_preflight():
    """`GET /json/version` takes the profile lock (family CLAUDE.md §17)."""
    offenders = []
    for name in SHIPPED_PY:
        if name == "smoke_test.py":
            continue
        tree = ast.parse(open(os.path.join(HERE, name)).read())
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and any(
                    isinstance(a, ast.Constant) and a.value == "/json/version" for a in node.args):
                offenders.append(name)
    eq("no code requests /json/version", offenders, [])


def test_fixtures_carry_no_session_material():
    # Token SHAPES, not bare words: "csrf" alone matched four letters that
    # occur by chance inside the guided fixture's 117 KB base64 SKU blob.
    patterns = [r"_abck=", r"bm_sz=", r"akamai-grn", r'"sessionId"', r"csrf[_-]?token",
                r"[0-9a-f]{8}-[0-9a-f]{4}-[1-9a-f][0-9a-f]{3}-[0-9a-f]{4}-[0-9a-f]{12}"]
    for name in os.listdir(FIXTURES):
        text = open(os.path.join(FIXTURES, name), encoding="utf-8", errors="replace").read()
        hits = [p for p in patterns if re.search(p, text, re.I)]
        eq("fixture %s carries no session material" % name, hits, [])


# ---------------------------------------------------------------------------
# Engines
# ---------------------------------------------------------------------------

def test_navigation_does_not_wait_for_the_page_to_render():
    """A navigation timeout can hold the Scraping Browser profile for 28+
    minutes; `domcontentloaded` timed out in 3 of 20 instrumented runs and
    `commit` in 0 of 10 (2026-10-01)."""
    text = open(os.path.join(HERE, "playwright_scraper.py")).read()
    check("playwright navigates on commit", 'wait_until="commit"' in text)
    check("...and never on load or domcontentloaded",
          'wait_until="load"' not in text and 'wait_until="domcontentloaded"' not in text)
    check("selenium returns at DOMContentLoaded, not load",
          'page_load_strategy = "eager"' in open(os.path.join(HERE, "selenium_scraper.py")).read())


def test_every_console_script_resolves():
    """The browser script on a base install died with ModuleNotFoundError on
    --help (audit, 2026-10-01). Every script must point at a function that
    exists, and the browser one must exit 2 with a hint when Playwright is
    absent."""
    import importlib
    import browser_bridge
    text = open(os.path.join(HERE, "pyproject.toml")).read()
    scripts = re.findall(r'^([a-z-]+) = "([a-z_]+):([a-z_]+)"$', text, re.M)
    check("four console scripts declared", len(scripts) == 4, scripts)
    for name, module, func in scripts:
        if module in ENGINES:
            check("%s does not import an engine directly" % name, False, module)
            continue
        check("%s -> %s:%s exists" % (name, module, func),
              callable(getattr(importlib.import_module(module), func, None)))
    real_import = __import__

    def no_playwright(name, *a, **k):
        if name == "playwright_scraper":
            raise ImportError("No module named 'playwright'")
        return real_import(name, *a, **k)
    with mock.patch("builtins.__import__", no_playwright):
        rc, text = quiet(browser_bridge.playwright_main, ["--help"])
    eq("potterybarn-browser without Playwright -> exit 2", rc, 2)
    check("...and says what to install", "potterybarn-scraper[playwright]" in text)


def test_engines_import_their_driver_at_module_level():
    drivers = {"playwright_scraper": "playwright", "puppeteer_scraper": "pyppeteer",
               "selenium_scraper": "selenium"}
    for module, lib in drivers.items():
        tree = ast.parse(open(os.path.join(HERE, module + ".py")).read())
        top = [n for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))]
        names = [(n.module or "") if isinstance(n, ast.ImportFrom) else n.names[0].name for n in top]
        check("%s imports %s at module level" % (module, lib), any(x.split(".")[0] == lib for x in names))


def test_no_javascript_crosses_the_bridge():
    text = open(os.path.join(HERE, "browser_bridge.py")).read()
    code = "\n".join(line for line in text.splitlines() if not line.strip().startswith("#"))
    check("the bridge evaluates no JavaScript", "evaluate(" not in code and "execute_script" not in code)
    for module in ENGINES:
        check("%s implements inject_token" % module,
              "def inject_token" in open(os.path.join(HERE, module + ".py")).read())


def test_engines_load_or_skip():
    for module in ENGINES:
        try:
            __import__(module)
        except ImportError as exc:
            SKIPS.append("%s (%s)" % (module, exc))
            print("SKIP %s — engine absent" % module)
            continue
        check("%s imports" % module, True)


def test_selenium_refuses_cdp():
    try:
        import selenium_scraper
    except ImportError:
        SKIPS.append("selenium_scraper (refuses-cdp check)")
        return
    args = _bridge_args(["--url", "https://www.potterybarn.com/products/x/"])
    args.cdp_endpoint = "ws://u:p@h:1"
    try:
        selenium_scraper.SeleniumDriver(args).start()
        refused = False
    except Exception as exc:  # noqa: BLE001
        refused = "cannot use --cdp-endpoint" in str(exc)
    check("selenium refuses --cdp-endpoint", refused)


# ---------------------------------------------------------------------------
# Whole-repo checks
# ---------------------------------------------------------------------------

def test_unresolved_names():
    """compileall proves a file PARSES; this proves its names RESOLVE."""
    import builtins
    for name in SHIPPED_PY:
        tree = ast.parse(open(os.path.join(HERE, name)).read())
        bound = set(dir(builtins)) | {"__file__", "__name__"}
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                bound.add(node.name)
            elif isinstance(node, ast.arg):
                bound.add(node.arg)
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                for alias in node.names:
                    bound.add((alias.asname or alias.name).split(".")[0])
            elif isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
                bound.add(node.id)
            elif isinstance(node, ast.ExceptHandler) and node.name:
                bound.add(node.name)
        unresolved = sorted({n.id for n in ast.walk(tree) if isinstance(n, ast.Name)
                             and isinstance(n.ctx, ast.Load) and n.id not in bound})
        eq("names resolve in %s" % name, unresolved, [])


def _local_imports(module, seen):
    path = os.path.join(HERE, module + ".py")
    if module in seen or not os.path.exists(path):
        return
    seen.add(module)
    for node in ast.walk(ast.parse(open(path).read())):
        if isinstance(node, ast.Import):
            for alias in node.names:
                _local_imports(alias.name.split(".")[0], seen)
        elif isinstance(node, ast.ImportFrom) and node.module:
            _local_imports(node.module.split(".")[0], seen)


def test_dockerfile_copies_every_module_the_entrypoints_import():
    text = open(os.path.join(HERE, "Dockerfile")).read()
    copied = set(re.findall(r"([a-z_]+)\.py", text.split("ENTRYPOINT")[0]))
    needed = set()
    for entry in ("api_scraper", "http_scraper", "playwright_scraper", "catalog_walk", "diff_runs"):
        _local_imports(entry, needed)
    eq("nothing the entrypoints import is missing from the image", sorted(needed - copied), [])
    check("the image never copies .env", not re.search(r"COPY[^\n]*\.env", text))


def test_sample_output_matches_the_schema():
    for name, cls in (("sample_output.json", W.Product), ("sample_skus.json", W.SkuRow)):
        path = os.path.join(HERE, name)
        if not os.path.exists(path):
            check("%s exists" % name, False)
            continue
        rows = json.load(open(path))
        eq("%s columns are %s's" % (name, cls.__name__), list(rows[0].keys()),
           [f.name for f in dataclasses.fields(cls)])
        check("%s is non-trivial" % name, len(rows) >= 5)
    header = open(os.path.join(HERE, "sample_output.csv")).readline().strip().split(",")
    eq("sample_output.csv header", header, [f.name for f in dataclasses.fields(W.Product)])


BANNED = ("cloud browser", "antidetect", "gate.2prx.com", "ANTIDETECT_LOCAL_API")
SIBLING_DRIFT = ("HOMEDEPOT_", "homedepot_products", "__APOLLO_STATE__", "Nao=", "bershka",
                 "inditex", "transfermarkt", "sephora", "Miter Saws", "WOMEN / SALE",
                 # The review prompt carried another repo's domain until the
                 # 2026-10-01 audit: Player/Transfer rows, prices in EUR, a
                 # "tokopedia lesson", a page_url() this repo does not have.
                 "player", "transfer list", "tokopedia", "in eur", "page_url(",
                 # An internal operator document; a public file must not cite it.
                 "claude.md")


_TEXT_SUFFIXES = (".py", ".md", ".txt", ".yml", ".toml", ".example", ".sh")


def _shipped_text_files():
    """The files that SHIP: what git tracks, when this is a checkout.

    The first version walked the disk, so in a checkout that held a
    `.claude/worktrees/...` copy of the repo it checked that copy too — and
    the copy's own smoke_test.py, which carries the banned phrases on purpose,
    failed 14 checks that had nothing to do with what ships. Outside git (an
    unpacked sdist), fall back to a walk that skips hidden directories and
    virtualenvs.
    """
    try:
        listed = subprocess.run(["git", "ls-files"], cwd=HERE, capture_output=True,
                                text=True, timeout=30)
        if listed.returncode == 0 and listed.stdout.strip():
            return [f for f in listed.stdout.splitlines()
                    if f.endswith(_TEXT_SUFFIXES) or os.path.basename(f) == "Dockerfile"]
    except (OSError, subprocess.SubprocessError):
        pass
    out = []
    for root, dirs, files in os.walk(HERE):
        dirs[:] = [d for d in dirs if not d.startswith(".") or d == ".github"]
        dirs[:] = [d for d in dirs if d not in ("__pycache__", "fixtures", "live", "venv", "env")
                   and not d.endswith(".egg-info")]
        for f in files:
            if f.endswith(_TEXT_SUFFIXES) or f == "Dockerfile":
                out.append(os.path.relpath(os.path.join(root, f), HERE))
    return out


def test_banned_wordings_and_sibling_drift():
    for rel in _shipped_text_files():
        if rel == "smoke_test.py":
            continue
        text = open(os.path.join(HERE, rel), encoding="utf-8").read()
        low = text.lower()
        for phrase in BANNED:
            check("%s avoids %r" % (rel, phrase), phrase.lower() not in low)
        for phrase in SIBLING_DRIFT:
            check("%s carries no sibling text %r" % (rel, phrase), phrase.lower() not in low)


def main():
    tests = [v for k, v in sorted(globals().items(), key=lambda kv: 0) if k.startswith("test_") and callable(v)]
    for test in tests:
        try:
            test()
        except Exception as exc:  # noqa: BLE001
            import traceback
            FAILURES.append("%s raised %s: %s" % (test.__name__, type(exc).__name__, exc))
            print("FAIL %s raised" % test.__name__)
            traceback.print_exc()
    print("\n%d passed, %d failed, %d skipped" % (len(PASSES), len(FAILURES), len(SKIPS)))
    for skip in SKIPS:
        print("  skipped: %s" % skip)
    for failure in FAILURES:
        print("  FAILED: %s" % failure)
    if os.environ.get("EXPECT_NO_SKIPS_FOR"):
        wanted = os.environ["EXPECT_NO_SKIPS_FOR"]
        if any(s.startswith(wanted) for s in SKIPS):
            print("  FAILED: %s was expected to be installed and was skipped" % wanted)
            return 1
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
