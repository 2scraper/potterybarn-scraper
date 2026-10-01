# Contributing

Bug reports, site-change reports and pull requests are welcome. This file
covers what is specific to a scraper, which is not the usual list.

## Before you open anything

Run the offline suite. It needs no network, no browser and no key:

```bash
pip install -r requirements.txt
python3 smoke_test.py
```

**It must pass with no engine installed.** CI's first job installs only
`requirements.txt`, so an engine import in a test has to be guarded with the
skip recorded — easy to get wrong locally, where an engine is probably
installed.

## Never commit a credential

```bash
tools/install_hooks.sh      # once per clone
```

The pre-commit hook refuses a commit that stages a `.env` variant, a
backup-shaped file (`.bak`, `.orig`, `.key`, ...) or anything credential-shaped;
the pre-push hook scans the commits about to leave. CI runs the same scanner,
but CI runs after the objects are on a server — and **deleting a branch does
not remove a pushed secret**; GitHub serves the objects by SHA. A pushed
secret is a disclosed one: rotate it.

Never `cp .env` to another name inside the repo. That is exactly how a sibling
repo published three live credentials.

## Reporting a site change

This repo reads two JSON sources, and "the markup changed" is almost never
the diagnosis:

1. **The listing API** — `ac.cnstrc.com/browse/group_id/<id>`. A result's
   fields are read in `product_parser.product_from_result`.
2. **A product page's `window.__INITIAL_STATE__`** —
   `product.productDetails`, and on a guided product its Brotli-compressed
   `subsetsCompressedValue`.

Say which one broke, and paste the fragment rather than describing it.

## Pull requests

Add a test for the behaviour you change. `smoke_test.py` is one file of plain
functions over fixtures cut from real captures (`fixtures/README.md` says
which are trimmed). Properties already pinned there because each cost time
once:

- **A guided product's SKUs are only in `subsetsCompressedValue`.** Reading
  `subsets` alone returns zero rows for every sofa while reporting success.
  A missing `brotli` package RAISES; it never degrades to zero rows.
- **The SKU count must close**: rows − NLA == the page's `skuCount`.
- **`sku` means the product slug in a listing row and the SKU id in a SKU
  row.** `leader_sku` is never written into `sku`, and diff_runs.py refuses
  to compare the two modes.
- **A redirect to potterybarn.co.uk is a refusal**, and the UK page is never
  fetched.
- **The Scraping Browser's own 403 page carries `cf-turnstile`** in its
  injected extension scripts; the strip is what keeps it from reaching the
  paid solver.
- **Exit 3 is the site; exit 5 is us** — timeouts, a proxy 407, a locked
  profile.
- **A run that finds nothing writes nothing.** `--allow-empty` is the opt-out.
- **No code calls `GET /json/version`**: it takes the profile lock it reports
  on.

### Style

Match the file you are editing. Comments explain *why*. Every remote call is
bounded. Fail loudly: an empty list returned on error is this codebase's most
common historical bug. Never present a guess as a fact — a price that could
not be read is `None`, a currency nobody stated is `None`.

### If your change needs a live run

Say in the PR what you ran, from what kind of address, and what came back —
the exit code and the sidecar's `stop_reason`. Product pages need a US exit
and curl; "it returned nothing" without saying which is not reproducible.

## Scope

Public catalogue data only, as an anonymous visitor is served it. Out of
scope: anything behind a login, anything that submits a form, any path the
site's robots.txt disallows, and anything that defeats a protection rather
than being served the way an ordinary visitor is.

## Licence

MIT. By opening a pull request you agree your contribution ships under it.
