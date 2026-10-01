"""product_parser.py — everything this repo knows about potterybarn.com.

Two sources, and they are different hosts
-----------------------------------------
* **Listings** come from Constructor.io's browse API on `ac.cnstrc.com`, not
  from potterybarn.com. A leaf category page's served HTML carries ZERO
  product links; its grid is fetched client-side from that API with the
  storefront's own public key. `parse_browse_response` reads that JSON.
* **Product pages** come from `www.potterybarn.com/products/<slug>/`, and all
  of their data sits in `window.__INITIAL_STATE__`. There is no `Product`
  JSON-LD on this site (the PDP's only JSON-LD block is a `BreadcrumbList`),
  so the family's "JSON-LD first" order does not apply. `parse_product` reads
  the state.

Two row shapes, on purpose (see output_writer)
-----------------------------------------------
A listing result is a PRODUCT with a price RANGE. A product page holds that
product's SKUs, each with its own price and stock. Measured 2026-09-24, one
sofa's page holds 4,843 SKUs.

The trap in the product page: `subsetsCompressedValue`
------------------------------------------------------
On a GUIDED product (sofas, sectionals — `pipType: "GUIDED_PIP"`) the served
state's `subsets` list is EMPTY. The SKUs are still in the page, as
`productDetails.subsetsCompressedValue`: Brotli, then base64. Measured
2026-09-24 on two guided pages — York sofa: 117,380 chars -> 4.2 MB of JSON
-> 1,367 SKUs; PB Comfort sofa: 4,843 SKUs. A parser that reads only
`subsets` returns zero rows for every sofa on the site while reporting a
successful fetch.

The decoded count is checkable against the page's own `skuCount`, which
counts the SKUs that are DISPLAYABLE or NOT `NLA` (no longer available).
Measured 2026-10-01: that rule matched skuCount on 20 of 20 random product
pages and on 20 of 20 more held out from them, plus the three fixtures (York:
1,367 SKUs, 1,274 counted). The first rule written here — "rows - NLA" — was
fitted on four products and missed 3 of those 20 (an NLA SKU that is still
displayable is counted). `sku_count_check` keeps the arithmetic so a partial
decode cannot pass as a complete one.
"""

from __future__ import annotations

import base64
import html as html_lib
import json
import re
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import urlparse

try:  # Needed only for GUIDED product pages; see module docstring.
    import brotli  # type: ignore
except ImportError:  # pragma: no cover - the suite covers the absent case
    brotli = None

from output_writer import Product, SkuRow

SITE = "potterybarn.com"
BASE_URL = "https://www.potterybarn.com"
HOSTS = ("www.potterybarn.com", "potterybarn.com")

# Refused WITH THE REASON rather than as "not a Pottery Barn site", which is
# false for the first one and sends the reader looking for a typo.
UNSUPPORTED_HOSTS = {
    "www.potterybarn.co.uk": "Pottery Barn UK is a separate storefront in GBP "
                             "with its own catalogue. This repo covers the US "
                             "store only; a non-US exit is geo-redirected "
                             "there, which is why that redirect is treated as "
                             "a refusal rather than as content.",
    "potterybarn.co.uk": "see www.potterybarn.co.uk",
    "www.potterybarnkids.com": "Pottery Barn Kids is a sibling brand on its "
                               "own host and has not been measured.",
    "www.pbteen.com": "PB Teen is a sibling brand on its own host and has not "
                      "been measured.",
}

# ---------------------------------------------------------------------------
# Constructor.io — the listing API
# ---------------------------------------------------------------------------

CONSTRUCTOR_HOST = "ac.cnstrc.com"
BROWSE_URL = "https://ac.cnstrc.com/browse/group_id/{group_id}"

# The storefront's PUBLIC client key. Not a secret: every listing page embeds
# it at `__INITIAL_STATE__.shop.searchEngineConfig.constructorKey`, and it is
# what every visitor's browser sends. It can rotate, so the scrapers read it
# from a listing page when they have a US exit, and fall back to this value
# (with a warning) when they do not. Measured identical on 2026-09-23 and
# 2026-09-24.
FALLBACK_CONSTRUCTOR_KEY = "key_w3v8XC1kGR9REv46"

# The currency each key's prices are in. The browse API does not state one,
# so a currency for a listing row has to come from somewhere that does:
# either the listing page the key was read from
# (`shop.currencyData.selectedCurrency`), or this table, which records what
# that page said for this key when it was measured (2026-09-24). An unknown
# key read with no page gets NO currency, never a defaulted one.
KNOWN_KEY_CURRENCY = {FALLBACK_CONSTRUCTOR_KEY: "USD"}

# 24 is what the site's own grid asks for. The API accepted 100 and 200
# (measured 2026-09-24), but 24 keeps each call identical in shape to the
# storefront's, which is the least surprising thing to send to a host whose
# robots.txt does not speak for it.
PAGE_SIZE = 24

# Prices in the browse response are DOLLARS (a sofa at 1679-4199, not
# 16.79). No factor is applied to them.

# ---------------------------------------------------------------------------
# Page states
# ---------------------------------------------------------------------------

# The shop's OWN branded refusal. Measured 2026-09-23/24, every one HTTP 403,
# body sizes 652, 1,326, 1,425, 1,600 and 3,307 bytes depending on the client
# that was refused — no vendor marker in any of them. Status is the primary
# signal (a rule of this scraper family); these are the secondary one, for engines with
# no response object.
BLOCK_MARKERS = (
    "pottery barn: 403 - restricted access",
    "due to website restrictions we are unable to display the requested page",
    "restricted access error",
)

# A rendered widget, never a sitekey string. The PDP's config carries one
# sitekey for a send-to-a-friend form, and an anchor probe on 2026-09-24
# showed it is a v2 CHECKBOX key (39,669 b anchor under size=normal; a bogus
# control key got 1,495 b and "Invalid site key"). Matching the key string
# would fire on every product page and route it to the paid solver.
CAPTCHA_MARKERS = (
    'class="g-recaptcha"',
    "recaptcha/api2/anchor",
    "geo.captcha-delivery.com",
    "cf-turnstile",
)

# The auto-solve extension of the Scraping Browser API injects its own
# hunters, one of them declaring `cf-turnstile` — which IS in the marker set
# above, so this strip is load-bearing here (a rule of this scraper family).
_EXTENSION_SCRIPT_RE = re.compile(
    r"<script[^>]+src=[\"'](?:chrome|moz)-extension://[^\"']*[\"'][^>]*>\s*</script>",
    re.I)

UK_HOST_MARKER = "potterybarn.co.uk"

_STATE_ANCHOR = "window.__INITIAL_STATE__"
_TAG_RE = re.compile(r"<[^>]*>")


def strip_extension_scripts(text: str) -> str:
    return _EXTENSION_SCRIPT_RE.sub("", text or "")


def _scan_json_object(text: str, start: int) -> Optional[str]:
    """The balanced `{...}` beginning at `start`, string-aware.

    The state is 250 KB to 1.4 MB of product copy, and a brace inside a
    description is common. homedepot-scraper's first scanner ignored string
    literals and died on exactly that with `Extra data`.
    """
    depth = 0
    i = start
    n = len(text)
    while i < n:
        c = text[i]
        if c == '"':
            i += 1
            while i < n:
                if text[i] == "\\":
                    i += 2
                    continue
                if text[i] == '"':
                    break
                i += 1
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
        i += 1
    return None


def initial_state(html: str) -> Optional[Dict[str, Any]]:
    """`window.__INITIAL_STATE__` as a dict, or None if the page has none."""
    if not html:
        return None
    i = html.find(_STATE_ANCHOR)
    if i < 0:
        return None
    j = html.find("{", i)
    if j < 0:
        return None
    raw = _scan_json_object(html, j)
    if raw is None:
        return None
    try:
        parsed = json.loads(raw)
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


def product_details(state: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """`product.productDetails`, the PDP's one authoritative node.

    The `product` dict around it has ~220 keys and most are UI state
    (`isSidePanelDrawerOpen`, `heroImageErrorState`, ...). Nothing outside
    `productDetails` is read as a product fact, except the page's currency.
    """
    if not isinstance(state, dict):
        return None
    product = state.get("product")
    if not isinstance(product, dict):
        return None
    details = product.get("productDetails")
    if isinstance(details, dict) and details.get("groupId"):
        return details
    return None


def detect_page_state(html: str, *, status: Optional[int] = None,
                      url: Optional[str] = None,
                      headers: Optional[Dict[str, str]] = None) -> str:
    """One of product, listing, hub, geo_redirect, captcha, blocked, empty,
    or transport_error (an empty document: nothing was served at all).

    Order is deliberate:

    1. **geo_redirect** first. A non-US exit is 302'd to potterybarn.co.uk —
       a different store in GBP. Following it blindly silently changes WHICH
       STORE is being scraped, so a final URL on that host is a refusal,
       whatever the page says.
    2. **product** — a `productDetails` node with a `groupId`. A marker on a
       page whose product is already there guards nothing.
    3. **captcha** before block: a captcha is payable, a block is not.
    4. **blocked** — HTTP 401/403/406/429, or the branded refusal text on a
       page with no storefront state.
    5. **listing** vs **hub** — both carry `shop` state; a leaf has a
       non-empty `categoryId`, a hub (`/shop/furniture/`) an empty one. A
       guessed URL such as `/shop/furniture/sofas/` 301s to the hub, so the
       page that LANDED is what gets classified.

    `headers` is accepted for signature parity with the family and unused:
    the branded 403 is told apart by status and text, not by a header.
    """
    if url and UK_HOST_MARKER in (urlparse(url).hostname or ""):
        return "geo_redirect"
    text = strip_extension_scripts(html or "")
    lowered = text.lower()

    state = initial_state(text)
    if product_details(state) is not None:
        return "product"
    if state is None and any(marker in lowered for marker in CAPTCHA_MARKERS):
        return "captcha"

    if status in (401, 403, 406, 429):
        return "blocked"
    if state is None and any(m in lowered for m in BLOCK_MARKERS):
        return "blocked"

    shop = (state or {}).get("shop")
    if isinstance(shop, dict):
        return "listing" if shop.get("categoryId") else "hub"
    if state is None and len(text) < 200 and not _TAG_RE.sub("", text).strip():
        # NOTHING was served: `<html><head></head><body></body></html>`, 39
        # bytes. Measured 2026-10-01, Selenium through a proxy whose
        # credential it cannot send — the proxy refused, the site was never
        # reached. That is our plumbing, exit 5, not a refusal by the site;
        # the shortest refusal this site has ever sent is 652 bytes of text.
        return "transport_error"
    if state is None and len(text) < 8000:
        # Every refusal measured is under 3.4 KB and every served page over
        # 600 KB. A short document with no storefront state is not a page
        # this site served, whatever it says.
        return "blocked"
    return "empty"


# ---------------------------------------------------------------------------
# Listing page -> what the API call needs
# ---------------------------------------------------------------------------

def listing_context(html: str) -> Dict[str, Optional[str]]:
    """What a leaf category page tells us about its own grid.

    `group_id` is the page's `shop.categoryId` — the Constructor browse
    group. `key` is the public client key. `currency` is what the page says
    its prices are in. Any of them may be None; the caller decides.
    """
    state = initial_state(html) or {}
    shop = state.get("shop") if isinstance(state.get("shop"), dict) else {}
    engine = shop.get("searchEngineConfig") or {}
    currency = (shop.get("currencyData") or {}).get("selectedCurrency")
    return {
        "group_id": shop.get("categoryId") or None,
        "key": engine.get("constructorKey") if isinstance(engine, dict) else None,
        "currency": currency or None,
    }


# ---------------------------------------------------------------------------
# Browse response -> Product rows
# ---------------------------------------------------------------------------

def _number(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).replace(",", ""))
    except ValueError:
        return None


def _text(value: Any) -> Optional[str]:
    """HTML-unescaped, stripped text. Titles arrive as `Table (14&quot;)`."""
    if value is None:
        return None
    out = html_lib.unescape(str(value)).strip()
    return out or None


def product_url(slug: str) -> str:
    return "%s/products/%s/" % (BASE_URL, slug)


def product_from_result(result: Dict[str, Any], *, currency: Optional[str],
                        category: Optional[str] = None) -> Optional[Product]:
    """One browse result -> one listing row, or None if it has no id.

    A result is USUALLY a product, and sometimes a SUB-GROUP of one — a slice
    by finish, say. Measured 2026-09-24, 15 of 144 results over three
    categories: `id: aldon-dining-bench-SPAF-finish-russet-oak-wood-finish`,
    `url: /products/aldon-dining-bench/?subGroupId=<that id>`, and a second
    result for the same bench under another finish. So `sku` is the result's
    `id` (unique per row), `product_id` is the slug in the URL's PATH (the
    product page's `groupId`, which is what a SKU row joins on), and
    `sub_group_id` names the slice when there is one.

    `price` is `lowestPrice` — the "from" price the grid prints — and
    `price_max` is `highestPrice`. Both are SELLING prices: measured
    2026-09-24, York sofa lowestPrice 1679 / highestPrice 4199, and its own
    product page's `aggregatePrice` lowSellingPrice 1679 / highSellingPrice
    4199.

    `original_price` is `regularPriceMin`, which the API publishes only on
    products with a markdown. No per-row `discount_pct` is computed: the two
    minimums need not belong to the same SKU, so their ratio is not any SKU's
    discount. The site's own `maxDiscountPercent` is kept, under that name.
    """
    data = result.get("data") if isinstance(result, dict) else None
    if not isinstance(data, dict):
        return None
    slug = data.get("id")
    if not slug:
        return None
    price = _number(data.get("lowestPrice"))
    original = _number(data.get("regularPriceMin"))
    if original is not None and price is not None and original <= price:
        original = None
    flags = data.get("flags")
    leader = data.get("leaderSku")
    url = data.get("url") or product_url(slug)
    product_id = slug_from_url(url) or str(slug)
    return Product(
        url=url,
        sku=str(slug),
        title=_text(data.get("title") or result.get("value")),
        image_url=data.get("image_url") or None,
        price=price,
        currency=currency if price is not None else None,
        category=category,
        price_source="constructor-browse",
        product_id=product_id,
        sub_group_id=str(slug) if str(slug) != product_id else None,
        price_max=_number(data.get("highestPrice")),
        original_price=original,
        original_price_max=_number(data.get("regularPriceMax")) if original else None,
        max_discount_pct=_number(data.get("maxDiscountPercent")),
        leader_sku=str(leader) if leader is not None else None,
        pip_type=data.get("pipType") or None,
        price_type=data.get("productPriceType") or None,
        flags=list(flags) if isinstance(flags, list) and flags else None,
    )


def parse_browse_response(payload: Dict[str, Any], *, currency: Optional[str],
                          category: Optional[str] = None) -> List[Product]:
    response = (payload or {}).get("response") or {}
    rows = []
    for index, result in enumerate(response.get("results") or []):
        row = product_from_result(result, currency=currency, category=category)
        if row is not None:
            row.row_index = index
            rows.append(row)
    return rows


def browse_total(payload: Dict[str, Any]) -> Optional[int]:
    total = ((payload or {}).get("response") or {}).get("total_num_results")
    return total if isinstance(total, int) and not isinstance(total, bool) else None


def browse_group(payload: Dict[str, Any]) -> Dict[str, Any]:
    """The group the API says it answered for: name, count, parents.

    `group_id=furniture` answered 8,266 results whose first was a curtain
    rod (2026-09-23), so a top-level group is broad. A display name comes
    from here, never from the id.
    """
    groups = ((payload or {}).get("response") or {}).get("groups") or []
    if not groups or not isinstance(groups[0], dict):
        return {}
    group = groups[0]
    return {
        "group_id": group.get("group_id"),
        "display_name": group.get("display_name"),
        "count": group.get("count"),
        "parents": [p.get("display_name") for p in group.get("parents") or []
                    if isinstance(p, dict)],
    }


# ---------------------------------------------------------------------------
# Product page -> SkuRow rows
# ---------------------------------------------------------------------------

IMAGE_BASE = "https://assets.pbimgs.com/pbimgs/rk/images/dp/"
# The size group the listing API's own `image_url` uses (`...c.jpg`, 558x501).
IMAGE_SUFFIX = "c.jpg"


class DecodeError(RuntimeError):
    """`subsetsCompressedValue` was present and could not be read."""


def decode_compressed_subsets(value: str) -> List[Dict[str, Any]]:
    """Brotli-in-base64 -> the `subsets` list a simple page carries inline."""
    if brotli is None:
        raise DecodeError(
            "this product's SKUs are Brotli-compressed and the `brotli` "
            "package is not installed — `pip install -r requirements.txt`")
    try:
        subsets = json.loads(brotli.decompress(base64.b64decode(value)))
    except Exception as exc:  # noqa: BLE001 - any failure is the same answer
        raise DecodeError("could not decode subsetsCompressedValue: %s: %s"
                          % (type(exc).__name__, str(exc)[:120])) from None
    if not isinstance(subsets, list):
        raise DecodeError("subsetsCompressedValue decoded to a %s, not a list"
                          % type(subsets).__name__)
    return subsets


def subsets_of(details: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], str]:
    """`(subsets, where)` — inline when present, else the compressed copy."""
    inline = details.get("subsets")
    if isinstance(inline, list) and inline:
        return inline, "inline"
    compressed = details.get("subsetsCompressedValue")
    if isinstance(compressed, str) and compressed:
        return decode_compressed_subsets(compressed), "compressed"
    return [], "none"


def page_currency(state: Dict[str, Any]) -> Optional[str]:
    """The currency the product page itself states, or None."""
    product = state.get("product") if isinstance(state, dict) else None
    pricing = ((product or {}).get("config") or {}).get("pricing") or {}
    currency = (pricing.get("currencyData") or {}).get("selectedCurrency")
    return currency or None


_VALUE_MEMBERS = ("attribute", "thumbnail", "text", "skuSwatch")


def _options(sku: Dict[str, Any], definitions: Dict[str, Any]) -> Dict[str, str]:
    """`{"Finish": "Brass & Banswara Marble", "Quantity": "Set of 2"}`.

    selectionValueIds -> selectionValues[id] -> the attribute id(s) inside
    whichever typed member it carries (`attribute`, `thumbnail`, `text`,
    `skuSwatch`) -> attributes[aid] -> typeName: valueName.
    """
    values = definitions.get("selectionValues") or {}
    attributes = definitions.get("attributes") or {}
    out: Dict[str, str] = {}
    for value_id in sku.get("selectionValueIds") or []:
        entry = values.get(str(value_id)) or {}
        member: Dict[str, Any] = {}
        for candidate in _VALUE_MEMBERS:
            if isinstance(entry.get(candidate), dict):
                member = entry[candidate]
                break
        ids = member.get("attributeIds") or (
            [member["attributeId"]] if member.get("attributeId") is not None else [])
        for attribute_id in ids:
            attribute = attributes.get(str(attribute_id)) or {}
            name = _text(attribute.get("typeName"))
            value = _text(attribute.get("valueName"))
            if name and value:
                out[name] = value
    return out


def _sku_image(sku: Dict[str, Any]) -> Optional[str]:
    for image in (sku.get("media") or {}).get("images") or []:
        if isinstance(image, dict) and image.get("path"):
            return IMAGE_BASE + image["path"] + IMAGE_SUFFIX
    return None


def _discount_pct(price: Optional[float], original: Optional[float]) -> Optional[float]:
    if price is None or original is None or original <= price or original <= 0:
        return None
    return round((original - price) / original * 100, 2)


def _hierarchy(details: Dict[str, Any]) -> Optional[List[str]]:
    crumbs = details.get("breadcrumbs")
    if not isinstance(crumbs, list):
        return None
    labels = [_text(c.get("label")) for c in crumbs if isinstance(c, dict)]
    labels = [label for label in labels if label]
    return labels or None


def counted_sku(sku: Dict[str, Any]) -> bool:
    """Whether the page's `skuCount` counts this SKU: displayable, or not NLA."""
    if (sku.get("availability") or {}).get("displayable"):
        return True
    return (sku.get("inventory") or {}).get("availability") != "NLA"


def sku_count_check(skus: Iterable[Dict[str, Any]], details: Dict[str, Any]) -> Dict[str, Any]:
    """Does the SKU list we read add up to what the page says it holds?

    `skus` are the RAW SKU nodes (the rule needs `availability.displayable`,
    which a SkuRow does not carry). Returned, not raised: the caller decides.
    """
    raw = list(skus)
    counted = sum(1 for sku in raw if counted_sku(sku))
    stated = details.get("skuCount")
    stated = stated if isinstance(stated, int) and not isinstance(stated, bool) else None
    return {
        "rows": len(raw),
        "nla": sum(1 for sku in raw if (sku.get("inventory") or {}).get("availability") == "NLA"),
        "counted": counted,
        "sku_count_stated": stated,
        "closes": stated is not None and counted == stated,
    }


def parse_product(html: str, *, url: Optional[str] = None,
                  category: Optional[str] = None
                  ) -> Tuple[List[SkuRow], Dict[str, Any]]:
    """One product page -> `(rows, facts)`, one row per SKU.

    `facts` carries what a run's sidecar should say about the page: where the
    SKUs came from (`inline` / `compressed`), the sku-count arithmetic and the
    page's own aggregate price range.

    Raises DecodeError when a compressed SKU list is present and unreadable:
    returning zero rows there would be the silent success this module's
    docstring describes.
    """
    state = initial_state(html)
    details = product_details(state)
    if details is None:
        return [], {"subsets_source": "none"}
    subsets, where = subsets_of(details)
    currency = page_currency(state or {})
    slug = str(details["groupId"])
    page_link = url or product_url(slug)
    product_title = _text(details.get("title"))
    hierarchy = _hierarchy(details)
    pip_type = details.get("pipType") or None
    source = "pdp-state" if where == "inline" else "pdp-compressed"

    rows: List[SkuRow] = []
    raw_skus: List[Dict[str, Any]] = []
    for subset in subsets:
        if not isinstance(subset, dict):
            continue
        definitions = subset.get("definitions") or {}
        for sku_id, sku in (definitions.get("skus") or {}).items():
            if not isinstance(sku, dict):
                continue
            raw_skus.append(sku)
            price_node = sku.get("price") or {}
            price = _number(price_node.get("sellingPrice"))
            regular = _number(price_node.get("regularPrice"))
            original = regular if (regular is not None and price is not None
                                   and regular > price) else None
            inventory = sku.get("inventory") or {}
            availability = sku.get("availability") or {}
            properties = sku.get("properties") or {}
            options = _options(sku, definitions)
            sellable = availability.get("sellable")
            rows.append(SkuRow(
                url=page_link,
                sku=str(sku.get("id") or sku_id),
                title=_text(sku.get("name")),
                image_url=_sku_image(sku),
                price=price,
                currency=currency if price is not None else None,
                category=category,
                price_source=source,
                product_id=slug,
                product_title=product_title,
                original_price=original,
                discount_pct=_discount_pct(price, original),
                availability=inventory.get("availability") or None,
                back_ordered_date=inventory.get("backOrderedDate") or None,
                sellable=sellable if isinstance(sellable, bool) else None,
                options=options or None,
                color=_text(properties.get("color")),
                width_in=_number(properties.get("width")),
                depth_in=_number(properties.get("depth")),
                height_in=_number(properties.get("height")),
                subset=_text(subset.get("name")),
                pip_type=pip_type,
                category_hierarchy=hierarchy,
                row_index=len(rows),
            ))
    aggregate = details.get("aggregatePrice") or {}
    facts = {
        "product_id": slug,
        "subsets_source": where,
        "sku_check": sku_count_check(raw_skus, details),
        "aggregate_low_selling": _number(aggregate.get("lowSellingPrice")),
        "aggregate_high_selling": _number(aggregate.get("highSellingPrice")),
        "currency": currency,
    }
    return rows, facts


# ---------------------------------------------------------------------------
# URLs
# ---------------------------------------------------------------------------

_SLUG_RE = re.compile(r"^/products/([a-z0-9][a-z0-9-]*)/?$")
_SHOP_RE = re.compile(r"^/shop/[a-z0-9-]+(?:/[a-z0-9-]+)*/?$")


def supported_host(url: str) -> Tuple[bool, Optional[str]]:
    host = (urlparse(url or "").hostname or "").lower()
    if not host:
        return False, "no host in URL %r" % (url,)
    if host in HOSTS:
        return True, None
    if host in UNSUPPORTED_HOSTS:
        return False, UNSUPPORTED_HOSTS[host]
    return False, "%s is not a www.potterybarn.com URL" % host


def slug_from_url(url: str) -> Optional[str]:
    """The product slug, which IS the product id.

    Never parse a trailing integer out of it: some slugs end in digits
    (`open-box-cline-swivel-counter-stool-14341192`) and some do not
    (`russo-vanity-mirror-mp`). The whole slug is the id — it equals the
    page's `groupId` and the API result's `id`.
    """
    match = _SLUG_RE.match(urlparse(url or "").path)
    return match.group(1) if match else None


def is_category_url(url: str) -> bool:
    return bool(_SHOP_RE.match(urlparse(url or "").path))


def is_department_path(url: str) -> bool:
    """`/shop/<department>/` — one segment under /shop/, a HUB with no grid.

    The 23 hubs among the category sitemap's 795 URLs are exactly these
    (2026-09-24); `/shop/furniture/` was measured to classify `hub` from its
    own page. Used where the page itself cannot be read.
    """
    if not is_category_url(url):
        return False
    return len(urlparse(url).path.strip("/").split("/")) == 2


def category_from_url(url: str) -> Optional[str]:
    """`/shop/furniture/sofa/` -> `furniture/sofa`. A label, not a group id."""
    if not is_category_url(url):
        return None
    return urlparse(url).path[len("/shop/"):].strip("/") or None


def browse_url(group_id: str) -> str:
    return BROWSE_URL.format(group_id=group_id)


# ---------------------------------------------------------------------------
# robots.txt — the committed snapshot, matched the way Google documents it
# ---------------------------------------------------------------------------
#
# `robots.snapshot.txt` is www.potterybarn.com/robots.txt as fetched through a
# US exit on 2026-09-23 (1,264 bytes, re-checked identical 2026-09-24). The
# rules that bite a scraper are `Disallow: /*N=` (every facet URL),
# `Disallow: /shop/*+*+*`, and `Disallow: */apartment-` with its exception
# `Allow: */apartment-furniture/`. The standard library's robotparser does not
# implement `*` or `$`, so a wildcard-aware matcher is written out here: the
# longest matching rule wins, and Allow wins a tie (RFC 9309 §2.2.2).
#
# It governs www.potterybarn.com ONLY. The listing API lives on
# ac.cnstrc.com, which this file does not speak for — see README, "The
# listing API is a third-party host".

# Embedded, not read from disk: an installed wheel does not put a data file
# beside this module, and a matcher that cannot find its rules must not
# quietly allow everything. `robots.snapshot.txt` is the same text for
# people to read; the suite asserts the two are identical.
ROBOTS_SNAPSHOT_TEXT = '# robots.txt - Pottery Barn https://www.potterybarn.com #\nUser-agent: EasouSpider\nDisallow: /\n\nUser-agent: Pinterestbot\ncrawl-delay: 0.2\n\nUser-agent: *\nAllow: /llms.txt\nDisallow: /account/\nDisallow: /checkout/\nDisallow: /shoppingcart/\nDisallow: /services/\nDisallow: /shop_g/\nDisallow: /shop_r/\nDisallow: /products_g/\nDisallow: /products_r/\nDisallow: */minipip\nDisallow: */quicklook\nDisallow: /personalization/\nDisallow: */show-mobile-email-signup\nDisallow: */show-join-email.jsonp\nDisallow: */registry/list.json\nDisallow: /registry/list.json?\nDisallow: */apartment-\nDisallow: /*N=\nDisallow: */order-shipment-tracking\nDisallow: */undefined\nAllow: */apartment-furniture/\nDisallow: /shop/*+*+*\n\n#Sitemaps\nSitemap: https://www.potterybarn.com/netstorage/sitemaps/ecm-sitemap-index.xml\nSitemap: https://www.potterybarn.com/netstorage/sitemaps/facet-sitemap-index.xml\nSitemap: https://www.potterybarn.com/netstorage/sitemaps/shop-sitemap-index.xml\nSitemap: https://www.potterybarn.com/netstorage/sitemaps/store-sitemap-index.xml\nSitemap: https://www.potterybarn.com/netstorage/sitemap-PB-tips-ideas.xml\nSitemap: https://www.potterybarn.com/netstorage/sitemaps/product-sitemap-index.xml\nSitemap: https://www.potterybarn.com/netstorage/sitemaps/thematic-sitemap-index.xml\n'


def _robots_rules(text: str) -> List[Tuple[str, str]]:
    """`[(kind, pattern), ...]` for the `User-agent: *` group."""
    rules: List[Tuple[str, str]] = []
    agents: List[str] = []
    in_rules = False
    for raw in (text or "").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        name, _, value = line.partition(":")
        name, value = name.strip().lower(), value.strip()
        if name == "user-agent":
            if in_rules:
                agents, in_rules = [], False
            agents.append(value)
        elif name in ("allow", "disallow"):
            in_rules = True
            if "*" in agents and value:
                rules.append((name, value))
    return rules


def _robots_pattern(pattern: str):
    anchored = pattern.endswith("$")
    body = pattern[:-1] if anchored else pattern
    regex = "".join(".*" if ch == "*" else re.escape(ch) for ch in body)
    return re.compile("^" + regex + ("$" if anchored else ""))


_ROBOTS_COMPILED = None


def robots_allows(url: str, robots_text: Optional[str] = None) -> bool:
    """Whether the snapshot allows `url` (path + query) for `User-agent: *`.

    Fails CLOSED: rules that parse to nothing refuse every URL, because an
    empty rule set is a broken snapshot, not a permissive site.
    """
    global _ROBOTS_COMPILED
    if robots_text is None:
        if _ROBOTS_COMPILED is None:
            _ROBOTS_COMPILED = [(k, p, _robots_pattern(p))
                                for k, p in _robots_rules(ROBOTS_SNAPSHOT_TEXT)]
        compiled = _ROBOTS_COMPILED
    else:
        compiled = [(k, p, _robots_pattern(p)) for k, p in _robots_rules(robots_text)]
    if not compiled:
        return False
    parts = urlparse(url or "")
    target = (parts.path or "/") + ("?" + parts.query if parts.query else "")
    best: Optional[Tuple[int, str]] = None
    for kind, pattern, regex in compiled:
        if regex.match(target):
            rank = (len(pattern), 1 if kind == "allow" else 0)
            if best is None or rank > (best[0], 1 if best[1] == "allow" else 0):
                best = (len(pattern), kind)
    return best is None or best[1] == "allow"
