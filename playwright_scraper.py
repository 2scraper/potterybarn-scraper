#!/usr/bin/env python3
"""potterybarn.com — Playwright engine (the primary BROWSER engine).

    python3 playwright_scraper.py --mode product \
        --url https://www.potterybarn.com/products/delaney-marble-end-table/
    python3 playwright_scraper.py --mode category --group-id sofa --pages 2

On this site the browser is the fallback and the parity check, not the main
path — see browser_bridge.py for the measurements. With `--cdp-endpoint` (or
POTTERYBARN_CDP_ENDPOINT) it connects ONCE to the Scraping Browser API and
arms `Captcha.setAutoSolve`; without one it launches a local Chromium, which
was measured refused on this site.
"""

from __future__ import annotations

import sys

import browser_bridge
import proxy_pool

# Module level on purpose: the offline suite skips this engine when
# Playwright is absent, and can only tell "absent" from "broken" if the import
# fails here (a rule of this scraper family).
from playwright.sync_api import sync_playwright

CAPTCHA_EVENTS = ("Captcha.solveStarted", "Captcha.solveFinished",
                  "Captcha.solveFailed", "Captcha.detected")


class PlaywrightDriver:
    name = "playwright"

    def __init__(self, args):
        self.args = args
        self.over_cdp = False
        self.autosolve_armed = False
        self.autosolve_events = []
        self.autosolve_notes = []
        self._pw = self._browser = self._context = self._page = self._cdp = None
        self._response = None

    def start(self):
        self._pw = sync_playwright().start()
        if self.args.cdp_endpoint:
            # ONE attempt. A connect that times out holds the profile lock for
            # 25+ minutes, so a retry costs the profile, not a retry.
            try:
                self._browser = self._pw.chromium.connect_over_cdp(
                    self.args.cdp_endpoint, timeout=self.args.timeout * 1000)
            except Exception as exc:
                raise browser_bridge.BridgeError(
                    "could not connect over CDP: %s: %s"
                    % (type(exc).__name__, browser_bridge.mask_text(str(exc)))) from None
            contexts = self._browser.contexts
            self._context = contexts[0] if contexts else self._browser.new_context()
            self.over_cdp = True
        else:
            launch = {"headless": self.args.headless}
            if getattr(self.args, "browser_path", None):
                launch["executable_path"] = self.args.browser_path
            pool = proxy_pool.from_args(self.args)
            if pool:
                # Playwright's own fields, never `--proxy-server=` on argv.
                launch["proxy"] = proxy_pool.to_playwright(pool.current)
            self._browser = self._pw.chromium.launch(**launch)
            # No user agent and no fingerprint: a hardcoded UA drifts from the
            # installed Chromium (a rule of this scraper family).
            self._context = self._browser.new_context(locale="en-US")
        self._page = self._context.new_page()
        self._page.set_default_timeout(self.args.timeout * 1000)
        if self.over_cdp:
            self._arm_autosolve()

    def _arm_autosolve(self):
        """Rung 1a. Every failure is a WARNING, recorded in the sidecar.

        `Captcha.enable` is not sent: the endpoint answers it with a protocol
        error (homedepot-scraper, 2026-09-23) and `setAutoSolve` alone arms
        the solver — accepted on every session measured here, 2026-09-24.
        """
        if self.args.solve_captcha == "never":
            return
        try:
            self._cdp = self._context.new_cdp_session(self._page)
        except Exception as exc:
            self.autosolve_notes.append("cdp session: %s"
                                        % browser_bridge.mask_text(str(exc))[:100])
            return
        for event in CAPTCHA_EVENTS:
            try:
                self._cdp.on(event, self._on_event(event))
            except Exception:
                pass
        try:
            self._cdp.send("Captcha.setAutoSolve", {"autoSolve": True})
            self.autosolve_armed = True
        except Exception as exc:
            self.autosolve_notes.append("Captcha.setAutoSolve unavailable: %s"
                                        % str(exc).splitlines()[0][:100])

    def _on_event(self, name):
        def handler(payload):
            self.autosolve_events.append({"event": name, "payload": str(payload)[:300]})
            print("[captcha] %s %s" % (name, str(payload)[:160]), file=sys.stderr)
        return handler

    def navigate(self, url):
        # `commit`: the navigation returns as soon as the response starts,
        # and the bridge reads that response's own BODY (the DOM loses the
        # state on hydration anyway). Waiting for `load` never ended within
        # 60 s on 2026-09-24 and left the Scraping Browser profile locked;
        # `domcontentloaded` still timed out in 2 of 10 instrumented runs on
        # 2026-09-24 and 1 of 10 on 2026-10-01, each of which risks the lock.
        response = self._page.goto(url, wait_until="commit",
                                   timeout=self.args.timeout * 1000)
        self._response = response
        return response.status if response else None

    def response_body(self):
        return self._response.text() if self._response is not None else None

    def state_json(self):
        return self._page.evaluate(
            "() => JSON.stringify(window.__INITIAL_STATE__ || null)")

    def content(self):
        try:
            return self._page.content()
        except Exception:
            return ""

    def sleep(self, ms):
        self._page.wait_for_timeout(ms)

    def inject_token(self, token):
        import captcha_solver
        return bool(self._page.evaluate(captcha_solver.INJECT_TOKEN_JS, token))

    def stop(self):
        for closer in (self._page, self._context, self._browser):
            try:
                if closer is not None:
                    closer.close()
            except Exception:
                pass
        if self._pw is not None:
            self._pw.stop()


def main(argv=None) -> int:
    parser = browser_bridge.build_parser(
        "Scrape potterybarn.com with Playwright (the primary browser engine).")
    args = browser_bridge.parse_args(parser, argv)
    return browser_bridge.run(args, PlaywrightDriver(args))


if __name__ == "__main__":
    sys.exit(main())
