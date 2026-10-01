# Troubleshooting

By symptom. Every "measured" below is 2026-09-24 unless it says otherwise.
Start with the sidecar: `<out>.latest_attempt.meta.json` is written on every
run and records the mode, each page's state, the stop reason and the exit
used (masked).

---

## Exit 3 on every product page

The site refused. In order of likelihood:

1. **No proxy, or not a US one.** From a non-US address every product page is
   the branded 403, and a GB exit is refused too. `python3 env_config.py`
   shows whether `POTTERYBARN_PROXY` is picked up.
2. **Not curl.** `python-requests` got the page on 0 of 18 attempts on
   2026-09-24 and 2 of 4 on 2026-10-01, where curl got it nearly every time.
   The sidecar's `transport` says `http-curl` or `http-requests`. If it says
   `requests`, curl is not on PATH.
3. **A local browser.** A local Chromium was refused on every product-page
   trial (0 of 19). Use `http_scraper.py`, or the Scraping Browser API.
4. **A scored exit.** `states` shows `blocked` or `geo_redirect` after
   several rotations. Raise `--proxy-block-retries`, or wait.

Exit 3 is never a captcha on this site. No challenge has been met anywhere,
so a solver key will not change it.

## `geo_redirect` in the states

The exit is not in the US, or has drifted out of it. A pinned 2Captcha
session was seen moving from El Paso to a Frankfurt AWS address minutes
into its life (recon, 2026-09-23). A redirect to potterybarn.co.uk is never
followed, and it rotates to a freshly minted session. If every rotation
lands there, check that the proxy login carries `-region-us`.

## Exit 5

Our own plumbing failed, and the site never answered:

* **`the proxy refused the credential (HTTP 407)`**: the proxy, not the site.
  It is not retried or rotated. It has been measured to be *transient* on
  this vendor (homedepot-scraper, 2026-09-22), so retry later before
  replacing anything.
* **`could not connect over CDP: ... 500 Internal Server Error`**: the
  Scraping Browser profile is held, almost always by a previous connection
  that ended abnormally. Measured: a navigation timeout left it answering
  500 twenty-eight minutes later. **Stop touching it and wait.** Do not
  "check" it with `GET /json/version`: that call takes the lock itself.
* **`500 Internal Server Error` on connect with `proxy_error` in the call
  log**: the profile's own exit is failing at the vendor. That is not a lock.
  A residential exit attached per connection (`tools/browser_profile_client.py
  connection`) worked on the same profile at the same time (2026-10-01).
* **A timeout or a TLS error**: an exit that failed at the transport level.
  It is retried on the same exit, then reported.

## A category returns 0 products (exit 4)

* **It is a hub.** `/shop/furniture/` has no grid, and a guessed
  `/shop/furniture/sofas/` 301s to it. `catalog_walk.py categories
  --leaf-only --grep sofa` lists the real leaves.
* **It is a department URL with no page read.** `/shop/<department>/` is
  refused as a hub even without a proxy, rather than guessed into a broad API
  group (`furniture` returned 8,278 results).
* **The group id is wrong.** Without a US exit, `api_scraper.py --url` uses
  the URL's last path segment and records `group_id_source: url-guess`. That
  guess matched the page's own `categoryId` on 15 of 15 categories sampled.
  If it does not for yours, pass `--group-id`, or set `POTTERYBARN_PROXY` so
  the page is read.

## A category stops early, or the count differs from the API's total

`transport_facts.arithmetic_closes` is false when the collected rows differ
from `total_num_results`. The API's page order has been measured repeating
products across pages, and so skipping others (434 results, 415 distinct).
With `--all-pages` a second, price-sorted sweep recovers them
(`second_sweep_recovered`). A listing still short afterwards exits 6 with
`stop_reason: listing_incomplete`: re-run it. If `total_moved_during_run` is
true, the catalogue changed under you.

## Two listing rows point at the same product

A result can be a sub-group of a product, a slice by finish (`?subGroupId=`
in `url`). Each slice is its own row with its own `sku`; they share
`product_id`. That was 15 of 144 results over three categories.

## Exit 3 from `api_scraper.py`

A 429 from the listing API, which has never been observed. At
`x-ratelimit-remaining: 0` the API kept answering 200 with full results for
194 calls (2026-10-01), and the scraper slows to one page per 5 seconds
there instead of stopping. The budget is per client address. If a 429 does
appear, raise `--delay` or use another address.

## A browser run is slow on its first listing page, or slow everywhere

Below 10 remaining the listing paces itself at 5 seconds a page; that is the
rate-limit courtesy, not a hang.

## A browser product run says `empty` on a page that clearly loaded

Fixed in this release, and worth knowing about if you write your own: the
storefront deletes its `window.__INITIAL_STATE__` script tag from the DOM
after hydration, so the rendered document of a served product page holds no
state. The engines read the navigation's response body (`state_source:
response-body`), or for Selenium the live JS object (`window-state`). A
`dom` source on an empty result means neither was available.

## A product page gives thousands of rows

Correct. One row per SKU, and a configurable sofa has thousands (York:
1,367; PB Comfort: 4,843).

## `could not decode subsetsCompressedValue` / `brotli package is not installed`

A guided product's SKUs exist only inside a Brotli-compressed value.
`pip install -r requirements.txt` installs `Brotli`. Without it those pages
fail loudly, and are never reported as zero SKUs.

## `the SKU list may be incomplete`

`rows − NLA` did not equal the page's `skuCount`. On every page measured
it closed exactly. If it stops closing, the page's shape has changed: file
a site-change issue with the product URL.

## The price looks 100× wrong

It shouldn't: every price is in dollars as the site states it (a sofa at
1679, not 16.79), and no factor is applied. If you see cents, the listing
API's shape changed.

## Selenium says the proxy credential was stripped, then exits 5

Chrome's `--proxy-server` cannot carry a password, so Selenium cannot use an
authenticated proxy. Chrome then shows the proxy's own refusal, an empty
39-byte document, which is reported as `transport_error` and exit 5: the
site was never reached. Use `playwright_scraper.py`, or `http_scraper.py`,
which is the recommended path for product pages anyway. Selenium reads
listings fine, since those need no proxy.

## pyppeteer: `Browser closed unexpectedly`, or `Network.setRequestInterception wasn't found`

* On Apple silicon pyppeteer downloads an x86_64 Chromium that did not start
  under Rosetta within its launch window. Pass `--browser-path` with the
  system Chrome (`/Applications/Google Chrome.app/Contents/MacOS/Google Chrome`).
* With an authenticated proxy on a current Chrome, `page.authenticate` fails
  because Chrome 154 dropped the CDP method it uses. That is exit 5 with the
  reason. Over `--cdp-endpoint` the password travels in the WebSocket URL and
  pyppeteer works.

## The row count is right but a column is empty

`--dump-html page.html` writes the exact bytes of every page, including on
success. Check whether the field was in the page before calling it a
parsing bug. `rating` and `review_count` do not exist here by design: the
data isn't in the page.
