#!/usr/bin/env python3
"""potterybarn.com category listings via the site's own listing API.

    python3 api_scraper.py --group-id sofa --all-pages --out sofas
    python3 api_scraper.py --url https://www.potterybarn.com/shop/furniture/sofa/ --pages 3

One row per PRODUCT, with its price range (see output_writer).

Why an API, and whose
---------------------
potterybarn.com's leaf category page does not carry its grid: the served
HTML has ZERO product links, and the page's state has
`catDataFromConstructor: {}`. The grid is filled client-side by a call to
Constructor.io's browse API, with the storefront's own public key:

    GET https://ac.cnstrc.com/browse/group_id/<categoryId>?key=<key>&page=<n>&num_results_per_page=24

This module makes that call. Measured 2026-09-24 from a residential address
in Moscow, no proxy, no browser, no special headers: HTTP 200, 218 KB, 24
results, `total_num_results: 434` for `sofa`. The same call through a US exit
returned an identical response (recon, 2026-09-23).

`ac.cnstrc.com` is a THIRD-PARTY host. potterybarn.com's robots.txt governs
www.potterybarn.com and does not speak for it — see README, "The listing API
is a third-party host". This module sends exactly the request the
storefront's own grid sends, one page at a time, and honours the API's
rate-limit headers.

What the call needs, and where each part comes from
---------------------------------------------------
* `group_id` — the leaf page's `__INITIAL_STATE__.shop.categoryId`. Read from
  the page when a US exit is available. Otherwise pass `--group-id`; with
  only `--url`, the URL's last path segment is used AS A GUESS, logged and
  recorded in the sidecar as `group_id_source: url-guess`.
* `key` — the page's `shop.searchEngineConfig.constructorKey`, or the last
  known value (`product_parser.FALLBACK_CONSTRUCTOR_KEY`) with a warning.
* `currency` — the page's `shop.currencyData.selectedCurrency`, or the
  currency measured for the fallback key. The API itself states none.

Rate limit
----------
The API answers with `x-ratelimit-limit: 201` and an `x-ratelimit-remaining`
that falls by about 0.8 per UNCACHED call (identical URLs are served from its
cache, `max-age=60`, and cost nothing). Measured 2026-10-01: it reached 0
after 240 uncached calls in 159 s — and then nothing happened. 194 further
calls at zero all answered 200 with full results; no 429, no `Retry-After`,
no truncated page. The budget is per CLIENT ADDRESS, not per key: the same
key through another address read `remaining: 200` while this one read 0.

So this module does not stop at zero; below `RATE_LIMIT_FLOOR` it slows to
one page per `RATE_LIMIT_LOW_PACE` seconds, and a 429 — which was never
observed — is still treated as a refusal with a backoff, never as an empty
page.

Pagination terminates on DATA: a page that adds no new product id ends the
listing. `total_num_results` is recorded and checked against the row count
after the run, but never used to decide when to stop — on Home Depot the
equivalent figure moved 144 -> 143 in four minutes. Here it held at 434 over
twelve calls in ten seconds (2026-09-24), which is not the same as "it never
moves".
"""

from __future__ import annotations

import argparse
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

import env_config
import output_writer
import product_parser as P
import proxy_pool
from output_writer import Product, finish_run

try:
    import requests
except ImportError:  # pragma: no cover
    requests = None

DEFAULT_OUT = "potterybarn_products"
RATE_LIMIT_FLOOR = 10
# Seconds between pages once the API's own budget runs low. Not "until the
# reset": at zero the API keeps answering 200 with full results (measured
# 2026-10-01, 194 calls past zero), and its reset moved ~20-30 minutes out,
# so waiting for it turned a 20-page category into a 20-minute run for
# nothing. A slower pace is the courtesy; stopping is not needed.
RATE_LIMIT_LOW_PACE = 5.0


class ApiResult:
    __slots__ = ("status", "payload", "headers", "error", "elapsed_ms")

    def __init__(self, status=None, payload=None, headers=None, error=None,
                 elapsed_ms=0):
        self.status = status
        self.payload = payload
        self.headers = headers or {}
        self.error = error
        self.elapsed_ms = elapsed_ms

    @property
    def state(self) -> str:
        return classify_api(self.status, self.payload, self.error)


def classify_api(status: Optional[int], payload: Any, error: Optional[str]) -> str:
    """The browse call's answer in page_flow's vocabulary."""
    if status is None:
        return "transport_error"
    if status == 429:
        return "rate_limited"
    if status in (401, 403):
        return "blocked"
    if status >= 500:
        return "transport_error"
    if status != 200 or not isinstance(payload, dict) \
            or not isinstance(payload.get("response"), dict):
        return "empty"
    return "api"


def _redact(text: str, key: Optional[str]) -> str:
    """The key rides in the query string, and `requests` puts the full URL in
    every exception. It is a PUBLIC key, but the rule is the family's and it
    costs nothing to keep: redact before anything is printed."""
    out = proxy_pool.mask_text(str(text))
    return out.replace(key, "key_***") if key else out


def browse(session, group_id: str, key: str, page: int, *,
           per_page: int = P.PAGE_SIZE, timeout: int = 30,
           proxies: Optional[dict] = None, sort: Optional[Tuple[str, str]] = None
           ) -> ApiResult:
    started = time.time()
    params = {"key": key, "page": page, "num_results_per_page": per_page}
    if sort:
        params["sort_by"], params["sort_order"] = sort
    try:
        response = session.get(
            P.browse_url(group_id),
            params=params,
            headers={"accept": "application/json"},
            timeout=timeout, proxies=proxies)
    except Exception as exc:  # noqa: BLE001
        return ApiResult(error="%s: %s" % (type(exc).__name__, _redact(exc, key)),
                         elapsed_ms=int((time.time() - started) * 1000))
    try:
        payload = response.json()
    except ValueError:
        payload = None
    return ApiResult(status=response.status_code, payload=payload,
                     headers={k.lower(): v for k, v in response.headers.items()},
                     elapsed_ms=int((time.time() - started) * 1000))


def rate_limit_pause(headers: Dict[str, str], now: Optional[float] = None) -> float:
    """Seconds to wait before the next call, from the API's own headers.

    `now` is kept for the call signature the suite uses; the pace no longer
    depends on the reset time (see RATE_LIMIT_LOW_PACE).
    """
    try:
        remaining = int(headers.get("x-ratelimit-remaining", ""))
    except ValueError:
        return 0.0
    return 0.0 if remaining >= RATE_LIMIT_FLOOR else RATE_LIMIT_LOW_PACE


def resolve_listing(args, log) -> Dict[str, Any]:
    """group_id, key and currency — and where each came from."""
    context: Dict[str, Any] = {"group_id": args.group_id, "key": None,
                               "currency": None, "group_id_source": None,
                               "key_source": None, "currency_source": None,
                               "page_state": None}
    if args.group_id:
        context["group_id_source"] = "flag"
    if args.url:
        ok, reason = P.supported_host(args.url)
        if not ok:
            raise output_writer.UsageError("refusing %s: %s" % (args.url, reason))
        if not P.is_category_url(args.url):
            raise output_writer.UsageError("refusing %s: not a category page (/shop/...). "
                             "For a product page use http_scraper.py." % args.url)
        if not P.robots_allows(args.url):
            raise output_writer.UsageError("refusing %s: disallowed by potterybarn.com's "
                             "robots.txt (a facet or filtered view). Page the "
                             "plain category instead." % args.url)
        pool = proxy_pool.from_args(args)
        if pool and not args.no_page:
            page = read_listing_page(args, pool, log)
            context["page_state"] = page["state"]
            if page["state"] == "hub":
                raise HubError(page["final_url"])
            if page["state"] == "listing":
                ctx = page["context"]
                if ctx["group_id"] and not args.group_id:
                    context["group_id"], context["group_id_source"] = ctx["group_id"], "page"
                if ctx["key"]:
                    context["key"], context["key_source"] = ctx["key"], "page"
                if ctx["currency"]:
                    context["currency"], context["currency_source"] = ctx["currency"], "page"
        if not context["group_id"] and P.is_department_path(args.url):
            # A top-level /shop/<department>/ is a hub. Guessing its last
            # segment would ask the API for a broad group instead —
            # `furniture` is 8,278 results headed by a curtain rod — and
            # report it as a complete listing (measured 2026-10-01).
            raise HubError(args.url)
        if not context["group_id"]:
            guess = args.url.rstrip("/").rsplit("/", 1)[-1]
            print("[!] could not read the category's group id from its page "
                  "(%s); guessing %r from the URL. Pass --group-id to be sure."
                  % (context["page_state"] or "no US exit configured", guess),
                  file=sys.stderr)
            context["group_id"], context["group_id_source"] = guess, "url-guess"
    if not context["group_id"]:
        raise output_writer.UsageError("nothing to fetch: pass --group-id (e.g. sofa) or "
                         "--url with a leaf category such as "
                         "https://www.potterybarn.com/shop/furniture/sofa/")
    if not context["key"]:
        context["key"], context["key_source"] = P.FALLBACK_CONSTRUCTOR_KEY, "fallback"
        log("using the last known Constructor key (it is public, and read "
            "from a listing page whenever a US exit is configured)")
    if not context["currency"]:
        known = P.KNOWN_KEY_CURRENCY.get(context["key"])
        if known:
            context["currency"], context["currency_source"] = known, "known-key"
    return context


class HubError(RuntimeError):
    """The URL landed on a department hub, which has no grid."""


def read_listing_page(args, pool, log) -> Dict[str, Any]:
    """Fetch the leaf page over curl for its key, group id and currency."""
    import http_scraper
    session = http_scraper.build_session(pool.current, args.http_client)
    facts: Dict[str, Any] = {}
    rotate = http_scraper.make_rotator(args, pool, facts)
    result, _ = http_scraper.fetch(session, args.url, args.timeout,
                                   retries=args.retries, retry_delay=args.retry_delay,
                                   log=log, rotate=rotate)
    state = result.state
    log("listing page: %s (HTTP %s, %d bytes)" % (state, result.status, len(result.html)))
    return {"state": state, "final_url": result.final_url,
            "context": P.listing_context(result.html) if state == "listing" else {}}


def sweep(args, session, group_id, key, currency, label, pages, proxies,
          rows, seen, facts, log, sort=None, until_empty=False) -> None:
    """One pass over the listing's pages, in the API's order or `sort`.

    Appends NEW products to `rows` (deduped through `seen`) and terminates
    on data: a page that adds no new product ends the first pass. A
    RECOVERY pass (`until_empty`) runs until a page comes back empty, since
    in a re-sorted pass most pages hold only products already seen.
    """
    for page in range(1, pages + 1):
        result = None
        for attempt in range(1, args.retries + 2):
            result = browse(session, group_id, key, page, timeout=args.timeout,
                            proxies=proxies, sort=sort)
            if result.state not in ("rate_limited", "transport_error"):
                break
            wait = args.retry_delay * (2 ** (attempt - 1))
            log("  page %d attempt %d: %s (%s) — waiting %.0fs"
                % (page, attempt, result.state, result.status or result.error, wait))
            time.sleep(wait)
        state = result.state
        facts["states"].append(state)
        if state != "api":
            facts["failed_pages"].append(page)
            if state == "transport_error":
                facts["transport_error"] = True
            elif state in ("blocked", "rate_limited"):
                facts["blocked"] = True
            print("page %d: %s (HTTP %s)%s" % (page, state, result.status,
                                               " — " + result.error if result.error else ""),
                  file=sys.stderr)
            break
        payload = result.payload
        total = P.browse_total(payload)
        facts["totals_seen"].append(total)
        if page == 1:
            facts["group"] = P.browse_group(payload)
        page_rows = P.parse_browse_response(payload, currency=currency, category=label)
        for row in page_rows:
            row.page = page
        fresh = output_writer.dedupe_by_sku(page_rows, seen)
        rows.extend(fresh)
        facts["pages_completed"] += 1
        if not until_empty:
            facts["results_seen"] += len(page_rows)
            facts["repeated_results"] += len(page_rows) - len(fresh)
        log("page %d: %d results (%d new, %d total of %s reported)"
            % (page, len(page_rows), len(fresh), len(rows), total))
        if not page_rows or (not fresh and not until_empty):
            facts["listing_exhausted"] = True
            break
        if args.max_products and len(rows) >= args.max_products:
            del rows[args.max_products:]
            facts["max_products_reached"] = True
            break
        if page < pages:
            pause = rate_limit_pause(result.headers)
            if pause:
                facts["rate_limit_waits"] += 1
                log("  rate limit low (%s left) — waiting %.0fs"
                    % (result.headers.get("x-ratelimit-remaining"), pause))
            time.sleep(max(args.delay, pause))



def scrape(args) -> Tuple[List[Product], Dict[str, Any]]:
    if requests is None:
        raise output_writer.UsageError("api_scraper.py needs `requests`: pip install -r requirements.txt")
    log = (lambda m: print(m, file=sys.stderr)) if args.verbose else (lambda m: None)
    context = resolve_listing(args, log)
    group_id, key, currency = context["group_id"], context["key"], context["currency"]
    label = args.category or (P.category_from_url(args.url) if args.url else group_id)

    proxies = None
    if args.api_via_proxy:
        pool = proxy_pool.from_args(args)
        if not pool:
            raise output_writer.UsageError("--api-via-proxy needs POTTERYBARN_PROXY or --proxy")
        proxies = {"http": pool.current, "https": pool.current}

    session = requests.Session()
    pages = 10 ** 6 if args.all_pages else args.pages
    facts: Dict[str, Any] = {
        "transport": "constructor-api" + ("+proxy" if proxies else ""),
        "group_id": group_id,
        "group_id_source": context["group_id_source"],
        "key_source": context["key_source"],
        "currency": currency,
        "currency_source": context["currency_source"],
        "listing_page_state": context["page_state"],
        "pages_requested": "all" if args.all_pages else args.pages,
        "pages_completed": 0,
        "failed_pages": [],
        "states": [],
        "totals_seen": [],
        "group": None,
        "blocked": False,
        "transport_error": False,
        "rate_limit_waits": 0,
        # Results the API returned in the first sweep, and how many of them
        # were a product already returned on an earlier page. Measured
        # 2026-09-24..10-01: repeats appeared in 3 of 9 sweeps of `sofa` —
        # 1 and 4 within the first three pages on two daily canary runs, 19
        # over a full sweep once — and in none of the rest.
        "results_seen": 0,
        "repeated_results": 0,
        "listing_exhausted": False,
        "max_products_reached": False,
        "start_url": P.browse_url(group_id),
        "final_url": P.browse_url(group_id),
    }
    rows: List[Product] = []
    seen: set = set()
    sweep(args, session, group_id, key, currency, label, pages, proxies,
          rows, seen, facts, log, sort=None)
    total = facts["totals_seen"][-1] if facts["totals_seen"] else None
    facts["second_sweep_recovered"] = None
    if (args.all_pages and total is not None and len(rows) < total
            and not facts["blocked"] and not facts["transport_error"]
            and not facts["max_products_reached"]):
        # Measured 2026-09-24: one sweep of `sofa` returned 434 results but
        # only 415 distinct products — 19 repeated on a second page, so 19
        # others were never returned — and minutes later five sweeps in a
        # row closed at 434/434. A second sweep in a different, deterministic
        # order is what recovers the ones the first never saw.
        print("[!] %d distinct products of %d reported — sweeping again sorted "
              "by lowestPrice to recover the rest" % (len(rows), total), file=sys.stderr)
        before = len(rows)
        sweep(args, session, group_id, key, currency, label, pages, proxies,
              rows, seen, facts, log, sort=("lowestPrice", "ascending"),
              until_empty=True)
        facts["second_sweep_recovered"] = len(rows) - before
    facts["total_num_results"] = facts["totals_seen"][-1] if facts["totals_seen"] else None
    facts["total_moved_during_run"] = len({t for t in facts["totals_seen"] if t is not None}) > 1
    # The total is compared with what was collected ONLY when the listing
    # was read to its end. On a `--pages 3` run the first version compared
    # 71 rows with 434 and warned that products were missing from a listing
    # nobody had asked for in full.
    if rows and facts["total_num_results"] is not None and not args.max_products \
            and facts["listing_exhausted"]:
        facts["arithmetic_closes"] = len(rows) == facts["total_num_results"]
        facts["collected"] = len(rows)
        if not facts["arithmetic_closes"]:
            print("[!] collected %d products but the API reports %d for %r"
                  % (len(rows), facts["total_num_results"], group_id), file=sys.stderr)
    if facts["repeated_results"]:
        print("[!] %d of %d results repeated a product from an earlier page — the "
              "API's order shifted during the run; %d distinct products kept. A "
              "repeat means another product was skipped: use --all-pages for a "
              "complete listing (it re-sweeps to recover them)."
              % (facts["repeated_results"], facts["results_seen"], len(rows)),
              file=sys.stderr)
    return rows, facts


def _stop_reason(args, facts: Dict[str, Any]) -> str:
    if facts["transport_error"]:
        return "transport_error"
    if facts["blocked"]:
        return "blocked"
    if facts.get("max_products_reached"):
        # A deliberate cap is a SUBSET of the category, never "complete": with
        # --all-pages --max-products 10 the first version wrote 10 of 433 as
        # `complete` (audit, 2026-10-01), which a diff would then read as 423
        # delistings.
        return "max_products_reached"
    if args.all_pages and facts.get("arithmetic_closes") is False \
            and facts.get("collected", 0) < (facts.get("total_num_results") or 0):
        # Known to have missed products: partial, never complete.
        return "listing_incomplete"
    if not args.all_pages and facts["pages_completed"] >= args.pages:
        return "completed"
    return "pagination_exhausted"


def _positive_int(value):
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be >= 1, got %s" % value)
    return number


def _non_negative_int(value):
    number = int(value)
    if number < 0:
        raise argparse.ArgumentTypeError("must be >= 0, got %s" % value)
    return number


def _non_negative_float(value):
    number = float(value)
    if number < 0:
        raise argparse.ArgumentTypeError("must be >= 0, got %s" % value)
    return number


def parse_args(argv=None):
    import http_scraper
    p = argparse.ArgumentParser(
        description="Scrape a potterybarn.com category through its listing API "
                    "(Constructor.io) — one row per product.",
        epilog="The listing API needs no proxy. POTTERYBARN_PROXY is used only "
               "to read the category page itself (for its group id, key and "
               "currency), and only with --url.")
    p.add_argument("--url", default=None,
                   help="A leaf category, e.g. https://www.potterybarn.com/shop/furniture/sofa/")
    p.add_argument("--group-id", default=None,
                   help="The Constructor browse group (the page's categoryId), "
                        "e.g. sofa. Needs no proxy at all.")
    p.add_argument("--category", default=None,
                   help="Label for the rows' `category` column. Default: the "
                        "URL's /shop/ path, or the group id.")
    p.add_argument("--pages", type=_positive_int, default=1,
                   help="API pages to fetch, %d products each." % P.PAGE_SIZE)
    p.add_argument("--all-pages", action="store_true",
                   help="Page until a page adds no new product.")
    p.add_argument("--max-products", type=_positive_int, default=None)
    p.add_argument("--no-page", action="store_true",
                   help="With --url, do not fetch the category page even if a "
                        "proxy is configured; use the URL guess and the "
                        "fallback key.")
    p.add_argument("--api-via-proxy", action="store_true",
                   help="Send the API calls through the proxy too. Not needed "
                        "from the addresses measured.")
    p.add_argument("--retries", type=_non_negative_int, default=2)
    p.add_argument("--retry-delay", type=_non_negative_float, default=5.0)
    p.add_argument("--delay", type=_non_negative_float, default=1.0)
    p.add_argument("--timeout", type=_positive_int, default=45)
    p.add_argument("--format", choices=["json", "csv", "both"], default="both")
    p.add_argument("--out", default=DEFAULT_OUT)
    p.add_argument("--allow-empty", action="store_true")
    p.add_argument("--verbose", action="store_true")
    p.add_argument("--http-client", choices=["curl", "requests"],
                   default=http_scraper.default_http_client(),
                   help="Client for the CATEGORY PAGE fetch (the API always "
                        "uses requests).")
    p.add_argument("--proxy", default=None)
    p.add_argument("--proxy-file", default=None)
    p.add_argument("--proxy-rotate", choices=proxy_pool.ROTATE_MODES, default="per-run")
    p.add_argument("--proxy-shuffle", action="store_true")
    p.add_argument("--proxy-sessions", type=_positive_int, default=None)
    p.add_argument("--proxy-block-retries", type=_non_negative_int, default=3)
    args = p.parse_args(argv)
    env_config.apply(args)
    return args


def main(argv=None) -> int:
    args = parse_args(argv)
    try:
        rows, facts = scrape(args)
    except HubError as exc:
        print("[!] %s is a department HUB, not a listing — it has no grid. "
              "Pick a leaf: `python3 catalog_walk.py categories --grep <word>` "
              "lists them from the sitemap." % exc, file=sys.stderr)
        return output_writer.EXIT_NO_PRODUCTS
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001
        import http_scraper
        if isinstance(exc, http_scraper.ProxyAuthError):
            print("[!] the proxy refused the credential while reading the "
                  "category page: %s" % exc, file=sys.stderr)
            return output_writer.EXIT_REMOTE_API_ERROR
        print("run failed: %s: %s" % (type(exc).__name__,
                                      proxy_pool.mask_text(str(exc))), file=sys.stderr)
        return output_writer.EXIT_CRASH
    return finish_run(
        rows, args.out, args.format, args.allow_empty,
        blocked=facts["blocked"], transport_error=facts["transport_error"],
        stop_reason=_stop_reason(args, facts),
        pages_requested=facts["pages_completed"] if args.all_pages else args.pages,
        pages_completed=facts["pages_completed"],
        pages_failed=facts["failed_pages"],
        mode="category",
        scope={"group_id": facts["group_id"], "currency": facts["currency"],
               "max_products": args.max_products,
               "is_full_catalog": bool(args.all_pages and not args.max_products)},
        start_url=facts["start_url"], final_url=facts["final_url"],
        extra=facts,
    )


if __name__ == "__main__":
    sys.exit(main())
