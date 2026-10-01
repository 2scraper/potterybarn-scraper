# Fixtures

Every file here was cut from a real capture made on 2026-09-23/24. Which ones
are verbatim, and what was done to the rest:

| File | From | Verbatim? |
|---|---|---|
| `blocked_403_1326.html` | the branded 403, from a non-US address | **verbatim** — no reference id or token in it |
| `blocked_403_scraping_browser.html` | the branded 403 as the Scraping Browser API rendered it, with the auto-solve extension's injected `<script>` tags | **verbatim** |
| `product_delaney.html` | a SIMPLE product page (4 SKUs, inline `subsets`) | **trimmed** — see below |
| `product_york_guided.html` | a GUIDED product page (1,367 SKUs, Brotli `subsetsCompressedValue`) | **trimmed** |
| `listing_sofa.html` | a leaf category page | **trimmed** |
| `hub_furniture.html` | a department hub | **trimmed** |
| `browse_sofa_p1.json` | one Constructor browse response | **trimmed**: 6 of 24 results, bulky per-result fields removed, `result_id` replaced with zeros |

**Trimmed** means: a minimal HTML shell around `window.__INITIAL_STATE__`, and
inside the state only the nodes the parser reads — `productDetails`
(`groupId`, `title`, `pipType`, `skuCount`, `subsets` or
`subsetsCompressedValue`, `aggregatePrice`, `breadcrumbs`, ...), the page's
`currencyData`, and, on the Delaney page, the send-to-a-friend form's
`recaptcha` config node verbatim, because it carries the sitekey a naive
detector would fire on. Everything else in the served state (~220 keys of UI
state, analytics context, A/B test assignments) was dropped, and with it any
session material.

Each trimmed fixture was checked to parse IDENTICALLY to its untrimmed
original when it was cut: the same rows, field for field, and the same
facts (sku-count check, aggregate prices, currency). The suite pins the
values, and guards the next capture against session-shaped strings with
patterns rather than literals.
