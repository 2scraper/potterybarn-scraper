#!/usr/bin/env python3
"""potterybarn.com — Selenium engine.

Same flags, same exit codes, same sidecar as `playwright_scraper.py`. Two
limits are real and stated rather than left to be discovered:

* **Selenium cannot use an authenticated remote CDP endpoint.**
  chromedriver's `debuggerAddress` takes a bare host:port with nowhere to put
  a password, so `--cdp-endpoint` is REFUSED here rather than ignored.
* **`--proxy-server` cannot authenticate.** A `user:pass@` exit is stripped
  to host:port and the warning names the bare address actually handed over.

Selenium has no response object, so it passes no HTTP status; the page
classifier stays correct without one (the branded refusal is recognised by
its own text).
"""

from __future__ import annotations

import sys

import browser_bridge
import proxy_pool

# Module level — see playwright_scraper.py.
from selenium import webdriver
from selenium.webdriver.chrome.options import Options


class SeleniumDriver:
    name = "selenium"

    def __init__(self, args):
        self.args = args
        self.over_cdp = False
        self._driver = None

    def start(self):
        if self.args.cdp_endpoint:
            raise browser_bridge.BridgeError(
                "selenium cannot use --cdp-endpoint: chromedriver's "
                "debuggerAddress takes a bare host:port and has nowhere to put "
                "the endpoint's password. Use playwright_scraper.py or "
                "puppeteer_scraper.py for the Scraping Browser API.")
        options = Options()
        if self.args.headless:
            options.add_argument("--headless=new")
        # `eager` = return at DOMContentLoaded. Selenium's default waits for
        # the LOAD event, which on this storefront did not fire within 60 s
        # (measured through Playwright, 2026-09-24), so every product page
        # would end in a page-load timeout. Not verified live through
        # Selenium itself: it cannot authenticate the proxy product pages need.
        options.page_load_strategy = "eager"
        options.add_argument("--lang=en-US")
        if getattr(self.args, "browser_path", None):
            options.binary_location = self.args.browser_path
        pool = proxy_pool.from_args(self.args)
        if pool:
            bare, credentials = proxy_pool.split_credentials(pool.current)
            bare = bare or pool.current
            if credentials:
                print("[!] selenium cannot authenticate a proxy. The credential "
                      "in POTTERYBARN_PROXY has been STRIPPED and Chrome is "
                      "being given %s with no username or password." % bare,
                      file=sys.stderr)
            options.add_argument("--proxy-server=%s" % bare)
        self._driver = webdriver.Chrome(options=options)
        self._driver.set_page_load_timeout(self.args.timeout)

    def navigate(self, url):
        self._driver.get(url)
        return None

    def state_json(self):
        # No response object in Selenium, so the live JS state is the only
        # route to a product page's data once the DOM has hydrated.
        return self._driver.execute_script(
            "return JSON.stringify(window.__INITIAL_STATE__ || null);")

    def content(self):
        try:
            return self._driver.page_source
        except Exception:
            return ""

    def sleep(self, ms):
        import time
        time.sleep(ms / 1000.0)

    def inject_token(self, token):
        import captcha_solver
        # Selenium's execute_script takes a function BODY, not `(t) => ...`.
        return bool(self._driver.execute_script(
            "return (%s)(arguments[0]);" % captcha_solver.INJECT_TOKEN_JS.strip(), token))

    def stop(self):
        if self._driver is not None:
            try:
                self._driver.quit()
            except Exception:
                pass


def main(argv=None) -> int:
    parser = browser_bridge.build_parser("Scrape potterybarn.com with Selenium.")
    args = browser_bridge.parse_args(parser, argv)
    return browser_bridge.run(args, SeleniumDriver(args))


if __name__ == "__main__":
    sys.exit(main())
