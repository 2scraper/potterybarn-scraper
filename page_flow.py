"""page_flow.py — what each page state means for a run, as DATA.

Every transport reaches the retry/rotate/solve/blocked decision through this
one table, so an engine cannot quietly disagree with its twins about whether
a page is worth retrying or paying for (a rule of this scraper family).

The states come from product_parser.detect_page_state (and, for the listing
API, api_scraper's own classifier):

    product        a product page with its `productDetails` — parse it
    listing        a leaf category page — only its key/group/currency are read
    api            a Constructor browse response — parse it
    hub            a department page with no grid (`/shop/furniture/`). A
                   correct answer to a URL that was never a listing: not
                   retried, not paid for, not "blocked"
    empty          a served page with nothing this repo reads on it
    blocked        the branded 403 — a decision about this exit and this
                   client, so rotate, never solve
    geo_redirect   sent to potterybarn.co.uk — a refusal of this exit
                   specifically; a fresh US session is the answer
    rate_limited   HTTP 429 from the listing API — back off, then retry
    captcha        a rendered challenge widget — the paid rung. Never met on
                   this site: 0 Captcha events over every browser session
                   measured 2026-09-24
    transport_error  our own plumbing (timeout, reset, TLS error at the
                   exit). Retry the same exit; never exit 3 for it

No JavaScript crosses this boundary (a rule of this scraper family).
"""

from typing import Dict

STATE_POLICY: Dict[str, Dict[str, bool]] = {
    "product": {"retry": False, "solve": False, "blocked": False, "rotate": False},
    "listing": {"retry": False, "solve": False, "blocked": False, "rotate": False},
    "api": {"retry": False, "solve": False, "blocked": False, "rotate": False},
    "hub": {"retry": False, "solve": False, "blocked": False, "rotate": False},
    "empty": {"retry": False, "solve": False, "blocked": False, "rotate": False},
    "blocked": {"retry": True, "solve": False, "blocked": True, "rotate": True},
    "geo_redirect": {"retry": True, "solve": False, "blocked": True, "rotate": True},
    "rate_limited": {"retry": True, "solve": False, "blocked": True, "rotate": False},
    "captcha": {"retry": True, "solve": True, "blocked": True, "rotate": False},
    "transport_error": {"retry": True, "solve": False, "blocked": False, "rotate": False},
}


def _policy(state: str) -> Dict[str, bool]:
    return STATE_POLICY.get(state, {})


def should_retry(state: str) -> bool:
    return _policy(state).get("retry", False)


def should_rotate_exit(state: str) -> bool:
    """A refusal wants a different address; a timeout wants the same one again."""
    return _policy(state).get("rotate", False)


def should_solve(state: str) -> bool:
    return _policy(state).get("solve", False)


def counts_as_blocked(state: str) -> bool:
    return _policy(state).get("blocked", False)


# How long a browser engine waits for a product page's state to appear. A
# product page is server-rendered: the whole `__INITIAL_STATE__` is in the
# first response (measured: 733 KB over curl with no script run at all), so
# this is a ceiling for a slow exit, not a hydration wait.
CONTENT_TIMEOUT_MS = {"category": 15000, "product": 20000}


def content_timeout_ms(mode: str) -> int:
    return CONTENT_TIMEOUT_MS.get(mode, 20000)
