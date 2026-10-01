#!/usr/bin/env python3
"""potterybarn.com — pyppeteer engine.

Behaves identically to `playwright_scraper.py`: same flags, same exit codes,
same sidecar. **pyppeteer is effectively unmaintained** (its own README
points at Playwright) and is kept for parity. Unlike Selenium it CAN use an
authenticated Scraping Browser endpoint: `browserWSEndpoint` takes the full
`ws://user:pass@host:port`.

It cannot authenticate a proxy on a current Chrome: `page.authenticate` rides
on `Network.setRequestInterception`, which Chrome 154 no longer has
(2026-10-01). That is reported as exit 5 with the reason.

On Apple silicon pass `--browser-path` (e.g. the system Google Chrome): the
Chromium pyppeteer downloads is an x86_64 build, and under Rosetta it did not
come up within pyppeteer's 30 s launch window (2026-10-01).

It does not arm `Captcha.setAutoSolve`: that is wired and measured on the
Playwright engine only, and nothing on this site has ever needed it.
"""

from __future__ import annotations

import asyncio
import sys

import browser_bridge
import proxy_pool

# Module level — see playwright_scraper.py.
from pyppeteer import connect, launch


class PuppeteerDriver:
    name = "pyppeteer"

    def __init__(self, args):
        self.args = args
        self.over_cdp = False
        self._loop = self._browser = self._page = None
        self._response = None

    def _run(self, coro):
        return self._loop.run_until_complete(coro)

    def start(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        if self.args.cdp_endpoint:
            try:
                self._browser = self._run(connect(browserWSEndpoint=self.args.cdp_endpoint))
            except Exception as exc:
                raise browser_bridge.BridgeError(
                    "could not connect over CDP: %s: %s"
                    % (type(exc).__name__, browser_bridge.mask_text(str(exc)))) from None
            self.over_cdp = True
            self._page = self._run(self._browser.newPage())
            return
        options = {"headless": self.args.headless, "args": ["--lang=en-US"]}
        if getattr(self.args, "browser_path", None):
            # pyppeteer downloads an x86_64 Chromium even on Apple silicon,
            # and it did not start under Rosetta (2026-10-01).
            options["executablePath"] = self.args.browser_path
        pool = proxy_pool.from_args(self.args)
        credentials = None
        if pool:
            bare, user, password = _split(pool.current)
            # The bare host:port only; the credential goes through
            # page.authenticate below, never on the browser's command line.
            options["args"].append("--proxy-server=%s" % bare)
            if user:
                credentials = {"username": user, "password": password}
        self._browser = self._run(launch(**options))
        self._page = self._run(self._browser.newPage())
        if credentials:
            try:
                self._run(self._page.authenticate(credentials))
            except Exception as exc:  # noqa: BLE001
                # pyppeteer authenticates through Network.setRequestInterception,
                # which current Chrome no longer implements (measured
                # 2026-10-01, Chrome 154: "'Network.setRequestInterception'
                # wasn't found"). The site was never reached: exit 5, and say
                # why instead of a protocol error.
                raise browser_bridge.BridgeError(
                    "pyppeteer cannot authenticate the proxy with this browser "
                    "(%s). Use playwright_scraper.py, or http_scraper.py for "
                    "product pages." % browser_bridge.mask_text(str(exc))[:120]) from None

    def navigate(self, url):
        # pyppeteer has no `commit` wait; DOMContentLoaded is the earliest.
        response = self._run(self._page.goto(
            url, {"waitUntil": "domcontentloaded", "timeout": self.args.timeout * 1000}))
        self._response = response
        return response.status if response else None

    def response_body(self):
        return self._run(self._response.text()) if self._response is not None else None

    def state_json(self):
        return self._run(self._page.evaluate(
            "() => JSON.stringify(window.__INITIAL_STATE__ || null)"))

    def content(self):
        try:
            return self._run(self._page.content())
        except Exception:
            return ""

    def sleep(self, ms):
        self._run(asyncio.sleep(ms / 1000.0))

    def inject_token(self, token):
        import captcha_solver
        return bool(self._run(self._page.evaluate(captcha_solver.INJECT_TOKEN_JS, token)))

    def stop(self):
        try:
            if self._browser is not None:
                if self.over_cdp:
                    self._run(self._browser.disconnect())
                else:
                    self._run(self._browser.close())
        except Exception:
            pass
        finally:
            if self._loop is not None:
                self._loop.close()


def _split(url):
    """`(scheme://host:port, user, password)` for a proxy URL."""
    from urllib.parse import urlparse
    parts = urlparse(url or "")
    host = parts.hostname or ""
    if parts.port:
        host = "%s:%d" % (host, parts.port)
    return "%s://%s" % (parts.scheme or "http", host), parts.username, parts.password


def main(argv=None) -> int:
    parser = browser_bridge.build_parser("Scrape potterybarn.com with pyppeteer.")
    args = browser_bridge.parse_args(parser, argv)
    return browser_bridge.run(args, PuppeteerDriver(args))


if __name__ == "__main__":
    sys.exit(main())
