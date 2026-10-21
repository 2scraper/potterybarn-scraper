"""output_writer.py — row model, JSON/CSV writers, exit codes, run metadata.

Two kinds of row, and why
-------------------------
The same catalogue answers two different questions here, at two grains:

    --mode category   one row per PRODUCT   `Product`   (the listing API)
    --mode product    one row per SKU       `SkuRow`    (a product page)

A listing result is a product with a price RANGE (`lowestPrice` ..
`highestPrice`); a product page lists every purchasable SKU with its own
price and stock. They are not the same fact at different resolutions, and the
obvious alternative — always one row per SKU, fetching every product page —
was measured before it was rejected: one sofa (PB Comfort Modern Square Arm,
2026-09-24) holds 4,843 SKUs, so the 434-product sofa category alone would be
on the order of a million rows and 434 product-page fetches through a US
proxy, where the listing answers in 19 API calls from anywhere.

So the mode picks the grain, the sidecar records the mode, and diff_runs.py
REFUSES to compare runs of different modes (amazon-scraper in this family
does the same for its `Review` rows).

`sku` is the row key in both, and it is a different thing in each
------------------------------------------------------------------
* In a `Product` row, `sku` is the listing result's id: the product slug,
  or — for a SUB-GROUP result, a slice of a product by finish — the slice's
  own id, with the product's slug in `product_id` and the slice in
  `sub_group_id`.
* In a `SkuRow`, `sku` is the SKU id (`2235076`) and the product id is in
  `product_id`.

Both classes carry `product_id`, so a consumer joins a listing row to its SKU
rows on `product_id` and never on `sku`. The SKU the listing API names as the
product's lead (`leaderSku`) is kept as `leader_sku` and is never written into
`sku`: letting a SKU id stand in for a product id is how two different
questions get one wrong answer.

Columns that are NOT here, and the measurement behind each
----------------------------------------------------------
- `rating` / `review_count`: the product page's `reviews` node is a bare
  reference (`{"type": "GROUP_REVIEWS", "id": <slug>}`) and the numbers are
  fetched after load; the browse API carries none. 0 of 3 product pages and
  0 of 24 listing results carried a rating key on 2026-09-24. A column that is
  null on every row should not exist.
- `description`: present in the browse API as marketing copy; it would
  dominate every CSV row of a price-monitoring run.
- `swatchPrices` (browse API): a per-SWATCH price range for the first ten
  fabric swatches only. Neither a product fact nor a SKU fact; the SKU rows
  from `--mode product` are the per-variant answer.
"""

import csv
import hashlib
import json
from dataclasses import dataclass, asdict, field, fields
from datetime import datetime, timezone
from typing import Optional, List, Set, Sequence, Any, Type, Dict

import proxy_meter

# One storefront. Pottery Barn UK is a separate store and is refused by
# product_parser.supported_host, so `source` is constant.
SOURCE_DEFAULT = "potterybarn.com"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class Product:
    """One listing result: a product and its price RANGE."""
    source: str = SOURCE_DEFAULT
    scraped_at: str = field(default_factory=_now)
    url: str = ""
    # The listing result's id: the product slug, or a sub-group's own id.
    sku: Optional[str] = None
    title: Optional[str] = None
    image_url: Optional[str] = None
    # `lowestPrice` — the lowest SELLING price over the product's SKUs, the
    # "from" figure the grid prints.
    price: Optional[float] = None
    # From the listing page the API key was read from
    # (`shop.currencyData.selectedCurrency`), or from the key's measured
    # market when no page was read. Never defaulted; see
    # product_parser.KNOWN_KEY_CURRENCY.
    currency: Optional[str] = None
    category: Optional[str] = None
    # "constructor-browse" — the only listing source. There is no DOM price
    # path: the served listing page holds no tiles at all.
    price_source: Optional[str] = None

    # ---- Pottery Barn specific, appended so the family prefix is stable ----
    # The slug in the URL's PATH — the product page's `groupId`. Equal to
    # `sku` except on a sub-group row (see `sub_group_id`).
    product_id: Optional[str] = None
    # Set when this result is a SLICE of a product rather than the product:
    # the API's `id` for it, also carried as `?subGroupId=` in `url`. 15 of
    # 144 results over three categories, 2026-09-24.
    sub_group_id: Optional[str] = None
    # `highestPrice` — the highest SELLING price over the product's SKUs.
    price_max: Optional[float] = None
    # `regularPriceMin` / `regularPriceMax`. Published by the API only on a
    # product with a markdown, so None means "no regular price stated", which
    # on this API means none was higher than the selling one.
    original_price: Optional[float] = None
    original_price_max: Optional[float] = None
    # The site's own `maxDiscountPercent`, kept under the site's meaning: the
    # largest discount on ANY of the product's SKUs. Not computed from
    # price/original_price, because those two minimums need not belong to
    # the same SKU.
    max_discount_pct: Optional[float] = None
    # The SKU the API names as the product's lead. NOT the row key.
    leader_sku: Optional[str] = None
    pip_type: Optional[str] = None      # e.g. "guided" (a configurator)
    price_type: Optional[str] = None    # `productPriceType`
    flags: Optional[List[str]] = None   # e.g. ["freePers", "inHome"]
    page: Optional[int] = None
    row_index: Optional[int] = None


@dataclass
class SkuRow:
    """One purchasable SKU from a product page."""
    source: str = SOURCE_DEFAULT
    scraped_at: str = field(default_factory=_now)
    url: str = ""
    # The SKU id (`2235076`).
    sku: Optional[str] = None
    # The SKU's own name ("Delaney 14" Marble End Table, Bronze").
    title: Optional[str] = None
    image_url: Optional[str] = None
    price: Optional[float] = None          # `price.sellingPrice`
    # The page's own `config.pricing.currencyData.selectedCurrency`.
    currency: Optional[str] = None
    category: Optional[str] = None
    # "pdp-state"      — the SKU list was inline in `productDetails.subsets`
    # "pdp-compressed" — it came from `subsetsCompressedValue` (Brotli), the
    #                    only place a GUIDED product's SKUs exist in the page
    price_source: Optional[str] = None

    # ---- Pottery Barn specific --------------------------------------------
    product_id: Optional[str] = None       # the slug; joins to a Product row
    product_title: Optional[str] = None
    # `price.regularPrice`, kept only when it is ABOVE the selling price.
    original_price: Optional[float] = None
    # Computed from price/original_price of THIS SKU, never read from a badge.
    discount_pct: Optional[float] = None
    # `inventory.availability`: ON_HAND, BACK_ORDERED or NLA (no longer
    # available) on every SKU measured, 2026-09-24 — 6,214 of 6,214 across
    # three product pages carried one.
    availability: Optional[str] = None
    back_ordered_date: Optional[str] = None
    sellable: Optional[bool] = None
    # The SKU's own selections, as the page names them:
    # {"Finish": "Brass & Banswara Marble", "Quantity": "Set of 2"}.
    options: Optional[Dict[str, str]] = None
    color: Optional[str] = None
    width_in: Optional[float] = None
    depth_in: Optional[float] = None
    height_in: Optional[float] = None
    subset: Optional[str] = None
    pip_type: Optional[str] = None         # SIMPLE_PIP, GUIDED_PIP, ...
    category_hierarchy: Optional[List[str]] = None
    page: Optional[int] = None
    row_index: Optional[int] = None


# The family prefix both classes share, byte-identical and in order.
FAMILY_PREFIX = ("source", "scraped_at", "url", "sku", "title", "image_url",
                 "price", "currency", "category", "price_source")

ROW_CLASS_BY_MODE = {
    "category": Product,
    "product": SkuRow,
}

# Modes whose rows are unique by `sku`, and therefore safe to dedupe on it and
# to hand to diff_runs.py. Both qualify — with different meanings of `sku`,
# which is why diff_runs.py also refuses to compare across them.
UNIQUE_BY_SKU_MODES = ("category", "product")


def dedupe_by_key(rows: Sequence[Any], seen: Set[str], key: str = "sku") -> List[Any]:
    """Drop rows whose key already appeared earlier in this same run.

    `seen` is mutated in place, so callers thread one set across pages. A row
    with no key is always kept: dropping it would be a silent data loss
    rather than a duplicate removal.
    """
    fresh = []
    for r in rows:
        val = getattr(r, key, None)
        if val is None or val not in seen:
            if val is not None:
                seen.add(val)
            fresh.append(r)
    return fresh


def dedupe_by_sku(rows: Sequence[Any], seen: Set[str]) -> List[Any]:
    return dedupe_by_key(rows, seen, key="sku")


# CSV cannot hold a list or a dict. Lists are joined with " | " so a cell
# splits back on the same separator; a dict (`options`) is written as JSON so
# a value containing " | " or ": " cannot corrupt it. The JSON output keeps
# the real structures.
LIST_CSV_SEPARATOR = " | "


def _csv_value(v: Any) -> Any:
    if isinstance(v, dict):
        return json.dumps(v, ensure_ascii=False, sort_keys=True)
    if isinstance(v, (list, tuple)):
        return LIST_CSV_SEPARATOR.join(str(x) for x in v)
    return v


# A spreadsheet EXECUTES a cell that begins with one of these, and every
# string in these rows was written by the merchant or the listing API. The CSV
# gets a leading apostrophe on such a cell (spreadsheets show it as text);
# JSON keeps the site's bytes. Only STRINGS are escaped — a negative number
# stays a number, or every sum a consumer writes over the column breaks — and
# the check runs AFTER the list/dict serialisation, because a joined list is
# text too. The count goes into the sidecar as `csv_cells_escaped`, so the
# difference between the two outputs is declared rather than discovered.
CSV_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r", "\n")


def _csv_cell(v: Any):
    """(cell, escaped?) for one value, after serialisation."""
    v = _csv_value(v)
    if isinstance(v, str) and v.startswith(CSV_FORMULA_PREFIXES):
        return "'" + v, True
    return v, False


def _atomic_write(path: str, text: str) -> None:
    """Write `text` to `path` so a crash cannot truncate what was there.

    Temp file in the SAME directory (so `os.replace` stays on one filesystem
    and is atomic), flush, fsync, then replace. `open(path, "w")` truncates
    the previous good file the instant it is called.
    """
    import os
    import tempfile
    directory = os.path.dirname(os.path.abspath(path)) or "."
    handle_fd, tmp = tempfile.mkstemp(dir=directory, prefix=".tmp-",
                                      suffix=os.path.basename(path))
    try:
        with os.fdopen(handle_fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        # mkstemp creates the file 0600 and os.replace keeps that mode, so
        # without this every output was readable by its owner only. A file
        # that already exists keeps ITS mode; a new one gets what open()
        # would have given it under the caller's umask — never a flat 0644,
        # which would narrow a 0664 under umask 002.
        os.chmod(tmp, _target_mode(path))
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _target_mode(path: str) -> int:
    import os
    import stat
    try:
        return stat.S_IMODE(os.stat(path).st_mode)
    except FileNotFoundError:
        pass
    umask = os.umask(0)
    os.umask(umask)
    return 0o666 & ~umask


def write_json(rows: Sequence[Any], path: str) -> None:
    payload = json.dumps([asdict(r) for r in rows], indent=2, ensure_ascii=False)
    _atomic_write(path, payload + "\n")


def write_csv(rows: Sequence[Any], path: str, row_cls: Type = Product) -> int:
    """CSV with the same field order as JSON. An EMPTY result still carries
    its header, so a consumer reads a table with no rows instead of failing
    on a zero-byte file. Returns how many cells were formula-escaped."""
    import io
    names = [f.name for f in fields(row_cls)]
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=names, extrasaction="ignore")
    writer.writeheader()
    escaped = 0
    for row in rows:
        out = {}
        for k, v in asdict(row).items():
            out[k], hit = _csv_cell(v)
            escaped += hit
        writer.writerow(out)
    _atomic_write(path, buffer.getvalue())
    return escaped


EXIT_CRASH = 1
EXIT_USAGE = 2
# The SITE refused us: a 403, a geo-redirect to the UK store, a 429 from the
# listing API.
EXIT_BLOCKED = 3
EXIT_NO_PRODUCTS = 4
# OUR plumbing failed and the site never answered: a timeout, a dropped CDP
# socket, a locked Scraping Browser profile, a proxy 407. Reporting any of
# those as 3 sends the reader to the anti-bot problem when the answer is
# somewhere else entirely. Also: zero rows from a run that never
# obtained its content (a failed page, a served page whose stated rows did not
# parse) — "nothing listed" would be a claim the run cannot make.
EXIT_REMOTE_API_ERROR = 5
EXIT_PARTIAL = 6


class UsageError(SystemExit):
    """Bad usage — exit 2, with the reason on stderr.

    `raise SystemExit("refusing ...")` exits with code 1, which the family
    contract reserves for a crash: a URL on the UK store or a robots-
    disallowed page read as if the scraper had fallen over (measured
    2026-10-01, `http_scraper.py --url https://www.potterybarn.co.uk/...`).
    """

    def __init__(self, message: str):
        import sys
        print(message, file=sys.stderr)
        super().__init__(EXIT_USAGE)


class RemoteAPIError(RuntimeError):
    """A transport or 2Captcha-product call failed on its own terms — not the
    target site refusing a page. Mapped to EXIT_REMOTE_API_ERROR."""


def write_run_meta(out_prefix: str, meta: dict, *, suffix: str = ".meta.json") -> str:
    path = f"{out_prefix}{suffix}"
    _atomic_write(path, json.dumps(meta, ensure_ascii=False, indent=2) + "\n")
    print(f"[+] Wrote run metadata -> {path} (status={meta.get('status')})")
    return path


ATTEMPT_META_SUFFIX = ".latest_attempt.meta.json"


def write_attempt_meta(out_prefix: str, meta: dict) -> str:
    """Metadata for EVERY attempt, successful or not.

    `.meta.json` describes the dataset beside it and is written only with
    one, so a blocked run leaves yesterday's `status: complete` sidecar beside
    yesterday's data. This file answers the other question — what happened
    the last time we ran — and is written unconditionally.
    """
    return write_run_meta(out_prefix, meta, suffix=ATTEMPT_META_SUFFIX)



def write_failed_attempt(out_prefix: str, *, stop_reason: str, mode: str,
                         start_url: Optional[str], pages_requested: int,
                         transport_error: str) -> str:
    """Record a run that ended before `finish_run` could, e.g. because the
    proxy refused its credential on the first request. Only the attempt
    sidecar is written: `.meta.json` and the data beside it stay as they were.
    """
    import uuid
    meta = run_meta(status="failed", stop_reason=stop_reason,
                    pages_requested=pages_requested, pages_completed=0,
                    start_url=start_url, final_url=start_url, products=0,
                    mode=mode)
    meta.update(run_id=uuid.uuid4().hex[:12], blocked=False,
                transport_error=transport_error, data_updated=False)
    meta.update(proxy_meter.sidecar())  # proxy_bytes / proxy_budget_bytes / proxy_budget_exceeded
    path = write_attempt_meta(out_prefix, meta)
    proxy_meter.report()
    return path


def schema_version(row_cls: Type = Product) -> str:
    names = ",".join(f.name for f in fields(row_cls))
    return hashlib.sha256(names.encode()).hexdigest()[:12]


def run_meta(status: str, stop_reason: str, pages_requested: int,
             pages_completed: int, start_url: str, final_url: str,
             products: int, pages_failed: Optional[List[int]] = None,
             mode: str = "category", source: str = SOURCE_DEFAULT,
             scope: Optional[dict] = None) -> dict:
    row_cls = ROW_CLASS_BY_MODE.get(mode, Product)
    return {
        "source": source,
        "mode": mode,
        "row_class": row_cls.__name__,
        "schema_version": schema_version(row_cls),
        "status": status,
        "stop_reason": stop_reason,
        "pages_requested": pages_requested,
        "pages_completed": pages_completed,
        "pages_failed": pages_failed or [],
        "products": products,
        "scope": scope or {},
        "start_url": start_url,
        "final_url": final_url,
        "finished_at": _now(),
    }


def save(rows: Sequence[Any], out_prefix: str, fmt: str,
         allow_empty: bool = False, row_cls: Type = Product,
         stats: Optional[dict] = None) -> int:
    """Write JSON/CSV and return an exit code. Zero rows writes NOTHING unless
    `allow_empty` — a run that finds nothing must not replace yesterday's good
    output with an empty file. `stats`, when given, receives
    `csv_cells_escaped`."""
    if not rows and not allow_empty:
        print(f"[!] 0 rows — refusing to write {out_prefix}.json/.csv, so an "
              f"earlier good result isn't overwritten with an empty one. "
              f"Pass --allow-empty if an empty result is the expected answer.")
        return EXIT_NO_PRODUCTS
    if fmt in ("json", "both"):
        write_json(rows, f"{out_prefix}.json")
        print(f"[+] Saved {len(rows)} row(s) -> {out_prefix}.json")
    if fmt in ("csv", "both"):
        escaped = write_csv(rows, f"{out_prefix}.csv", row_cls=row_cls)
        if stats is not None:
            stats["csv_cells_escaped"] = escaped
        print(f"[+] Saved {len(rows)} row(s) -> {out_prefix}.csv")
    return 0 if rows else EXIT_NO_PRODUCTS


COMPLETE_STOP_REASONS = ("completed", "pagination_exhausted", "no_new_products",
                         "single_page_mode")

# A run with ZERO rows exits 4 ("nothing listed") only when it stopped for one
# of the reasons above: the site answered and the answer was empty. Every
# other reason — a failed page, a served page whose stated rows did not
# parse, a reason nobody has named yet — means the content was never
# obtained, which is exit 5. Keyed on the COMPLETE list rather than on a list
# of failures, because a failure list cannot cover a reason added later and
# would let it fall through to "the catalogue is empty".


def finish_run(rows: Sequence[Any], out_prefix: str, fmt: str,
               allow_empty: bool, *, blocked: bool, stop_reason: str,
               pages_requested: int, pages_completed: int,
               start_url: str, final_url: str,
               pages_failed: Optional[List[int]] = None,
               mode: str = "category", source: str = SOURCE_DEFAULT,
               scope: Optional[dict] = None,
               extra: Optional[dict] = None,
               transport_error: bool = False) -> int:
    """Write output + both sidecars; return the exit code.

    Shared by every transport, so the status/exit mapping cannot drift
    between them. `transport_error` means our own plumbing failed before the
    site answered — exit 5, and never 3.
    """
    import uuid
    if proxy_meter.exceeded():
        # The run stopped itself at --proxy-budget-mb: rows kept -> 6, none
        # -> 5, whatever else the pages it did fetch said.
        stop_reason, blocked, transport_error = proxy_meter.STOP_REASON, False, True
    complete = stop_reason in COMPLETE_STOP_REASONS
    row_cls = ROW_CLASS_BY_MODE.get(mode, Product)
    stats: dict = {}
    rc = save(rows, out_prefix, fmt, allow_empty=allow_empty, row_cls=row_cls,
              stats=stats)
    wrote_output = bool(rows) or allow_empty

    status = "complete" if (rows and complete) else ("partial" if rows else "failed")
    base = run_meta(
        status=status, stop_reason=stop_reason,
        pages_requested=pages_requested, pages_completed=pages_completed,
        pages_failed=pages_failed, mode=mode, source=source, scope=scope,
        start_url=start_url, final_url=final_url, products=len(rows))
    base["run_id"] = uuid.uuid4().hex[:12]
    base["blocked"] = bool(blocked)
    if "csv_cells_escaped" in stats:
        base["csv_cells_escaped"] = stats["csv_cells_escaped"]
    base.update(proxy_meter.sidecar())
    if extra:
        base["transport_facts"] = extra

    if wrote_output:
        write_run_meta(out_prefix, dict(base, data_updated=True))
    write_attempt_meta(out_prefix, dict(base, data_updated=bool(wrote_output)))
    proxy_meter.report()

    if not rows:
        if transport_error:
            return EXIT_REMOTE_API_ERROR
        if blocked:
            return EXIT_BLOCKED
        if not complete:
            print(f"[!] 0 rows and the run did not finish ({stop_reason}): the "
                  f"content was never obtained, which is not the same as an "
                  f"empty listing — see {out_prefix}{ATTEMPT_META_SUFFIX}.")
            return EXIT_REMOTE_API_ERROR
        return rc
    if not complete:
        print(f"[!] Partial run: stopped after {pages_completed} of "
              f"{pages_requested} page(s) ({stop_reason}). The output holds "
              f"what was gathered, but it is NOT a complete view — see "
              f"{out_prefix}.meta.json.")
        return EXIT_PARTIAL
    return rc
