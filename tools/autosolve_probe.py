#!/usr/bin/env python3
"""Run N live browser iterations and record what the challenge ladder did.

Answers three questions the ordinary CLI does not:

1. **Did auto-solve fire?** Over the Scraping Browser API this arms
   `Captcha.setAutoSolve`, subscribes to every Captcha event and records
   each with a timestamp, so "the browser cleared it" stops being an
   assumption.
2. **What did the page serve?** The state is recorded before and after the
   readiness wait.
3. **Do repeats agree?** `tools/compare_runs.py` compares the iterations.

Each iteration is ONE connection, closed in `finally`. There is no
`GET /json/version` pre-flight: that check takes the profile lock itself. A
connect that FAILS ends the run's CDP use — after an abnormal end the profile
is held for a long time, and every further attempt re-takes it.

    python3 tools/autosolve_probe.py --iterations 10
    python3 tools/compare_runs.py --in live/autosolve
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from urllib.parse import urlencode

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

import browser_bridge  # noqa: E402
import env_config  # noqa: E402
import product_parser as P  # noqa: E402
import proxy_pool  # noqa: E402

from playwright.sync_api import sync_playwright  # noqa: E402

# A mix, on purpose. Repeats of one target test whether the scraper is
# DETERMINISTIC; different targets test it in more than one corner. `category`
# targets are the Constructor browse API fetched through the browser; the hub
# is a potterybarn.com page that must classify as `hub` and yield nothing.
TARGETS = [
    ("sofa-api", "category", "sofa"),
    ("sofa-api", "category", "sofa"),
    ("sofa-api", "category", "sofa"),
    ("dining-benches-api", "category", "dining-benches"),
    ("end-tables-api", "category", "end-tables"),
    ("delaney-pdp", "product",
     "https://www.potterybarn.com/products/delaney-marble-end-table/"),
    ("delaney-pdp", "product",
     "https://www.potterybarn.com/products/delaney-marble-end-table/"),
    ("york-pdp", "product",
     "https://www.potterybarn.com/products/york-slope-arm-deep-slipcovered-sofa-collection/"),
    ("sofa-api", "category", "sofa"),
    ("furniture-hub", "hub", "https://www.potterybarn.com/shop/furniture/"),
]

CAPTCHA_EVENTS = ("Captcha.solveFinished", "Captcha.solveStarted",
                  "Captcha.detected", "Captcha.solveFailed")


def now():
    return datetime.now(timezone.utc).isoformat()


def run_one(index, name, mode, target, pages, timeout, endpoint):
    record = {"iteration": index, "target": name, "mode": mode,
              "started_at": now(), "captcha_events": [], "autosolve_notes": [],
              "autosolve_armed": False, "states": [], "rows": 0, "skus": [],
              "error": None, "connect_failed": False, "pre_wait_state": None,
              "ms": 0, "transport": "scraping-browser-cdp"}
    rows = []
    started = time.time()
    with sync_playwright() as pw:
        try:
            browser = pw.chromium.connect_over_cdp(endpoint, timeout=timeout * 1000)
        except Exception as exc:  # noqa: BLE001
            record["error"] = "connect: %s" % proxy_pool.mask_text(
                str(exc).splitlines()[0])[:200]
            record["connect_failed"] = True
            record["ms"] = int((time.time() - started) * 1000)
            return rows, record
        try:
            ctx = browser.contexts[0] if browser.contexts else browser.new_context()
            page = ctx.new_page()
            cdp = ctx.new_cdp_session(page)
            for event in CAPTCHA_EVENTS:
                cdp.on(event, lambda payload, e=event: record["captcha_events"].append(
                    {"at": now(), "event": e, "payload": str(payload)[:400]}))
            try:
                cdp.send("Captcha.setAutoSolve", {"autoSolve": True})
                record["autosolve_armed"] = True
                record["autosolve_notes"].append("Captcha.setAutoSolve: ok")
            except Exception as exc:  # noqa: BLE001
                record["autosolve_notes"].append(str(exc).splitlines()[0][:120])
            page.set_default_timeout(timeout * 1000)

            if mode == "category":
                seen = set()
                for page_no in range(1, pages + 1):
                    url = "%s?%s" % (P.browse_url(target), urlencode(
                        {"key": P.FALLBACK_CONSTRUCTOR_KEY, "page": page_no,
                         "num_results_per_page": P.PAGE_SIZE}))
                    response = page.goto(url, wait_until="commit",
                                         timeout=timeout * 1000)
                    try:
                        payload = json.loads(response.text()) if response else None
                    except Exception:  # noqa: BLE001
                        payload = browser_bridge.json_from_document(page.content())
                    state = "api" if payload else "empty"
                    record["states"].append({"page": page_no, "state": state,
                                             "status": response.status if response else None,
                                             "total": P.browse_total(payload or {})})
                    if not payload:
                        break
                    page_rows = P.parse_browse_response(
                        payload, currency=P.KNOWN_KEY_CURRENCY.get(P.FALLBACK_CONSTRUCTOR_KEY),
                        category=target)
                    fresh = [r for r in page_rows if r.sku not in seen]
                    for row in fresh:
                        seen.add(row.sku)
                        row.page = page_no
                    rows.extend(fresh)
                    if not fresh:
                        break
            else:
                response = page.goto(target, wait_until="commit",
                                     timeout=timeout * 1000)
                status = response.status if response else None
                # The served body first: the storefront deletes its state
                # <script> from the DOM on hydration (2026-10-01).
                body = response.text() if response else ""
                record["pre_wait_state"] = P.detect_page_state(body, status=status,
                                                               url=page.url)
                if record["pre_wait_state"] == "product":
                    html, record["state_source"] = body, "response-body"
                else:
                    html, record["state_source"] = browser_bridge.wait_for_product(
                        _Adapter(page), 0 if record["pre_wait_state"] in (
                            "blocked", "hub", "geo_redirect") else 15000)
                state = P.detect_page_state(html, status=status, url=page.url)
                record["states"].append({"page": 1, "state": state, "status": status,
                                         "bytes": len(html)})
                if state == "product":
                    rows, facts = P.parse_product(html, url=target)
                    record["sku_check"] = facts["sku_check"]
            record["rows"] = len(rows)
            record["skus"] = [r.sku for r in rows]
        except Exception as exc:  # noqa: BLE001
            record["error"] = "%s: %s" % (type(exc).__name__, proxy_pool.mask_text(
                str(exc).splitlines()[0])[:200])
        finally:
            try:
                browser.close()
            except Exception:  # noqa: BLE001
                pass
    record["ms"] = int((time.time() - started) * 1000)
    return rows, record


class _Adapter:
    """Just the two driver operations `wait_for_product` uses."""

    def __init__(self, page):
        self._page = page

    def content(self):
        try:
            return self._page.content()
        except Exception:  # noqa: BLE001
            return ""

    def sleep(self, ms):
        self._page.wait_for_timeout(ms)

    def state_json(self):
        return self._page.evaluate("() => JSON.stringify(window.__INITIAL_STATE__ || null)")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--iterations", type=int, default=10)
    parser.add_argument("--pages", type=int, default=2)
    parser.add_argument("--timeout", type=int, default=60)
    parser.add_argument("--out", default="live/autosolve")
    parser.add_argument("--start", type=int, default=1,
                        help="Iteration to start at (1-based), to resume a run "
                             "that stopped on a held profile.")
    parser.add_argument("--append", action="store_true",
                        help="Keep the iterations already in --out.")
    parser.add_argument("--settle", type=float, default=25.0,
                        help="Seconds between iterations; a just-closed profile "
                             "is held briefly.")
    args = parser.parse_args(argv)

    env = argparse.Namespace(cdp_endpoint=None, proxy=None, twocaptcha_key=None, url=None)
    env_config.apply(env, quiet=True)
    if not env.cdp_endpoint:
        raise SystemExit("no POTTERYBARN_CDP_ENDPOINT in .env")

    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    records, all_rows = [], {}
    if args.append and os.path.exists(args.out + ".jsonl"):
        with open(args.out + ".jsonl", encoding="utf-8") as handle:
            records = [json.loads(line) for line in handle if line.strip()
                       and json.loads(line)["iteration"] < args.start]
        with open(args.out + ".rows.json", encoding="utf-8") as handle:
            all_rows = {int(k): v for k, v in json.load(handle).items()
                        if int(k) < args.start}
    for index in range(args.start - 1, args.iterations):
        name, mode, target = TARGETS[index % len(TARGETS)]
        print("[%2d/%d] %-20s %s" % (index + 1, args.iterations, name, mode), flush=True)
        rows, record = run_one(index + 1, name, mode, target, args.pages,
                               args.timeout, env.cdp_endpoint)
        records.append(record)
        all_rows[index + 1] = [r.__dict__ for r in rows]
        print("        states=%s rows=%d captcha_events=%d%s"
              % ([s["state"] for s in record["states"]], record["rows"],
                 len(record["captcha_events"]),
                 " ERR " + record["error"] if record["error"] else ""), flush=True)
        with open(args.out + ".jsonl", "w", encoding="utf-8") as handle:
            for item in records:
                handle.write(json.dumps(item, ensure_ascii=False) + "\n")
        with open(args.out + ".rows.json", "w", encoding="utf-8") as handle:
            json.dump(all_rows, handle, ensure_ascii=False)
        if record["connect_failed"]:
            print("CDP connect failed — stopping; the profile needs to be left "
                  "alone, not retried.", flush=True)
            break
        if index + 1 < args.iterations:
            time.sleep(args.settle)
    print("\nwrote %s.jsonl and %s.rows.json" % (args.out, args.out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
