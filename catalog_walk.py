#!/usr/bin/env python3
"""Sitemap discovery for potterybarn.com — find targets without crawling.

    python3 catalog_walk.py categories --grep sofa
    python3 catalog_walk.py products --grep sectional --out-file targets.txt
    python3 http_scraper.py --urls-file targets.txt --out sectionals

robots.txt lists seven sitemap indexes; two matter here, and each points at
ONE gzip file (measured 2026-09-24):

    /netstorage/sitemaps/shop-sitemap-index.xml     ->  shop-sitemap-1.xml.gz
                                                        795 category URLs
    /netstorage/sitemaps/product-sitemap-index.xml  ->  product-sitemap-1.xml.gz
                                                        16,966 product URLs

The product count was 16,873 the day before — it is a live catalogue.

No proxy is needed. From a Moscow address with no proxy, all three files
answered 200 to a browser user agent — and **403 to the default
`python-requests/...` user agent** (1,326 b, `server: AkamaiNetStorage`), so
the browser header set is sent here too. This is unlike the product pages,
which refuse every non-US address.

Take category URLs from here, never from a guess: `/shop/furniture/sofas/`
(plural) 301s to the HUB `/shop/furniture/`, which looks like a valid page and
has no grid. The leaf is `/shop/furniture/sofa/`. `--leaf-only` removes the
top-level `/shop/<department>/` entries, which are the hubs among the
sitemap's rows; a deeper URL can still land on a hub, and api_scraper.py
classifies the page that actually came back.

Open-box items are separate products with their own slugs
(`open-box-...`), and `--no-open-box` drops them.
"""

from __future__ import annotations

import argparse
import gzip
import re
import sys
from typing import Callable, Iterator, List, Optional
from urllib.parse import urlparse

import product_parser as P

SHOP_INDEX = "https://www.potterybarn.com/netstorage/sitemaps/shop-sitemap-index.xml"
PRODUCT_INDEX = "https://www.potterybarn.com/netstorage/sitemaps/product-sitemap-index.xml"

_LOC_RE = re.compile(r"<loc>\s*([^<\s]+)\s*</loc>", re.I)


def locs(xml: str) -> List[str]:
    """Every `<loc>`. A regex, not an XML parser: the files are large,
    machine-generated and well-formed, and a malformed one yields fewer URLs
    here rather than raising — the caller reports the count."""
    return _LOC_RE.findall(xml or "")


def decode(body: bytes, url: str) -> str:
    """A sitemap body, gunzipped when it is gzip (by magic, not by name)."""
    if body[:2] == b"\x1f\x8b":
        body = gzip.decompress(body)
    return body.decode("utf-8", "replace")


def is_hub(url: str) -> bool:
    """`/shop/<department>/` — one segment under /shop/."""
    return P.is_department_path(url)


def walk(fetch: Callable[[str], bytes], index_url: str, *,
         grep: Optional[str] = None, limit: Optional[int] = None,
         leaf_only: bool = False, open_box: bool = True,
         log=None) -> Iterator[str]:
    pattern = re.compile(grep, re.I) if grep else None
    children = locs(decode(fetch(index_url), index_url))
    if log:
        log("%s: %d sitemap file(s)" % (index_url, len(children)))
    emitted = 0
    for child in children:
        urls = locs(decode(fetch(child), child))
        if log:
            log("  %s: %d URL(s)" % (child, len(urls)))
        for url in urls:
            if leaf_only and is_hub(url):
                continue
            if not open_box and "/products/open-box-" in url:
                continue
            if pattern and not pattern.search(url):
                continue
            if not P.robots_allows(url):
                continue
            yield url
            emitted += 1
            if limit and emitted >= limit:
                return


def http_fetch(timeout: int, proxy: Optional[str] = None) -> Callable[[str], bytes]:
    import requests
    import http_scraper
    session = requests.Session()
    session.headers.update(http_scraper.BROWSER_HEADERS)
    if proxy:
        session.proxies.update({"http": proxy, "https": proxy})

    def fetch(url: str) -> bytes:
        try:
            response = session.get(url, timeout=timeout)
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError("could not fetch %s: %s: %s" % (
                url, type(exc).__name__, _mask(str(exc)))) from None
        if response.status_code != 200:
            raise RuntimeError("%s answered HTTP %d" % (url, response.status_code))
        return response.content
    return fetch


def _mask(text: str) -> str:
    import proxy_pool
    return proxy_pool.mask_text(text)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Discover potterybarn.com scrape targets from its sitemaps.")
    parser.add_argument("kind", choices=["categories", "products"])
    parser.add_argument("--grep", default=None, help="Keep URLs matching this regex.")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--leaf-only", action="store_true",
                        help="categories: drop /shop/<department>/ hubs.")
    parser.add_argument("--no-open-box", action="store_true",
                        help="products: drop open-box-* items.")
    parser.add_argument("--out-file", default=None)
    parser.add_argument("--timeout", type=int, default=60)
    parser.add_argument("--via-proxy", action="store_true",
                        help="Fetch through POTTERYBARN_PROXY. Not needed from "
                             "the addresses measured.")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    proxy = None
    if args.via_proxy:
        import env_config
        ns = argparse.Namespace(proxy=None)
        env_config.apply(ns, keys={"POTTERYBARN_PROXY": "proxy"}, quiet=True)
        proxy = ns.proxy
        if not proxy:
            print("--via-proxy needs POTTERYBARN_PROXY", file=sys.stderr)
            return 2

    log = (lambda m: print(m, file=sys.stderr)) if args.verbose else (lambda m: None)
    index = SHOP_INDEX if args.kind == "categories" else PRODUCT_INDEX
    found: List[str] = []
    try:
        for url in walk(http_fetch(args.timeout, proxy), index, grep=args.grep,
                        limit=args.limit, leaf_only=args.leaf_only,
                        open_box=not args.no_open_box, log=log):
            found.append(url)
    except RuntimeError as exc:
        print("[!] %s" % exc, file=sys.stderr)
        return 5 if "could not fetch" in str(exc) else 3

    if args.out_file:
        with open(args.out_file, "w", encoding="utf-8") as handle:
            handle.write("\n".join(found) + ("\n" if found else ""))
        print("[+] %d target(s) -> %s" % (len(found), args.out_file))
    else:
        for url in found:
            print(url)
    return 0 if found else 4


if __name__ == "__main__":
    sys.exit(main())
