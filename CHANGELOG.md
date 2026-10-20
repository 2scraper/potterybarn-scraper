# Changelog

Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Versioning
follows SemVer as closely as a CLI toolkit can: a patch release means fixes,
not a promise that every flag and default is frozen. Anything that changes
behaviour for an existing user is called out at the top of its release.

## [Unreleased]

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

### Added

- `--load-media` on the browser engines.

### Changed

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
