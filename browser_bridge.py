"""Everything the three browser engines share, so they cannot drift apart.

The engines differ only in how they start a browser and how they ask it for
a URL and for the rendered document. Everything that decides what a run DOES
lives here, once. No JavaScript crosses this boundary (a rule of this scraper family).

What the browser is FOR on this site — measured
-----------------------------------------------
* **The listing needs no browser.** It comes from a JSON API that answers a
  plain request from anywhere (api_scraper.py). In `--mode category` the
  browser fetches that same JSON — the call the storefront's own grid makes —
  so a browser run can be checked field for field against api_scraper.py,
  and it was: 72 of 72 rows identical through Playwright, Selenium and
  pyppeteer (2026-10-01). It never visits the `/shop/` page itself.
* **Product pages need the Scraping Browser API**; a local browser is
  refused (0 of 19 trials over 2026-09-24 and 10-01). Over the Scraping
  Browser the answer moved: refused with HTTP 403 on 2026-09-24, served on
  14 of 14 sessions on 2026-10-01 and identical to curl in every field.
  `Captcha.setAutoSolve` was accepted every time and no Captcha event ever
  fired — nothing was ever offered to solve.

**Read the RESPONSE BODY, not the rendered DOM.** After hydration the
storefront deletes its `<script>window.__INITIAL_STATE__=...</script>` tag,
so `content()` of a perfectly served product page classifies as `empty`
(measured 2026-10-01: six sessions looked empty before this was found).
`fetch_product` therefore reads the navigation's own body first and falls
back to the live `window.__INITIAL_STATE__` object (Selenium has no response
object), recording which in `state_source`.

http_scraper.py stays the default product-page path: it needs no browser and
holds no profile lock.

The ladder, for when a challenge DOES appear
--------------------------------------------
Rung 1a — over `--cdp-endpoint`, `Captcha.setAutoSolve` is armed on connect
          and its events are counted into the sidecar (`autosolve_events`).
Rung 2  — `captcha_solver.py`, only for a rendered widget with a sitekey,
          only with `--solve-captcha` not `never`, and never fatal.
A 403 is on neither rung: it is a decision about this client and this exit.

One connection per run
----------------------
A Scraping Browser profile allows ONE live connection, and an abnormally
ended one holds it: measured 2026-09-24, a navigation that timed out (60 s,
`wait_until="load"` on a page whose load event never fires) left the profile
answering 500 on the next connect. So a run opens exactly one connection,
closes it in `finally`, never retries a connect, and never calls
`GET /json/version` — that check takes the lock itself (a rule of this scraper family).
"""

from __future__ import annotations

import html as html_lib
import json
import logging
import re
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

import env_config
import output_writer
import page_flow
import product_parser as P
import proxy_pool
from output_writer import EXIT_REMOTE_API_ERROR, finish_run

logger = logging.getLogger("potterybarn")

DEFAULT_OUT = {"category": "potterybarn_products", "product": "potterybarn_skus"}
_PRE_RE = re.compile(r"<pre[^>]*>(.*?)</pre>", re.S | re.I)


def mask_text(text: str) -> str:
    return proxy_pool.mask_text(text)


class BridgeError(RuntimeError):
    """The BROWSER could not do its job — the site never answered.

    A navigation timeout, a dropped CDP socket, a locked profile: our
    plumbing, not a refusal. Exit 5, never 3 (a rule of this scraper family).
    """


# ---------------------------------------------------------------------------
# The driver protocol
# ---------------------------------------------------------------------------
#
# An engine supplies an object with:
#   name          str
#   over_cdp      bool, after start()
#   start()       bring a browser up (ONE connection over CDP)
#   navigate(url) -> Optional[int]   HTTP status if the engine can see one
#   content()     -> str             the rendered document
#   response_body() -> Optional[str] OPTIONAL: the last navigation's body
#   state_json()  -> Optional[str]   OPTIONAL: JSON of window.__INITIAL_STATE__
#   sleep(ms)
#   stop()
# and, optionally, autosolve_armed / autosolve_events / autosolve_notes.


def json_from_document(document: str) -> Optional[Dict[str, Any]]:
    """The JSON a browser shows for a JSON URL.

    Chromium renders `application/json` as `<pre>{...}</pre>` inside a
    generated HTML document; a CDP content() call returns that document, not
    the raw body. Take the `<pre>`, unescape it, parse it. A document that is
    already raw JSON is accepted too.
    """
    text = document or ""
    match = _PRE_RE.search(text)
    candidate = html_lib.unescape(match.group(1)) if match else text
    candidate = candidate.strip()
    start = candidate.find("{")
    if start < 0:
        return None
    try:
        parsed = json.loads(candidate[start:])
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


def state_document(state_json: Optional[str]) -> Optional[str]:
    """Wrap `JSON.stringify(window.__INITIAL_STATE__)` back into the shape the
    parser reads, so the live JS object and a served page parse the same way."""
    if not state_json or state_json in ("null", "undefined"):
        return None
    return "<script>window.__INITIAL_STATE__=%s</script>" % state_json


def wait_for_product(driver, timeout_ms: int, log=None) -> Tuple[str, str]:
    """Poll until a product's state is readable, or time runs out.

    Returns `(html, source)`. The rendered DOM is NOT enough on this site:
    after hydration the storefront DELETES its `<script>window.
    __INITIAL_STATE__=...</script>` tag (measured 2026-10-01 over the Scraping
    Browser: the served body classified `product`, the DOM three seconds later
    `empty`), while the JS object itself stays alive. So each poll reads the
    DOM and, if the engine offers `state_json()`, the live object.

    Returns what is there at the end rather than raising: "did not paint" and
    "was refused" want different exit codes, and the caller classifies.
    """
    waited, step = 0, 500
    getter = getattr(driver, "state_json", None)
    while True:
        html = driver.content() or ""
        state = P.detect_page_state(html)
        if state in ("product", "blocked", "geo_redirect", "captcha"):
            source = "dom"
            break
        if getter is not None:
            try:
                live = state_document(getter())
            except Exception:  # noqa: BLE001 - a page mid-navigation
                live = None
            if live and P.detect_page_state(live) == "product":
                html, source = live, "window-state"
                break
        if waited >= timeout_ms:
            source = "dom"
            break
        driver.sleep(step)
        waited += step
    if log:
        log("  readiness wait: %dms, %d bytes (%s)" % (waited, len(html), source))
    return html, source


def solve_if_captcha(driver, args, url: str, html: str, log=None) -> Optional[str]:
    """Rung 2. Fresh HTML if a solve happened, else None. Never fatal."""
    if getattr(args, "solve_captcha", "when-blocked") == "never":
        return None
    if P.detect_page_state(html, url=url) != "captcha":
        return None
    key = getattr(args, "twocaptcha_key", None)
    if not key:
        print("[!] a captcha was detected but no TWOCAPTCHA_KEY is set — "
              "continuing without solving.", file=sys.stderr)
        return None
    try:
        import captcha_solver
        solved = captcha_solver.solve_on_page(
            driver, html, url=url, api_key=key,
            api_version=getattr(args, "captcha_api", "v2"),
            min_score=getattr(args, "min_score", 0.7))
    except Exception as exc:  # noqa: BLE001 - a solver failure is not a crash
        print("[!] solver failed: %s: %s" % (type(exc).__name__, mask_text(str(exc))),
              file=sys.stderr)
        return None
    if not solved:
        return None
    if log:
        log("  solver returned a token; re-reading the page")
    driver.sleep(3000)
    return driver.content()


def fetch_product(driver, args, url: str, log=None) -> Dict[str, Any]:
    """One product page. The navigation's own RESPONSE BODY is read first —
    the bytes the server sent, identical in kind to what curl gets — because
    the rendered DOM loses the state on hydration (see wait_for_product).
    """
    try:
        status = driver.navigate(url)
    except Exception as exc:  # noqa: BLE001
        raise BridgeError("navigation failed: %s: %s"
                          % (type(exc).__name__, mask_text(str(exc)))) from None
    body = None
    reader = getattr(driver, "response_body", None)
    if reader is not None:
        try:
            body = reader()
        except Exception:  # noqa: BLE001 - e.g. a redirect with no body
            body = None
    if body:
        state = P.detect_page_state(body, status=status, url=url)
        if state in ("product", "blocked", "geo_redirect"):
            return {"html": body, "state": state, "status": status,
                    "source": "response-body"}
    html, source = wait_for_product(driver, page_flow.content_timeout_ms("product"), log=log)
    state = P.detect_page_state(html, status=status, url=url)
    if state == "captcha":
        fresh = solve_if_captcha(driver, args, url, html, log=log)
        if fresh:
            html = fresh
            state = P.detect_page_state(html, url=url)
    return {"html": html, "state": state, "status": status, "source": source}


def fetch_browse(driver, group_id: str, key: str, page: int, log=None
                 ) -> Tuple[str, Optional[Dict[str, Any]], Optional[int]]:
    """One Constructor browse page through the browser."""
    from urllib.parse import urlencode
    url = "%s?%s" % (P.browse_url(group_id),
                     urlencode({"key": key, "page": page,
                                "num_results_per_page": P.PAGE_SIZE}))
    try:
        status = driver.navigate(url)
    except Exception as exc:  # noqa: BLE001
        raise BridgeError("navigation failed: %s: %s"
                          % (type(exc).__name__,
                             mask_text(str(exc)).replace(key, "key_***"))) from None
    payload = None
    reader = getattr(driver, "response_body", None)
    if reader is not None:
        try:
            body = reader()
            parsed = json.loads(body) if body else None
            payload = parsed if isinstance(parsed, dict) else None
        except Exception:  # noqa: BLE001 - fall back to the rendered document
            payload = None
    if payload is None:
        payload = json_from_document(driver.content())
    import api_scraper
    return api_scraper.classify_api(status if status is not None else
                                    (200 if payload else None), payload, None), payload, status


# ---------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------

def _group_for(args) -> Tuple[str, str]:
    if args.group_id:
        return args.group_id, "flag"
    if not args.url:
        raise output_writer.UsageError("nothing to fetch: pass --group-id (e.g. sofa) or "
                         "--url with a leaf category or a product page")
    if not P.is_category_url(args.url):
        raise output_writer.UsageError("refusing %s: not a category page (/shop/...)" % args.url)
    if P.is_department_path(args.url):
        raise output_writer.UsageError(
            "refusing %s: a top-level /shop/<department>/ is a hub with no grid. "
            "Pick a leaf: `python3 catalog_walk.py categories --leaf-only`." % args.url)
    return args.url.rstrip("/").rsplit("/", 1)[-1], "url-guess"


def _check_url(args) -> None:
    if not args.url:
        return
    ok, reason = P.supported_host(args.url)
    if not ok:
        raise output_writer.UsageError("refusing %s: %s" % (args.url, reason))
    if not P.robots_allows(args.url):
        raise output_writer.UsageError("refusing %s: disallowed by potterybarn.com's robots.txt"
                         % args.url)
    if args.mode == "product" and not P.slug_from_url(args.url):
        raise output_writer.UsageError("refusing %s: --mode product needs a /products/<slug>/ "
                         "URL" % args.url)


def run(args, driver) -> int:
    """Drive one browser engine end to end and return its exit code."""
    log = (lambda m: print(m, file=sys.stderr)) if getattr(args, "verbose", False) \
        else (lambda m: None)
    _check_url(args)
    if args.mode == "product" and not args.url:
        raise output_writer.UsageError("--mode product needs --url with a product page")
    if args.mode == "category":
        group_id, group_source = _group_for(args)
        if group_source == "url-guess":
            log("group id %r taken from the URL (it matched the page's own "
                "categoryId on 15 of 15 categories sampled 2026-09-24)" % group_id)
        start_url = P.browse_url(group_id)
    else:
        group_id, group_source, start_url = None, None, args.url

    facts: Dict[str, Any] = {
        "transport": driver.name,
        "pages_requested": args.pages if args.mode == "category" else 1,
        "pages_completed": 0,
        "failed_pages": [],
        "states": [],
        "blocked": False,
        "transport_error": False,
        "group_id": group_id,
        "group_id_source": group_source,
        "start_url": start_url,
        "final_url": start_url,
    }
    rows: List[Any] = []
    seen: set = set()

    try:
        driver.start()
    except Exception as exc:  # noqa: BLE001
        print("could not start %s: %s: %s"
              % (driver.name, type(exc).__name__, mask_text(str(exc))),
              file=sys.stderr)
        return EXIT_REMOTE_API_ERROR
    facts["over_cdp"] = bool(getattr(driver, "over_cdp", False))
    if not facts["over_cdp"]:
        print("[!] a LOCAL browser was measured refused on this site's product "
              "pages: 0 of 19 trials got one (2026-09-24 and 10-01), headless "
              "and headful, where curl through the same exits got the page. "
              "Listings are unaffected (they come from the API). Use "
              "http_scraper.py for product pages.", file=sys.stderr)

    try:
        if args.mode == "product":
            result = fetch_product(driver, args, args.url, log=log)
            facts["states"].append(result["state"])
            facts["http_status"] = result["status"]
            facts["state_source"] = result.get("source")
            _dump(args, result["html"], 1)
            if result["state"] == "product":
                rows, page_facts = P.parse_product(result["html"], url=args.url,
                                                   category=args.category)
                for row in rows:
                    # The same numbering http_scraper uses (product 1 of 1),
                    # so the two transports agree field for field.
                    row.page = 1
                facts["product"] = page_facts
                facts["pages_completed"] = 1
                if not page_facts["sku_check"]["closes"] and not args.accept_count_mismatch:
                    print("[!] %s: %d SKUs counted, the page states skuCount=%s — "
                          "run marked partial (--accept-count-mismatch to override)"
                          % (page_facts["product_id"], page_facts["sku_check"]["counted"],
                             page_facts["sku_check"]["sku_count_stated"]), file=sys.stderr)
                    facts["failed_pages"].append(1)
                    facts["sku_count_mismatch"] = True
            else:
                facts["failed_pages"].append(1)
                facts["blocked"] = page_flow.counts_as_blocked(result["state"])
                facts["transport_error"] = result["state"] == "transport_error"
                print("product page: %s (HTTP %s, %d bytes)"
                      % (result["state"], result["status"], len(result["html"])),
                      file=sys.stderr)
        else:
            key = P.FALLBACK_CONSTRUCTOR_KEY
            currency = P.KNOWN_KEY_CURRENCY.get(key)
            facts.update({"key_source": "fallback", "currency": currency,
                          "currency_source": "known-key", "totals_seen": []})
            label = args.category or (P.category_from_url(args.url) if args.url
                                      else group_id)
            for page in range(1, args.pages + 1):
                state, payload, status = fetch_browse(driver, group_id, key, page, log)
                facts["states"].append(state)
                if state != "api":
                    facts["failed_pages"].append(page)
                    facts["blocked"] = page_flow.counts_as_blocked(state)
                    facts["transport_error"] = state == "transport_error"
                    print("page %d: %s (HTTP %s)" % (page, state, status), file=sys.stderr)
                    break
                facts["totals_seen"].append(P.browse_total(payload))
                if page == 1:
                    facts["group"] = P.browse_group(payload)
                page_rows = P.parse_browse_response(payload, currency=currency,
                                                    category=label)
                for row in page_rows:
                    row.page = page
                fresh = output_writer.dedupe_by_sku(page_rows, seen)
                rows.extend(fresh)
                facts["pages_completed"] = page
                log("page %d: %d results (%d new, %d total)"
                    % (page, len(page_rows), len(fresh), len(rows)))
                if not page_rows or not fresh:
                    break
                if args.max_products and len(rows) >= args.max_products:
                    rows = rows[:args.max_products]
                    facts["max_products_reached"] = True
                    break
                if page < args.pages:
                    time.sleep(args.delay)
    except BridgeError as exc:
        print("[!] %s" % exc, file=sys.stderr)
        facts["transport_error"] = True
        facts["transport_error_detail"] = str(exc)[:300]
    except P.DecodeError as exc:
        print("[!] %s" % exc, file=sys.stderr)
        facts["failed_pages"].append(1)
    finally:
        try:
            driver.stop()
        except Exception as exc:  # noqa: BLE001
            logger.warning("%s did not shut down cleanly: %s", driver.name,
                           mask_text(str(exc)))

    facts["autosolve_armed"] = getattr(driver, "autosolve_armed", False)
    facts["autosolve_events"] = getattr(driver, "autosolve_events", [])
    facts["autosolve_notes"] = getattr(driver, "autosolve_notes", [])

    return finish_run(
        rows, args.out, args.format, args.allow_empty,
        blocked=facts["blocked"], transport_error=facts["transport_error"],
        stop_reason=_stop_reason(args, facts),
        pages_requested=facts["pages_requested"],
        pages_completed=facts["pages_completed"],
        pages_failed=facts["failed_pages"],
        mode=args.mode,
        scope={"group_id": group_id, "max_products": args.max_products} if group_id else {},
        start_url=facts["start_url"], final_url=facts["final_url"],
        extra=facts,
    )


def _stop_reason(args, facts: Dict[str, Any]) -> str:
    if facts["transport_error"]:
        return "transport_error"
    if facts["blocked"]:
        return "blocked"
    if facts.get("sku_count_mismatch"):
        return "sku_count_mismatch"
    if facts.get("max_products_reached"):
        return "max_products_reached"
    if facts["failed_pages"]:
        return "incomplete"
    if args.mode == "product":
        return "single_page_mode"
    if facts["pages_completed"] >= args.pages:
        return "completed"
    return "pagination_exhausted"


def _dump(args, html: str, page: int) -> None:
    if not getattr(args, "dump_html", None):
        return
    import os
    root, ext = os.path.splitext(args.dump_html)
    path = "%s_p%d%s" % (root, page, ext or ".html")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(html or "")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# CLI, defined once for all three engines
# ---------------------------------------------------------------------------

def _positive_int(value):
    import argparse
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be >= 1, got %s" % value)
    return number


def _non_negative_float(value):
    import argparse
    number = float(value)
    if number < 0:
        raise argparse.ArgumentTypeError("must be >= 0, got %s" % value)
    return number


def build_parser(description: str):
    """Every flag the browser engines share, defined ONCE."""
    import argparse
    p = argparse.ArgumentParser(
        description=description,
        epilog="Credentials belong in .env, never on a command line — `ps` "
               "reads argv and argv lands in shell history.")
    p.add_argument("--url", default=None,
                   help="A product page (--mode product) or a leaf category "
                        "(--mode category) on www.potterybarn.com.")
    p.add_argument("--group-id", default=None,
                   help="--mode category: the Constructor browse group, e.g. sofa.")
    p.add_argument("--mode", choices=["category", "product"], default="product")
    p.add_argument("--category", default=None)
    p.add_argument("--pages", type=_positive_int, default=1)
    p.add_argument("--max-products", type=_positive_int, default=None)
    p.add_argument("--accept-count-mismatch", action="store_true",
                   help="--mode product: do not mark the run partial when the "
                        "SKU count disagrees with the page's skuCount.")
    p.add_argument("--delay", type=_non_negative_float, default=1.0)
    p.add_argument("--timeout", type=_positive_int, default=45)
    p.add_argument("--format", choices=["json", "csv", "both"], default="both")
    p.add_argument("--out", default=None)
    p.add_argument("--allow-empty", action="store_true")
    p.add_argument("--dump-html", default=None)
    p.add_argument("--verbose", action="store_true")
    p.add_argument("--browser-path", default=None,
                   help="A local browser binary to launch instead of the "
                        "engine's own. Needed for pyppeteer on Apple silicon: "
                        "its downloaded Chromium is an x86_64 build and did "
                        "not start under Rosetta (2026-10-01); the system "
                        "Chrome did. Ignored with --cdp-endpoint.")
    p.add_argument("--headless", action="store_true", default=True)
    p.add_argument("--headful", dest="headless", action="store_false")
    p.add_argument("--cdp-endpoint", default=None,
                   help="Connect to a running browser over CDP. Falls back to "
                        "POTTERYBARN_CDP_ENDPOINT in .env — never pass this on "
                        "a command line, it contains a password.")
    p.add_argument("--proxy", default=None,
                   help="Prefer POTTERYBARN_PROXY in .env. Ignored with "
                        "--cdp-endpoint: the remote browser brings its own exit.")
    p.add_argument("--proxy-file", default=None)
    p.add_argument("--proxy-rotate", choices=proxy_pool.ROTATE_MODES,
                   default="per-run")
    p.add_argument("--proxy-shuffle", action="store_true")
    p.add_argument("--twocaptcha-key", default=None,
                   help="Prefer TWOCAPTCHA_KEY in .env — `ps` reads argv.")
    p.add_argument("--captcha-api", choices=["v2", "v1"], default="v2")
    p.add_argument("--solve-captcha", choices=["when-blocked", "always", "never"],
                   default="when-blocked")
    p.add_argument("--min-score", type=float, default=0.7)
    return p


def parse_args(parser, argv=None):
    args = parser.parse_args(argv)
    env_config.apply(args)
    if args.cdp_endpoint and args.proxy:
        print("[!] --proxy is ignored with --cdp-endpoint: the remote browser "
              "brings its own exit.", file=sys.stderr)
        args.proxy = None
    if args.out is None:
        args.out = DEFAULT_OUT[args.mode]
    return args


def playwright_main(argv=None) -> int:
    """The `potterybarn-browser` console script.

    The core wheel installs it, while Playwright is an optional extra, and
    `playwright_scraper.py` imports Playwright at MODULE level on purpose (the
    offline suite can only tell "engine absent" from "engine broken" that
    way). Pointing the script straight at it made a base install fail on
    `--help` with a ModuleNotFoundError (audit, 2026-10-01). This wrapper
    says what to install, and exits 2.
    """
    try:
        import playwright_scraper
    except ImportError as exc:
        print("potterybarn-browser needs Playwright, which is an optional extra "
              "(%s).\n  pip install 'potterybarn-scraper[playwright]' && "
              "playwright install chromium\nListings and product pages do not "
              "need a browser: potterybarn-listing and potterybarn-products."
              % exc, file=sys.stderr)
        return output_writer.EXIT_USAGE
    return playwright_scraper.main(argv)
