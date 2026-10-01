#!/usr/bin/env python3
"""CI checks that are not the offline suite, runnable locally too.

    python3 .github/ci_checks.py --help-check     every CLI answers --help
    python3 .github/ci_checks.py --sample-check   the committed samples are real
    python3 .github/ci_checks.py --secret-check   delegates to tools/scan_secrets.py
    python3 .github/ci_checks.py --all

The secret check is NOT a second implementation: it runs the same scanner the
hooks run. A second copy of a security check is a second place for it to be
wrong, which is literally what happened in a sibling repo.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

CLIS = ["api_scraper.py", "http_scraper.py", "catalog_walk.py", "diff_runs.py",
        "env_config.py", "tools/scan_secrets.py", "tools/browser_profile_client.py"]
ENGINE_CLIS = ["playwright_scraper.py", "puppeteer_scraper.py", "selenium_scraper.py"]
FABRICATION_MARKERS = ("lorem", "example product", "test product", "foo", "dummy")


def help_check() -> int:
    bad = 0
    for cli in CLIS + ENGINE_CLIS:
        args = [sys.executable, os.path.join(HERE, cli)]
        args += [] if cli == "env_config.py" else ["--help"]
        result = subprocess.run(args, capture_output=True, text=True, cwd=HERE)
        if result.returncode != 0 and cli in ENGINE_CLIS and "ModuleNotFoundError" in result.stderr:
            print("skip  %s (engine not installed)" % cli)
            continue
        ok = result.returncode == 0
        bad += not ok
        print("%s  %s" % ("ok  " if ok else "FAIL", cli))
        if not ok:
            print(result.stderr[-400:])
    return 1 if bad else 0


def sample_check() -> int:
    import output_writer as W
    problems = []
    for name, cls in (("sample_output.json", W.Product), ("sample_skus.json", W.SkuRow)):
        rows = json.load(open(os.path.join(HERE, name), encoding="utf-8"))
        want = [f.name for f in dataclasses.fields(cls)]
        if not rows or list(rows[0].keys()) != want:
            problems.append("%s columns do not match %s" % (name, cls.__name__))
        text = json.dumps(rows).lower()
        for marker in FABRICATION_MARKERS:
            if '"%s' % marker in text:
                problems.append("%s contains a fabrication marker %r" % (name, marker))
        meta = json.load(open(os.path.join(HERE, name.replace(".json", ".meta.json"))))
        if meta.get("status") != "complete" or meta.get("products", 0) < len(rows):
            problems.append("%s: its sidecar is not a complete run" % name)
    for problem in problems:
        print("::error::%s" % problem)
    print("sample check: %d problem(s)" % len(problems))
    return 1 if problems else 0


def secret_check() -> int:
    return subprocess.call([sys.executable, os.path.join(HERE, "tools", "scan_secrets.py"),
                            "--worktree"], cwd=HERE)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--help-check", action="store_true")
    parser.add_argument("--sample-check", action="store_true")
    parser.add_argument("--secret-check", action="store_true")
    parser.add_argument("--all", action="store_true")
    args = parser.parse_args()
    rc = 0
    if args.help_check or args.all:
        rc |= help_check()
    if args.sample_check or args.all:
        rc |= sample_check()
    if args.secret_check or args.all:
        rc |= secret_check()
    return rc


if __name__ == "__main__":
    sys.exit(main())
