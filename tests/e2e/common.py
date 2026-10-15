"""Shared helpers for the browser suites: the server address, a browser
launcher, page preparation and a small pass/fail recorder.

Environment:
  VD_E2E_URL          base URL of the server under test (run.py sets it)
  VD_E2E_CHROMIUM     path to a Chromium binary, if Playwright's own isn't installed
  VD_E2E_OUT          folder for screenshots (default: a temp folder)
"""
import os
import sys
import tempfile

BASE = os.environ.get("VD_E2E_URL", "http://localhost:5055")
OUT = os.environ.get("VD_E2E_OUT") or tempfile.mkdtemp(prefix="vd-e2e-")


async def launch(playwright):
    exe = os.environ.get("VD_E2E_CHROMIUM")
    return await playwright.chromium.launch(**({"executable_path": exe} if exe else {}))


async def prepare(page):
    """Collects page errors in page.errors and keeps the page off the
    network: OpenStreetMap tiles are never fetched (the app's own base map
    is used). Everything else the pages load comes from the app itself."""
    page.errors = []
    page.on("pageerror", lambda e: page.errors.append(str(e)))
    await page.route("https://tile.openstreetmap.org/**", lambda r: r.abort())
    return page


class Checks:
    """check(name, ok, detail) prints PASS/FAIL lines; done() prints the
    total and exits non-zero when anything failed."""

    def __init__(self):
        self.results = []

    def __call__(self, name, ok, detail=""):
        self.results.append(bool(ok))
        print(("PASS " if ok else "FAIL ") + name, detail if not ok or detail else "")

    def done(self, *pages):
        errors = [e for p in pages for e in getattr(p, "errors", [])]
        if errors:
            self("no JavaScript errors on the page", False, errors[:3])
        passed = sum(self.results)
        print(f"{passed} / {len(self.results)}")
        sys.exit(0 if passed == len(self.results) else 1)


def shot(name):
    return os.path.join(OUT, name)
