# potterybarn-scraper

Scrapes Pottery Barn (US) category listings and product pages: every product
in a category with its price range, and every SKU of a product with its own
price, markdown, stock state and options. JSON or CSV, with a run-metadata
sidecar that says whether the result is complete.

The listing comes from **the site's own listing API** and needs no proxy, no
browser and no key. Product pages come from potterybarn.com over **plain
curl** through a US exit. A browser path (**Playwright**, **Selenium**,
**pyppeteer**, or the **Scraping Browser API** over CDP) exists as the
fallback and the parity check. On this site it measured weaker than curl,
and this README says by how much.

[![release](https://img.shields.io/github/v/release/2scraper/potterybarn-scraper?sort=semver)](https://github.com/2scraper/potterybarn-scraper/releases)
[![tests](https://github.com/2scraper/potterybarn-scraper/actions/workflows/tests.yml/badge.svg)](https://github.com/2scraper/potterybarn-scraper/actions/workflows/tests.yml)
[![canary](https://github.com/2scraper/potterybarn-scraper/actions/workflows/canary.yml/badge.svg)](https://github.com/2scraper/potterybarn-scraper/actions/workflows/canary.yml)
[![Python](https://img.shields.io/badge/python-3.9%2B-blue?logo=python&logoColor=white)](https://www.python.org/)
[![licence](https://img.shields.io/badge/licence-MIT-lightgrey)](LICENSE)
[![engines](https://img.shields.io/badge/engines-API%20%7C%20curl%20%7C%20Playwright%20%7C%20Selenium%20%7C%20pyppeteer%20%7C%20CDP-informational)](#engines)
[![listings need no account](https://img.shields.io/badge/listings-no%20proxy%2C%20no%20key-brightgreen)](#what-needs-what)

---

## What needs what

Everything here was measured on **2026-09-24** and re-measured on
**2026-10-01** where it says so, from a residential address in Moscow and
through 2Captcha residential US exits.

| You want | Run | Needs |
|---|---|---|
| Every product in a category, with price ranges | `api_scraper.py` | **nothing**: no proxy, no browser, no key |
| Every SKU of a product: price, markdown, stock, options | `http_scraper.py` | a **US** exit, **curl**, and the browser header set (sent for you) |
| Every category URL, every product URL | `catalog_walk.py` | nothing |
| A browser, for parity or because you must | `playwright_scraper.py` | the Scraping Browser API for product pages (a local browser is refused) |

**The listing does not come from potterybarn.com at all.** A leaf category
page's served HTML contains zero product links. Its grid is filled client-side
by a call to Constructor.io's browse API, using the storefront's public
key, and `api_scraper.py` makes that same call. From Moscow, with no proxy
and no special headers, the whole sofa category came back as **434 rows over
19 pages**, matching the API's own `total_num_results: 434`.

**Product pages refuse on three axes at once** (a rule of this scraper family):

1. **Address.** A non-US address gets the branded 403 ("Pottery Barn: 403 -
   Restricted Access"), and `/robots.txt` is redirected to
   **potterybarn.co.uk**, a different store in GBP. A GB exit is refused on
   every page (recon, 2026-09-24).
2. **Request shape.** From the same US exits, a bare user agent got 403 on
   3 of 3 and the full browser header set got 200 on 3 of 3 (recon,
   2026-09-24).
3. **The client itself.** Every trial below minted one fresh US exit and put
   each client through *that same exit*, back to back:

| Client, product page | 2026-09-24 | 2026-10-01 |
|---|---|---|
| system `curl`, HTTP/2 | **29 of 31** (2 errors at the exit, 0 refused) | **4 of 4** |
| `python-requests`, same headers | **0 of 18** (15 refused, 3 errors) | **2 of 4** (2 refused) |
| local Playwright Chromium | **0 of 15** (12 refused, 3 errors; headless and headful) | **0 of 4** (4 refused) |

A week apart, `requests` went from never to sometimes, so treat this axis as
something the site scores and can re-score, not a fixed rule. A local
Chromium was refused every time, even on `/`.
**Which part of the handshake is scored was not isolated**, and this README
does not claim to know. `http_scraper.py` uses the client that was measured
to work: `--http-client curl` is the default wherever curl is on PATH.

---

## Install

```bash
git clone https://github.com/2scraper/potterybarn-scraper
cd potterybarn-scraper
pip install -r requirements.txt      # beautifulsoup4, requests, Brotli

# only if you want a browser, and then ONE engine per virtualenv
pip install -r requirements-playwright.txt && playwright install chromium
```

`Brotli` is not optional. A configurable product (every sofa measured) ships
its SKUs Brotli-compressed inside the page. Without the package those pages
fail loudly instead of returning zero rows.

### Credentials go in `.env`, never on the command line

```bash
cp .env.example .env          # POTTERYBARN_PROXY for product pages
python3 env_config.py         # prints what was picked up, without secrets
tools/install_hooks.sh        # refuse to commit or push a credential
```

`ps` reads argv, and argv lands in shell history and CI logs. **Install the
hooks.** They are the only guard that runs while a secret is still private.
By the time CI runs, the objects are on a server, and deleting the branch
afterwards does not remove them: GitHub serves them by SHA. A pushed secret is
a disclosed secret, so rotate it.

---

## Usage

```bash
# A whole category, no proxy. --group-id is the page's categoryId.
python3 api_scraper.py --group-id sofa --all-pages --out sofas

# The same, from its URL. With POTTERYBARN_PROXY set, the page itself is read
# for its group id, key and currency; without it, the URL's last segment is
# used and recorded as a guess.
python3 api_scraper.py --url https://www.potterybarn.com/shop/furniture/sofa/ --pages 3

# Every SKU of two products
python3 http_scraper.py \
  --url https://www.potterybarn.com/products/delaney-marble-end-table/ \
  --url https://www.potterybarn.com/products/york-slope-arm-deep-slipcovered-sofa-collection/ \
  --out skus

# Every SKU of every product a listing run found
python3 http_scraper.py --from-listing sofas.json --out sofa_skus

# Find targets without crawling
python3 catalog_walk.py categories --leaf-only --grep sofa
python3 catalog_walk.py products --no-open-box --grep sectional --out-file targets.txt
python3 http_scraper.py --urls-file targets.txt --out sectionals

# Through a browser: the listing via the API, or one product page
python3 playwright_scraper.py --mode category --group-id sofa --pages 2
python3 playwright_scraper.py --mode product \
  --url https://www.potterybarn.com/products/delaney-marble-end-table/
```

### Pagination

`page=<n>`, 24 results a page, the same call the storefront makes. A run
stops when a page adds no new product id. `total_num_results` is recorded and
checked against the row count afterwards (`arithmetic_closes` in the
sidecar), but it is **never** used to decide when to stop.

**The API's page order is not always stable, and that loses products.** A
product that repeats on a later page means another one was never returned.
Deduping hides the repeat, not the gap. Seen so far:

| When | Run | Repeats |
|---|---|---|
| 2026-09-24 | full sweep of `sofa` | 434 results, **415 distinct** (19 repeats) |
| 2026-09-26 | daily canary, 3 pages | 1 |
| 2026-09-28 | daily canary, 3 pages | 4 |
| 2026-10-01 | full sweep of `sofa` | 433 results, **403 distinct** (30 repeats) |

Other sweeps the same days closed exactly, including five in a row on
2026-09-24 in relevance and price order at 24, 100 and 200 per page, and 5
of the 7 daily canaries. So with `--all-pages`, when the distinct count falls
short of the total, `api_scraper.py` runs a **second sweep sorted by
`lowestPrice`** and adds what the first missed (`second_sweep_recovered` in
the sidecar). On 2026-10-01 that recovered all 30 and the listing closed at
433/433. A listing still short afterwards is reported **partial**
(`stop_reason: listing_incomplete`, exit 6), never complete. A `--pages N`
run reports its repeats (`repeated_results`) and is not compared with the
category total, because it never asked for all of it.

`total_num_results` does move: sofas were 434 on 2026-09-24 and 433 on
2026-10-01.

**Rate limit.** `x-ratelimit-limit: 201`, and `x-ratelimit-remaining` falls
by about 0.8 per *uncached* call. Repeating an identical URL is served from
the API's cache (`max-age=60`) and costs nothing: 400 identical calls never
pushed it below 173. Measured 2026-10-01 with unique URLs, it reached **0
after 240 calls in 159 seconds, and then nothing happened**: 194 more calls
at zero all answered 200 with full results, with no 429, no `Retry-After`
and no truncated page. A full category scraped at zero came back complete,
433 of 433. The budget is **per client address**: the same key through
another address read `remaining: 200` at the same moment. So `api_scraper.py`
does not stop at zero. Below 10 it slows to one page every 5 seconds, and a
429, never observed, is still treated as a refusal (exit 3), never as an
empty category.

---

## What you get

**Two row shapes, chosen by `--mode`, and that is a decision, not an
accident.**

| Mode | Row | One row per | Its `sku` is |
|---|---|---|---|
| `category` (`api_scraper.py`) | `Product` | listing result | the product slug, or a sub-group's id |
| `product` (`http_scraper.py`) | `SkuRow` | purchasable SKU | the SKU id |

A listing result is a product with a price *range*. A product page lists
each SKU with its own price. The alternative, always one row per SKU, was
measured before it was rejected. **One sofa has 4,843 SKUs** (PB Comfort
Modern Square Arm), so the 434-product sofa category alone would be on the
order of a million rows and 434 proxied page fetches, where the listing
takes 19 API calls from anywhere. So the mode picks the grain, the sidecar
records it, and `diff_runs.py` refuses to compare across modes.

**Both rows carry `product_id`.** Join a listing row to its SKU rows on
`product_id`, never on `sku`. It matters: a listing result is sometimes a
**sub-group** of a product, a slice by finish, rather than the product. That
was 15 of 144 results over three categories. "Aldon Dining Bench" appears
twice, as `aldon-dining-bench-SPAF-finish-russet-oak-wood-finish` and
`...-remainder`, both at `/products/aldon-dining-bench/?subGroupId=...`. Such
a row's `sku` is the slice's id, `sub_group_id` names it, and `product_id`
is the slug in the URL's path. `--from-listing` fetches each product page
once. The listing's lead SKU is `leader_sku`. It is
never written into `sku`, because a SKU id standing in for a product id is
how two questions get one wrong answer.

```
Product  source scraped_at url sku title image_url price currency category price_source
         product_id sub_group_id price_max original_price original_price_max max_discount_pct
         leader_sku pip_type price_type flags page row_index

SkuRow   source scraped_at url sku title image_url price currency category price_source
         product_id product_title original_price discount_pct availability
         back_ordered_date sellable options color width_in depth_in height_in
         subset pip_type category_hierarchy page row_index
```

The first ten columns are the family prefix, identical and in the same
order in both. One real listing row, from `sample_output.json`:

```json
{
  "url": "https://www.potterybarn.com/products/york-slope-arm-deep-slipcovered-sofa-collection/",
  "sku": "york-slope-arm-deep-slipcovered-sofa-collection",
  "title": "York Slope Arm Deep Seat Slipcovered Sofa (60\"-108\")",
  "price": 1679.0,
  "price_max": 4199.0,
  "original_price": 1999.0,
  "max_discount_pct": 20.0,
  "currency": "USD",
  "leader_sku": "865462",
  "price_source": "constructor-browse"
}
```

And one real SKU row, from `sample_skus.json`:

```json
{
  "sku": "2235076",
  "title": "Delaney 14\" Marble End Table, Bronze",
  "price": 349.0,
  "original_price": 449.0,
  "discount_pct": 22.27,
  "currency": "USD",
  "availability": "ON_HAND",
  "options": {"Finish": "Bronze & Banswara Marble", "Quantity": "Individual"},
  "product_id": "delaney-marble-end-table",
  "price_source": "pdp-state"
}
```

Both samples are cut from real runs by `tools/cut_samples.py`, not written
by hand, and CI checks their columns against the schema.

**The two sources agree where they overlap.** A listing row's
`price`/`price_max` is the product page's own `aggregatePrice`
low/high selling price: York 1679/4199 and PB Comfort 1359/4899, from both
sources.

**The currency is stated, never assumed.** A product page states it
(`config.pricing.currencyData.selectedCurrency: "USD"`), and so does a
listing page (`shop.currencyData`). The listing API states none, so a listing
row's currency comes from the page the key was read from. With no page read,
it comes from the currency recorded for that exact key when it was measured.
An unknown key read with no page gives `null`.

**Stock is per SKU, and it is real.** `availability` is `ON_HAND`,
`BACK_ORDERED` (with `back_ordered_date`) or `NLA`, and was populated on
**6,214 of 6,214** SKUs across three product pages. There is **no rating
column**: the product page's `reviews` node is a bare reference and the
listing API carries none. Zero rating keys appeared across 3 product pages
and 24 listing results. A column that is null on every row should not exist.

---

## Traps that look like bugs

**A sofa's product page served "no SKUs", and yet this returns 1,367 rows.**
On a *guided* product (a configurator: sofas, sectionals) the page's
`subsets` list is **empty**. The SKUs are in
`productDetails.subsetsCompressedValue`, base64-encoded Brotli. York sofa:
117,380 characters decode to 4.2 MB of JSON and 1,367 SKUs. A parser that
reads `subsets` alone returns zero rows for every sofa on the site while
reporting success. The count is checkable, and the check closes exactly:
`skuCount` counts the non-`NLA` SKUs, so York's 1,367 − 93 NLA = 1,274 =
`skuCount`. Every product in a run is checked, and a mismatch is warned
about and recorded.

**A product's SKU rows go lower than its listing price.** That is expected.
SKUs that are `NLA` (no longer available) stay in the page with their last
price, and the site's own range leaves them out. On York on 2026-10-01, all
1,380 SKUs ran 1679–4199, while the 1,235 that are not `NLA` ran 1839–4199,
which is exactly the listing's and the page's `aggregatePrice`. Filter on
`availability` for what can be bought.

**A guessed category URL lands on a hub.** `/shop/furniture/sofas/` (plural)
301s to the department hub `/shop/furniture/`, a real page with no grid. The
leaf is `/shop/furniture/sofa/`. Take category URLs from `catalog_walk.py`,
never from a guess. A hub is reported as a hub (exit 4), not as blocked.

**`group_id=furniture` returns curtain rods.** A top-level group is broad:
8,266 results, and the first one was a curtain rod (recon, 2026-09-23).
Check what a group contains (the sidecar's `group.display_name` and
`count`) before naming it.

**A product slug is the id. Do not parse the number out of it.** Some slugs
end in digits (`open-box-cline-swivel-counter-stool-14341192`), some do not
(`russo-vanity-mirror-mp`). Open-box items are separate products: **3,339**
of the sitemap's 16,966 product URLs are `open-box-*`
(`--no-open-box` drops them).

**The UK store is not a fallback.** A non-US exit is redirected to
potterybarn.co.uk. curl here follows redirects only inside
www.potterybarn.com. A hop to the UK host is returned *unfetched* and
classified `geo_redirect`, which rotates to a fresh US exit. A pinned 2Captcha
session has been seen drifting from El Paso to a Frankfurt AWS address
minutes into its life (recon, 2026-09-23), and a fresh session fixed it every
time. So `http_scraper.py` mints a fresh session on every refusal.

**A 200 is not a product page.** The 2Captcha Scraper API answered HTTP 200
for two different product URLs with the *same* 137,461-byte prerendered page,
titled "Timeless Furniture & Elevated Interiors", with no product in it. The
classifier needs a `productDetails` node before it calls anything a product.

---

## The listing API is a third-party host

This deserves its own section, because the usual reassurance does not apply.

`api_scraper.py` calls **`ac.cnstrc.com`**, Constructor.io's API, not
potterybarn.com. It sends the request the storefront's own grid sends, with
the storefront's own public key, one page at a time. But **potterybarn.com's
robots.txt governs www.potterybarn.com and does not speak for that host**.
Saying "robots.txt allows it" would be false. It neither allows nor forbids
it, because it is not about that host at all.

What this repo does about that:

* It sends only what a visitor's browser sends. No key of its own and no
  parameter the storefront doesn't use (24 results a page, though the API
  accepted 100 and 200).
* It honours the API's rate-limit headers and slows down before they run
  out.
* It never pages a category through potterybarn.com's own facet URLs, which
  robots.txt does disallow (`Disallow: /*N=`).

Whether that is acceptable for your use is your call to make, and it should
be made knowing which host is involved. A sibling in this family set the
precedent of reading a site's own catalogue API. This one is the first where
that API belongs to a third party.

**robots.txt on www.potterybarn.com** is respected for every URL this repo
fetches there. `robots.snapshot.txt` is the file as fetched (and embedded in
`product_parser.py`, so an installed wheel cannot silently lose it). The
matcher implements `*` and longest-match precedence. `/products/<slug>/` and
`/shop/<category>/` are allowed. Facet URLs, `/shop/*+*+*`, `*/quicklook`,
`*/apartment-` (except `apartment-furniture/`), the cart and the account
pages are refused before any request is made.

---

## About reCAPTCHA

**No challenge was met anywhere this scraper goes.** The product page
carries one reCAPTCHA sitekey, in the config of a send-to-a-friend form, and
renders no widget. An anchor probe with a deliberately bogus control key:

| Key | `size=normal` | `size=invisible` |
|---|---|---|
| the page's key | **39,669 b anchor, no error** | 1,492 b |
| bogus control | 1,495 b, `Invalid site key` | 1,495 b |

A full anchor under `size=normal` means **v2 checkbox**, not v3 and not
invisible. The detector looks for a *rendered* widget and never for the
sitekey string. Matching the string would fire on every product page and
send it to the paid solver.

On the one refusal the Scraping Browser returned, the page still *mentioned*
a captcha: the auto-solve extension injects its own scripts into every
page, and they name `cf-turnstile`. Those scripts are stripped before
classification, and a test pins that the Scraping Browser's own 403 page
would otherwise have been sent to the solver.

---

## Engines

**The listing API and curl are the primary transports here.** That is
unusual in this family and it is measured, not preferred. Every engine has
now been run against the live site (2026-10-01):

|  | `api_scraper` | `http_scraper` | Playwright / CDP | Playwright, local | pyppeteer | Selenium |
|---|---|---|---|---|---|---|
| Listings, live | yes | — | yes | yes | yes, local and CDP | yes |
| ...identical to `api_scraper`, field for field | — | — | yes | **yes** (72/72) | **yes** (72/72) | **yes** (72/72) |
| Product pages, live | — | **yes** | **yes** | refused | over CDP only | no (see below) |
| ...identical to `http_scraper`, field for field | — | — | **yes** (4/4, 1,380/1,380) | — | **yes** (4/4) | — |
| Needs a US exit | no | yes | brings its own | yes | yes / brings its own | yes |
| Authenticated remote CDP | n/a | n/a | yes | — | yes | **no** |
| Authenticated proxy | n/a | yes | n/a | yes | **no** on current Chrome | **no** |

### The Scraping Browser API on product pages, and the bug it exposed

**2026-10-01: 14 of 14 sessions were served the product page** (HTTP 200,
the real page): 10 through our residential US exits attached per connection,
4 through the profile's own exit. **Field-for-field parity with curl:**
Delaney end table 4 of 4 SKUs and York sofa (guided, compressed) **1,380 of
1,380** SKUs, the same SKU sets, **zero differing fields** except
`scraped_at`. `Captcha.setAutoSolve` was accepted every time, with zero
Captcha events.

The first six of those sessions *looked* empty, and that is a bug this
repo had, now fixed. **After hydration the storefront deletes its
`<script>window.__INITIAL_STATE__=…</script>` tag from the DOM.** The served
body classified `product`, and the rendered DOM three seconds later
classified `empty`, while `window.__INITIAL_STATE__` stayed alive in
JavaScript. So the browser engines now read the **navigation's own response
body** first, which is the same bytes curl gets. Selenium has no response
object, so it reads the live JS object (`JSON.stringify(window.__INITIAL_STATE__)`).
The sidecar records which one was used (`state_source`).

A week earlier the picture was different, and the numbers stand as
measured. On 2026-09-24 the Scraping Browser was refused with a real HTTP
**403** on product pages: 8 of 8 sessions on the profile's own exit (one
Verizon Business address every time), 2 of 3 through our exits, and 3 of 3
in ten instrumented iterations. In those iterations `setAutoSolve` was
accepted 10 of 10 with 0 Captcha events, 5 of 7 listing runs completed with
48 rows each, 2 ended in a 60-second navigation timeout, and two repeats of
one listing were identical. Same profile, same code path to the site, and a
different answer a week later: **measure before relying on either week.**

### One connection per run

One Scraping Browser connection per run, closed in `finally`, never retried,
and **never preceded by `GET /json/version`**: that check takes the profile
lock itself (homedepot-scraper, 2026-09-23). A connection that ends
abnormally holds the profile. On 2026-09-24 a navigation that timed out
(`wait_until="load"` on a page whose load event never fires) left the profile
answering `500` to a connect **28 minutes later**. So Playwright navigates
with `wait_until="commit"` and reads the response body, never waiting for the
page to render. `domcontentloaded` still timed out in 2 of 10 instrumented
runs on 2026-09-24 and 1 of 10 on 2026-10-01. After the change: **0 of 10**,
5 to 8 seconds a run (2026-10-01). Selenium uses `page_load_strategy =
"eager"` for the same reason; pyppeteer has no earlier wait than
`domcontentloaded`. If a profile is stuck, stop touching it and wait.

A connect that fails with `500` and the body **`proxy_error`** is the
profile's own exit failing at the vendor, not a lock. It happened on
2026-10-01 while a residential exit attached to the same profile per
connection worked (`tools/browser_profile_client.py connection`).

### Engine limits, measured

* **Selenium cannot use an authenticated CDP endpoint.** chromedriver's
  `debuggerAddress` has nowhere to put a password, so `--cdp-endpoint` is
  refused (exit 5).
* **Selenium cannot authenticate a proxy.** The credential is stripped, and
  Chrome then gets the proxy's own refusal: an empty 39-byte document. That
  is reported as exit 5, the transport, not exit 3, the site. It reads
  listings fine, since those need no proxy.
* **pyppeteer cannot authenticate a proxy on current Chrome.** Its
  `page.authenticate` rides on `Network.setRequestInterception`, which Chrome
  154 no longer implements. Reported as exit 5 with the reason. Over
  `--cdp-endpoint` the password travels in the WebSocket URL instead, and
  there it works (product page 4/4 identical to curl).
* **pyppeteer on Apple silicon needs `--browser-path`.** The Chromium it
  downloads is an x86_64 build that did not come up under Rosetta within its
  launch window. The system Chrome did. pyppeteer is effectively
  unmaintained and is kept for parity.

**The 2Captcha Scraper API is not a transport in this repo.** It answered
HTTP 200 with a prerendered generic page instead of the product (see *Traps*),
3 calls of 3, at $0.0005 each (2026-09-24). A client that gets a 200 with the
wrong page is worse than none, so none ships.

---

## Exit codes

| Code | Meaning |
|---|---|
| `0` | Complete |
| `1` | Crash |
| `2` | Bad usage, including a URL on another host or one robots.txt disallows |
| `3` | Blocked: the site refused (a 403, a redirect to the UK store, a 429 from the API) |
| `4` | Zero products, including a hub category, which is a correct answer |
| `5` | Our plumbing, not the site: a timeout, a dropped CDP socket, a locked profile, a proxy 407 |
| `6` | Partial: some pages or products fetched, then stopped or failed |

**`3` and `5` mean different things.** A 407 on the proxy tunnel means
potterybarn.com was never reached. It is never retried or rotated, because
every exit on a dead credential fails the same way. On this vendor a 407 has
been measured to be *transient* (homedepot-scraper: an hour of 407s, then 200
the next day with nothing changed), so "retry later" comes before "replace
the credential".

**A run that finds nothing writes nothing.** Yesterday's good output is not
replaced with `[]`; `--allow-empty` is the opt-out. `<out>.meta.json`
describes the dataset beside it, and `<out>.latest_attempt.meta.json` is
written on **every** run, so a pipeline can tell a fresh success from
yesterday's.

---

## Measured results

2026-09-24 unless marked.

| Run | Transport | Rows | Notes |
|---|---|---:|---|
| Sofas, all pages | `api_scraper`, no proxy, Moscow | **434** | 19 pages + 1 empty; `total_num_results` 434; arithmetic closes (one sweep in between did not; see *Pagination*) |
| Sofas, all pages, **at rate-limit zero** (2026-10-01) | `api_scraper`, no proxy | **433** | first sweep 403 distinct, second sweep recovered 30; 433 of 433 |
| Delaney + York, browser vs curl (2026-10-01) | Playwright / Scraping Browser | **1,384** | identical to curl in every field |
| Dining benches, all pages | `api_scraper`, no proxy | **72** | 72 of 72; 15 sub-group rows over 65 products |
| Delaney end table + York sofa | `http_scraper`, curl, US exit | **1,371** | 4 SKUs inline + 1,367 decoded; both SKU counts close |
| 15 random leaf categories | curl, US exit | — | 15 of 15 served; page `categoryId` == URL's last segment on **15 of 15** |
| Category sitemap | `catalog_walk`, no proxy | 795 | 772 leaves, 23 hubs |
| Product sitemap | `catalog_walk`, no proxy | 16,966 | 3,339 `open-box-*`; 16,873 the day before |

The sitemaps need no proxy, but they are **user-agent filtered**: from
Moscow, a browser user agent got 200 on all three files and the default
`python-requests/…` agent got 403 on all three. `catalog_walk.py` sends the
browser header set.

---

## Comparing two runs

```bash
python3 diff_runs.py --old sofas.2026-09-24.json --new sofas.2026-09-25.json
```

Joins on `sku` and reports added, removed and changed rows. It refuses runs
that are not both `complete` (a partial run's unfetched pages would read as
delistings), and runs of different modes (a product slug and a SKU id are
different keys). The tracked fields differ per mode: a product's range and
markdown for listings, and a SKU's price, markdown and stock for product
runs.

---

## Testing

```bash
python3 smoke_test.py          # the offline suite: no network, no browser
python3 -m pytest -q           # the same suite as one pytest test
python3 .github/ci_checks.py --all
```

Over 900 checks, the same with no engine installed and with each engine in
its own virtualenv (2026-10-01, 0 failures in all four). Absent engines
report a skip, and CI fails when the engine it installed is skipped.
`tests.yml` also runs one venv per engine, builds the wheel and uses it
outside the checkout, and builds the Docker image.

`tools/transport_trials.py` re-runs the paired client measurement above,
`tools/autosolve_probe.py` + `tools/compare_runs.py` the browser iterations.

---

## Troubleshooting

See [TROUBLESHOOTING.md](TROUBLESHOOTING.md), organised by symptom.

---

## Do you need the paid products?

**For listings: no.** No proxy, no browser, no key.

**For product pages: a US residential exit, yes. Nothing else.**

* **[Proxies](https://2captcha.com/proxy)**: `POTTERYBARN_PROXY`. The US
  exit product pages require. A `-region-us` gateway login lets
  `http_scraper.py` mint a fresh session-pinned exit on every refusal: five
  minted sessions gave five distinct US residential addresses.
* **Scraping Browser API**: `POTTERYBARN_CDP_ENDPOINT`. The only browser
  path that reaches product pages, since a local Chromium is refused. It got
  them on 14 of 14 sessions on 2026-10-01, identical to curl field for
  field, after being refused a week earlier. curl remains the default because
  it needs no browser and no profile lock.
* **[Captcha solving](https://2captcha.com)**: `TWOCAPTCHA_KEY`. Wired, and
  on this site it has never had anything to do.
* **Fingerprints**: `fingerprint_client.py`. Not needed over CDP, where the
  remote browser brings its own.

---

## Contributing, security, licence

* [CONTRIBUTING.md](CONTRIBUTING.md): how to report that the site changed.
* [SECURITY.md](SECURITY.md): how to report a vulnerability.
* [TROUBLESHOOTING.md](TROUBLESHOOTING.md): failure modes, by symptom.
* [CHANGELOG.md](CHANGELOG.md): what shipped, with the measurements.
* MIT; see [LICENSE](LICENSE).

---

## Legal

This repo reads **public catalogue data** as an anonymous visitor is served
it. It does not log in, does not touch a cart or an order, and does not
attempt to defeat a protection. A refused page is reported, not worked
around.

On www.potterybarn.com, robots.txt is respected for every request. The
listing API is a different host that robots.txt does not govern. See [The
listing API is a third-party host](#the-listing-api-is-a-third-party-host)
and decide with that in mind.

The default `--delay` is 1 second between requests. Raise it for anything
long, and do not point concurrency at one address. Respect the site's terms
and the law where you operate.
