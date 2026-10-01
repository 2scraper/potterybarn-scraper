#!/usr/bin/env python3
"""Paired transport trials: the same fresh exit, the same URL, four clients.

Each trial mints ONE fresh session-pinned US exit and puts every local client
through it back to back, so the comparison is not confounded by which address
each client happened to get:

    A  system curl, HTTP/2       the measured header set
    B  python-requests           the same headers
    C  local Chromium            Playwright's own, headless
    D  Scraping Browser          over CDP, `Captcha.setAutoSolve` armed

D cannot use exit A's address — the remote browser brings its own — so its
row records the exit it reports instead (`--cdp-exit` visits ipinfo.io after
the target to find out). It is ONE connection per trial, closed in `finally`.

What this script deliberately does NOT do: call `GET /json/version` to see
whether the profile is free. That check TAKES the profile lock (measured on
homedepot-scraper, 2026-09-23: three profiles reported free and a connect
seconds later failed `profile_locked` on the one just checked). The copy of
this script this repo was bootstrapped from did exactly that before every
trial. A connect that fails is recorded and ends the CDP arm for the rest of
the run instead: after an abnormal end the profile stays held for a long
time, and every further attempt re-takes it.

    python3 tools/transport_trials.py --trials 6
    python3 tools/transport_trials.py --trials 6 --no-cdp
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

import env_config  # noqa: E402
import http_scraper  # noqa: E402
import product_parser as P  # noqa: E402
import proxy_pool  # noqa: E402

from playwright.sync_api import sync_playwright  # noqa: E402

URL = "https://www.potterybarn.com/products/delaney-marble-end-table/"
SETTLE_MS = 4000


def now():
    return datetime.now(timezone.utc).isoformat()


def _summary(client, status, html, url, started, **extra):
    state = P.detect_page_state(html, status=status, url=url)
    rows = 0
    if state == "product":
        try:
            rows = len(P.parse_product(html, url=url)[0])
        except P.DecodeError:
            rows = -1
    out = {"client": client, "status": status, "bytes": len(html or ""),
           "state": state, "skus": rows, "ms": int((time.time() - started) * 1000)}
    out.update(extra)
    return out


def trial_http(client, exit_url, url, timeout):
    started = time.time()
    try:
        session = http_scraper.build_session(exit_url, client)
        response = session.get(url, timeout=timeout)
    except Exception as exc:  # noqa: BLE001
        return {"client": client, "error": "%s: %s" % (
            type(exc).__name__, proxy_pool.mask_text(str(exc))[:120]),
            "ms": int((time.time() - started) * 1000)}
    return _summary(client, response.status_code, response.text, response.url, started)


def trial_local(exit_url, url, timeout):
    started = time.time()
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True,
                                     proxy=proxy_pool.to_playwright(exit_url))
        try:
            page = browser.new_context(locale="en-US").new_page()
            response = page.goto(url, wait_until="commit",
                                 timeout=timeout * 1000)
            page.wait_for_timeout(SETTLE_MS)
            # The served body, not page.content(): the storefront deletes its
            # state <script> from the DOM on hydration (2026-10-01).
            return _summary("local-chromium", response.status if response else None,
                            response.text() if response else page.content(), page.url, started)
        except Exception as exc:  # noqa: BLE001
            return {"client": "local-chromium",
                    "error": proxy_pool.mask_text(str(exc).splitlines()[0])[:160]}
        finally:
            browser.close()


def trial_cdp(endpoint, url, timeout, want_exit):
    started = time.time()
    events, notes, armed = [], [], False
    with sync_playwright() as pw:
        try:
            browser = pw.chromium.connect_over_cdp(endpoint, timeout=timeout * 1000)
        except Exception as exc:  # noqa: BLE001
            return {"client": "scraping-browser", "connect_failed": True,
                    "error": proxy_pool.mask_text(str(exc).splitlines()[0])[:160]}
        try:
            ctx = browser.contexts[0] if browser.contexts else browser.new_context()
            page = ctx.new_page()
            try:
                cdp = ctx.new_cdp_session(page)
                for event in ("Captcha.solveStarted", "Captcha.solveFinished",
                              "Captcha.solveFailed", "Captcha.detected"):
                    cdp.on(event, lambda payload, e=event: events.append(
                        {"event": e, "at": now(), "payload": str(payload)[:200]}))
                cdp.send("Captcha.setAutoSolve", {"autoSolve": True})
                armed = True
            except Exception as exc:  # noqa: BLE001
                notes.append(str(exc).splitlines()[0][:100])
            # domcontentloaded, never load: the load event did not fire in 60 s
            # on 2026-09-24, and that timeout left the profile locked.
            response = page.goto(url, wait_until="commit",
                                 timeout=timeout * 1000)
            page.wait_for_timeout(SETTLE_MS)
            result = _summary("scraping-browser", response.status if response else None,
                              response.text() if response else page.content(), page.url, started,
                              autosolve_armed=armed, autosolve_events=events,
                              autosolve_notes=notes)
            if want_exit:
                try:
                    page.goto("https://ipinfo.io/json", wait_until="domcontentloaded",
                              timeout=30000)
                    info = json.loads(page.inner_text("body"))
                    result["exit_seen"] = "%s %s" % (info.get("country"),
                                                     (info.get("org") or "")[:40])
                except Exception:  # noqa: BLE001
                    result["exit_seen"] = None
            return result
        except Exception as exc:  # noqa: BLE001
            return {"client": "scraping-browser", "autosolve_armed": armed,
                    "error": proxy_pool.mask_text(str(exc).splitlines()[0])[:160]}
        finally:
            try:
                browser.close()
            except Exception:  # noqa: BLE001
                pass


def _line(result):
    if result.get("error"):
        return "ERR %s" % result["error"]
    extra = ""
    if "autosolve_armed" in result:
        extra = " armed=%s events=%d exit=%s" % (
            result["autosolve_armed"], len(result.get("autosolve_events") or []),
            result.get("exit_seen"))
    return "status=%s bytes=%s state=%s skus=%s%s" % (
        result.get("status"), result.get("bytes"), result.get("state"),
        result.get("skus"), extra)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--trials", type=int, default=6)
    parser.add_argument("--timeout", type=int, default=45)
    parser.add_argument("--url", default=URL)
    parser.add_argument("--out", default="live/trials")
    parser.add_argument("--settle", type=float, default=25.0,
                        help="Seconds between trials; a just-closed profile is "
                             "held briefly.")
    parser.add_argument("--no-cdp", action="store_true")
    parser.add_argument("--no-local", action="store_true")
    parser.add_argument("--cdp-exit", action="store_true",
                        help="After the target, visit ipinfo.io to record the "
                             "Scraping Browser's exit.")
    args = parser.parse_args(argv)

    env = argparse.Namespace(cdp_endpoint=None, proxy=None, twocaptcha_key=None, url=None)
    env_config.apply(env, quiet=True)
    if not env.proxy:
        raise SystemExit("POTTERYBARN_PROXY is required for these trials")
    cdp_live = bool(env.cdp_endpoint) and not args.no_cdp

    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    trials = []
    for index in range(1, args.trials + 1):
        exit_url = proxy_pool.mint_sessions(env.proxy, 1)[0]
        record = {"trial": index, "at": now(), "exit": proxy_pool.mask(exit_url),
                  "results": []}
        print("[%2d/%d]" % (index, args.trials), flush=True)
        arms = [lambda: trial_http("curl", exit_url, args.url, args.timeout),
                lambda: trial_http("requests", exit_url, args.url, args.timeout)]
        if not args.no_local:
            arms.append(lambda: trial_local(exit_url, args.url, args.timeout))
        for arm in arms:
            result = arm()
            record["results"].append(result)
            print("   %-16s %s" % (result["client"], _line(result)), flush=True)
        if cdp_live:
            result = trial_cdp(env.cdp_endpoint, args.url, args.timeout, args.cdp_exit)
            record["results"].append(result)
            print("   %-16s %s" % ("scraping-browser", _line(result)), flush=True)
            if result.get("connect_failed"):
                print("   CDP connect failed — no further CDP attempts this run "
                      "(every attempt re-takes a held profile).", flush=True)
                cdp_live = False
        trials.append(record)
        with open(args.out + ".json", "w", encoding="utf-8") as handle:
            json.dump(trials, handle, ensure_ascii=False, indent=1)
        if index < args.trials:
            time.sleep(args.settle)
    print("\nwrote %s.json" % args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
