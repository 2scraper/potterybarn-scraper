# Changelog

Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Versioning
follows SemVer as closely as a CLI toolkit can: a patch release means fixes,
not a promise that every flag and default is frozen. Anything that changes
behaviour for an existing user is called out at the top of its release.

## [Unreleased]

### Added

- `.env` values holding typographic quotes (`“ ” ‘ ’ „ « »`, which macOS
  TextEdit types in place of `"` and `'`) are warned about on every load,
  by key and line number, never by value. Only ASCII quotes are stripped, so
  `POTTERYBARN_PROXY=“http://…”` was read with the curly quotes and failed
  far from its cause. The value is still loaded as written; nothing is
  rewritten. See TROUBLESHOOTING.

- `http_scraper.py --proxy-rotate per-page` now does what it says: every
  product after the first is fetched from a fresh exit (a newly minted
  session on a 2Captcha gateway, the next `--proxy-file` entry otherwise),
  without spending the run-wide `--proxy-block-retries` budget. Counted in
  `transport_facts.page_rotations`. On one plain (non-gateway) exit the run
  says there is nothing to rotate to. For `--urls-file` / `--from-listing`
  batches it rules out the refused-exit failure below; it does not beat a
  refusal of every exit (2026-11-26 20:10: 15 fresh sessions, all 403, run
  `32d49c199a14`).
- An `empty` product page now says what it most often means: check the URL
  is a live product (a made-up slug was answered HTTP 200 with no product
  state, run `8481328e15a9`). The exit code is unchanged (5).

### Changed

- **`--proxy-rotate per-page` is refused (exit 2) by `api_scraper.py` and the
  browser engines** instead of being accepted and ignored. All three CLIs
  took the flag and nothing read it (`ProxyPool.rotates_per_page()` had no
  caller). A browser run keeps one browser and one exit, and the listing
  script sends at most one request through the proxy, so neither has a page
  to rotate on.
- `--proxy-block-retries` help now says the budget is shared by the whole
  run, not per product, which is how it has always worked.

### Fixed

- `--proxy-rotate per-page` was silently ignored everywhere, and a single
  `--proxy` was forced to `per-run` whatever was asked. Found after a
  48-product batch (run `746c97ee08e5`, 2026-11-26) spent the 3-rotation
  budget by product 10 and fetched products 11–48 from the refused exit:
  9 fetched, 37 × 403, 2 transport errors. Whether fresh exits would have
  got those 37 is not measured: the site was refusing every exit later
  that day.
- Tests only: `smoke_test.py` no longer reads the developer's real `.env`.
  With `POTTERYBARN_PROXY` and `POTTERYBARN_CDP_ENDPOINT` set there, 17
  proxy-budget and pre-check checks failed locally while CI (no `.env`) was
  green, and the loaded values stayed in `os.environ` for every later test.
  The suite now points `env_config.DEFAULT_ENV_PATH` (new: the one place the
  default location is decided) at a file that does not exist, drops the
  credential variables from its environment, and arms an audit hook that
  makes any `open()` of the real `.env` a `PermissionError`. Nothing changes
  for the scripts or for `python3 env_config.py`.

## [1.2.0] — 2026-11-24

> **Behaviour changes.**
> - The browser engines (`playwright_scraper.py`, `puppeteer_scraper.py`,
>   `selenium_scraper.py`) send ONE `HEAD` for the site's `robots.txt`
>   through `--proxy` / `--proxy-file` before starting a local browser. A
>   gateway `407` or `402` now ends the run there with exit 5 and
>   `stop_reason: proxy_auth_failed` in `<out>.latest_attempt.meta.json`,
>   instead of starting a browser on a refused proxy.
> - Behind a local proxy, those engines now abort images, video/audio and
>   web fonts. `--load-media` restores them. Nothing changes with
>   `--cdp-endpoint` or with no proxy.
> - Every transport now stops once about 10 MB have gone through a local
>   proxy (`--proxy-budget-mb`, `0` = unlimited): the rows read so far are
>   kept and the run exits 6 (5 with none), `stop_reason:
>   proxy_budget_exceeded`. `catalog_walk.py --via-proxy` exits 6 with the
>   targets found so far, 5 with none. Nothing is limited over
>   `--cdp-endpoint`, with no proxy, or for `api_scraper.py`'s direct API
>   calls.
> - `http_scraper.py`: a refused product no longer moves to a fresh exit
>   after its LAST attempt. In a multi-URL run the next product therefore
>   starts on the exit the previous one was refused on, and rotates from
>   there. A Mac's system curl (LibreSSL) is now warned about on every run:
>   it is refused on product pages; set `POTTERYBARN_CURL` (see Added).

### Added

- `--fingerprint`, `--fp-tags`, `--fp-country`, `--fp-refresh` on
  `playwright_scraper.py` (local browser only). Fetches a profile from the
  2Captcha Fingerprint API with `TWOCAPTCHA_KEY` and applies it: user agent
  with matching client hints (`Sec-CH-UA*`, `navigator.userAgentData`),
  locale and `Accept-Language`, timezone, screen, window and scale factor,
  navigator values and WebGL strings. The values are read back from the page
  after the first navigation and recorded in `transport_facts.fingerprint`.
  Refused with a typed `--cdp-endpoint` (exit 2); a
  `POTTERYBARN_CDP_ENDPOINT` from `.env` is ignored for the run. A key
  without a fingerprint plan exits 5 before any page is fetched. Selenium and
  pyppeteer refuse the flag (exit 2). `fingerprint_client.py` now reads the
  fields where a real response keeps them (WebGL strings in
  `webgl.glParameters`, client hints from `userAgent.*`); no engine used it
  before, so no earlier run was affected.
- `--load-media` on the browser engines.
- Proxy traffic meter (`proxy_meter.py`) on every transport, active only
  behind a local proxy: curl's own size counters, `requests` request/response
  sizes, Playwright `request.sizes()`, and CDP
  `Network.loadingFinished.encodedDataLength` plus request sizes for pyppeteer
  and Selenium. An estimate of the tunnelled payload (TLS/`CONNECT` overhead
  not included). Both sidecars record `proxy_bytes`, `proxy_budget_bytes` and
  `proxy_budget_exceeded`; a proxied run ends with one `Proxy traffic: X.X MB
  of Y MB budget` line. New flag `--proxy-budget-mb` (default 10) on every
  CLI.
- CI only: the canary's product run passes `--proxy-budget-mb 10`, writes
  `**live (product)**: proxy traffic X.X MB of 10 MB` to the job summary and
  fails with `::error::stopped: proxy budget 10 MB exceeded` when the run hit
  it. The engine jobs run the meter against a stub proxy in a real browser.
- `--curl-path` (and `POTTERYBARN_CURL` in `.env`) on `http_scraper.py` and
  `api_scraper.py`: which curl binary to run. The sidecar records it as
  `transport_facts.curl` (`path`, `version`, `tls`), and a curl whose default
  TLS library is LibreSSL or SecureTransport is warned about on every run.
  Measured 2026-11-24 on macOS: `/usr/bin/curl` 8.7.1 (LibreSSL 3.3.6) got
  the 403 page on 8 of 8 US residential exits, Homebrew curl 8.0.1
  (OpenSSL 1.1.1t) got the product page through the same exits and completed
  3 of 3 runs, field-for-field identical to the Scraping Browser API. With
  an OpenSSL curl, `api_scraper.py --url` also reads the category page
  again (`group_id_source: page`, `key_source: page`) instead of guessing.

### Changed

- CI only: the offline-checks job runs on `ubuntu-24.04` instead of `ubuntu-latest`, which moved to Ubuntu 26.04, where `actions/setup-python` has no Python 3.9 build. 3.9 stays the supported floor.
- Docker: the image installs `libatomic1` before Playwright. Playwright's Node driver needs it to run `playwright install --with-deps` at all, and `python:3.12-slim` does not ship it, so the image build failed (2026-11-24).
- A proxy `402` (traffic/balance exhausted) is handled like a `407` in
  `http_scraper.py`, `api_scraper.py` and `catalog_walk.py --via-proxy`:
  exit 5, never retried or rotated. `catalog_walk.py` reported a proxy `407`
  on an `http://` request as blocked (exit 3); it is now exit 5.

- When the proxy refuses its credential (HTTP 407), `http_scraper.py` and
  `api_scraper.py` now write `<out>.latest_attempt.meta.json` with
  `status: failed` and `stop_reason: proxy_auth_failed`. They still exit 5
  and leave the data and `<out>.meta.json` untouched. Before this change, a
  407 run wrote no sidecar at all, and the canary step that reads the sidecar
  crashed with `FileNotFoundError` and no `::error::` line. The canary now
  also reports a missing sidecar as an error.
- CI only: the whole canary, free and paid jobs alike, now runs weekly (Mondays 06:23 UTC) and on a manual dispatch; the daily schedule is removed.
- `http_scraper.py`: a product URL that is served but does not classify as a product page now records the page's shape in `transport_facts.unparsed_pages`: size, whether `window.__INITIAL_STATE__` was found and parsed, its top-level key names, and the `productDetails` key names. Values are not recorded. On 2026-10-20 the canary's product page came back through `POTTERYBARN_PROXY` as 536 KB with HTTP 200, was classified `empty` (exit 5), and nothing in the sidecar or the artefact could tell a changed page shape from a different page. The canary now also uploads that page (`--dump-html`). Exit codes are unchanged.

### Fixed

- A browser engine whose start fails after the browser is already up (for
  example a refused fingerprint fetch) now closes that browser before
  exiting 5.
- The README's "Related scrapers" list is complete again. The offline
  suite's check that no other repository's text drifted into this one now
  skips that section only, which names other repositories on purpose; every
  other part of the README and every other shipped file is still checked.
- Proxy traffic meter: Selenium and Puppeteer no longer count requests that never leave the browser. Chrome's new-tab page loads `chrome://` scripts (about 2.8 MB, seen through a stub proxy) that were reported as finished loads and added to `proxy_bytes`, enough to bring a run noticeably closer to `--proxy-budget-mb`. Only `http(s)` requests and responses are counted now.
- `http_scraper.py` no longer rotates the exit after the LAST attempt on a
  refused page. With the defaults (`--retries 2`, `--proxy-block-retries 3`)
  a blocked product sent 3 requests but minted 3 fresh sessions, so the
  sidecar's `transport_facts.exit` named a session that never sent a request
  and `rotations: 3` read as four exits tried (measured 2026-11-24, run
  `7967dd2e4ddd`). The unused rotation also spent one unit of the run-wide
  `--proxy-block-retries` budget. Side effect in a multi-URL run: the next
  product now starts on the exit the previous one was refused on, and rotates
  from there if the budget allows, instead of starting on a fresh exit.
  Covered by `test_fetch_does_not_rotate_after_its_last_attempt`.

## [1.1.0] — 2026-10-16

> **Behaviour changes.**
> - A run with **zero rows** whose content was never obtained now exits
>   **5**, not 4: a page that failed with an unexpected answer, a served
>   product page with no product state, or a response that states results
>   which did not parse. Exit 4 now means the site itself answered "nothing
>   here" (a listing stating 0 results, a hub).
> - `api_scraper.py`: a later page answering an unexpected HTTP status (a
>   400, a 404) or a 200 that is not the listing shape now makes the run
>   **partial, exit 6** (`stop_reason: page_failed`). It used to end the run
>   as `complete`, exit 0, with the failed page listed in the same sidecar.
> - A browse response that states its results and parses to none is
>   `stop_reason: parser_found_nothing` (exit 5, or 6 with earlier rows),
>   never the end of the listing. Applies to `api_scraper.py` and the browser
>   engines' `--mode category`.
> - Output files (JSON, CSV, both sidecars) were created **0600**. A new file
>   now gets `0666` minus your umask, and an existing file keeps its mode.
> - CSV cells starting `=`, `+`, `-`, `@`, tab, CR or LF are prefixed with an
>   apostrophe (CSV only; JSON unchanged; count in `csv_cells_escaped`).

### Fixed

- `api_scraper.py --group-id` no longer fails with exit 2 when
  `POTTERYBARN_URL` in `.env` holds a product page. The environment's URL is
  used only when neither `--group-id` nor `--url` is typed, and only if it is
  a category page; `.env.example` ships it commented out.
- `tools/scan_secrets.py --worktree` also reads untracked files and
  gitignored `.env.*` backups (by content), skips virtualenvs by their
  `pyvenv.cfg`, prints how many files it read, and fails when it read none.
- `.gitignore` covers output directories by kind (`out/`, `tmp/`, `dumps/`,
  …), `*.pageN` dumps and raw HTML at the repository root.
- The daily canary no longer goes green with product pages untested: without
  the `POTTERYBARN_PROXY` secret the product-page half fails with an error
  naming it, and both halves now also check the exit code (and the product
  half its rows).
- Comments that cited internal documents now state the rule in place, and
  the suite fails if such a citation comes back.

## [1.0.0] — 2026-10-01

First release. Measurements were made on 2026-09-24 and re-made on
2026-10-01; each one below says which.

### Transports

- **`api_scraper.py`: category listings through the site's own listing
  API** (Constructor.io's browse API on `ac.cnstrc.com`, with the
  storefront's public key). It needs no proxy, browser or key, and answers
  a residential address in Moscow and a GitHub Actions runner alike. The
  whole sofa category: 434 of 434 (09-24) and 433 of 433 (10-01). The API
  host is a third party that potterybarn.com's robots.txt does not govern;
  the README says so in its own section.
- **Unstable page order is recovered, not hidden.** The API sometimes
  repeats a product on a later page and so skips another: 415 distinct of
  434 (09-24), 403 of 433 (10-01), and 1 and 4 repeats on two daily canary
  runs. With `--all-pages` a second, price-sorted sweep recovers the rest
  (all 30 on 10-01). A listing still short is reported partial, never
  complete. A `--pages N` run reports `repeated_results` and is not compared
  with the category total.
- **Rate limit, measured to zero** (10-01): `x-ratelimit-remaining` reached 0
  after 240 uncached calls, and 194 further calls at zero all answered 200
  with full results. The budget is per client address. The scraper slows to
  one page per 5 seconds below 10 remaining, and does not stop.
- **`http_scraper.py`: product pages over system curl**, one row per SKU.
  On fresh US exits: curl 29 of 31 (09-24) and 4 of 4 (10-01);
  `python-requests` 0 of 18 and 2 of 4; a local Chromium 0 of 15 and 0 of 4.
  The proxy reaches curl on stdin, never argv. Redirects are followed only
  inside www.potterybarn.com, and a hop to potterybarn.co.uk is a refusal
  that rotates to a freshly minted session.
- **Browser engines**: `playwright_scraper.py`, `puppeteer_scraper.py`,
  `selenium_scraper.py`, sharing one `browser_bridge.py`, all three run live.
  Listings are identical to `api_scraper.py` field for field through all
  three (72 of 72). Product pages over the Scraping Browser API: refused with
  HTTP 403 on 09-24, served on 14 of 14 sessions on 10-01 and identical to
  curl field for field (4 of 4 and 1,380 of 1,380 SKUs). `Captcha.setAutoSolve`
  was accepted every time, with zero Captcha events. One connection per run,
  closed in `finally`, never preceded by `GET /json/version`.
- **Navigation never waits for the page to render.** Playwright returns on
  `commit` and reads the body; `domcontentloaded` had timed out in 3 of 20
  instrumented runs, and a timeout can hold the Scraping Browser profile. On
  the new wait: 0 of 10. Selenium uses `page_load_strategy = "eager"`.
- **Browsers read the response body, not the DOM.** The storefront deletes
  its state `<script>` on hydration, so a served product page's DOM reads
  as empty. The engines read the navigation's body first and fall back to
  the live `window.__INITIAL_STATE__` object, recording `state_source`.
- `--browser-path` for every engine: pyppeteer's downloaded Chromium is an
  x86_64 build that did not start under Rosetta. Measured engine limits are
  reported as exit 5 with the reason: Selenium cannot authenticate a proxy
  (Chrome then shows an empty 39-byte document) and cannot use an
  authenticated CDP endpoint, and pyppeteer cannot authenticate a proxy on
  Chrome 154 (`Network.setRequestInterception` is gone).
- **`catalog_walk.py`**: the category and product sitemaps (795 and 16,966
  URLs on 09-24), gzip-aware, robots-filtered, no proxy needed. The files
  refuse the default `python-requests` user agent, so the browser header set
  is sent.

### Parsing

- Product pages are read from `window.__INITIAL_STATE__.product.productDetails`
  with a string-aware scanner. There is no `Product` JSON-LD on this site.
- **Guided products' SKUs are decoded from `subsetsCompressedValue`**
  (base64 Brotli). On a guided product, which covers every sofa measured,
  the served `subsets` list is empty. Each product's SKU count is checked
  against the page's own `skuCount` (rows − NLA).
- `Product` (listing) and `SkuRow` (product page) share the family's
  ten-column prefix, and both carry `product_id` for the join. A listing
  result can be a sub-group of a product, a slice by finish (15 of 144
  results): `sku` is the result's id, `product_id` the URL path's slug, and
  `sub_group_id` the slice. `--from-listing` fetches each product once.
- Listing price ranges equal the product page's own aggregate range.
- Currency is read from what a page states. The listing API states none, so
  a listing row's currency comes from the listing page, or from the currency
  recorded for that exact key.
- Exit codes follow the family contract everywhere: a refused URL (another
  host, a robots-disallowed path, the wrong page kind) is exit 2, never the
  crash code 1, and a top-level `/shop/<department>/` is a hub (exit 4) even
  when its page cannot be read, instead of being guessed into a broad API
  group (`furniture`: 8,278 results).
- The branded 403 is recognised by status and its own text. An empty document
  is a transport failure, not a refusal. The Scraping Browser's auto-solve
  extension scripts are stripped before classification, because on its own
  403 page they name `cf-turnstile` and would otherwise route a refusal to
  the paid solver. The page's reCAPTCHA sitekey (v2 checkbox, by anchor
  probe) is ignored unless a widget is actually rendered.

### Completeness is never claimed for a subset

- A `--max-products` cap stops with `max_products_reached` and is a partial
  run (exit 6), never `complete`. The sidecar's scope records the cap and
  `is_full_catalog`, and the recovery sweep does not run after a deliberate cap.
- A product whose SKU count disagrees with the page's own `skuCount` makes
  the run partial (`sku_count_mismatch`; `--accept-count-mismatch` to
  override). The counting rule is "displayable, or not NLA". It matched on
  40 of 40 product pages (20 fitted, 20 held out), where the first rule,
  "rows − NLA", missed 3 of 20.
- `diff_runs.py` fails closed: a missing or unreadable sidecar, or two runs
  differing in mode, schema, source, listing group or currency, is refused
  without `--force`.
- `potterybarn-browser` on a base install (no `[playwright]` extra) says what
  to install and exits 2, instead of a traceback on `--help`.

### Safety

- `tools/scan_secrets.py` with `--staged`, `--worktree`, `--range` and
  `--history`, run by versioned `pre-commit` and `pre-push` hooks and by CI.
  `--range` splits the pre-push hook's `<sha> --not --remotes` into separate
  arguments and fails closed on any git error. Without that, a new branch's
  first push is never scanned.
- robots.txt is embedded as well as committed (`robots.snapshot.txt`), so an
  installed wheel cannot lose it. The matcher fails closed on an empty rule
  set.
- CI actions on their Node 24 majors, read-only workflow permissions, and
  Dependabot for pip, actions and the Docker base. The image runs as an
  unprivileged user.

### Not included, and why

- **No Scraper API transport.** The 2Captcha Scraper API answered HTTP 200
  for two different product URLs with the same prerendered generic page and
  no product in it (3 of 3 calls, 09-24).
- **No rating or review columns.** Zero rating keys appeared in 3 product
  pages and 24 listing results.
