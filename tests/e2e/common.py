"""Shared helpers for the browser suites: the server address, a browser
launcher, page preparation and a small pass/fail recorder.

Environment:
  VD_E2E_URL          base URL of the server under test (run.py sets it)
  VD_E2E_CHROMIUM     path to a Chromium binary, if Playwright's own isn't installed
  VD_E2E_ASSETS       folder with local copies of Leaflet and the Inter font, for
                      machines that can't reach cdnjs/Google Fonts (see README)
  VD_E2E_OUT          folder for screenshots (default: a temp folder)
"""
import os
import sys
import tempfile

BASE = os.environ.get("VD_E2E_URL", "http://localhost:5055")
OUT = os.environ.get("VD_E2E_OUT") or tempfile.mkdtemp(prefix="vd-e2e-")
ASSETS = os.environ.get("VD_E2E_ASSETS")


async def launch(playwright):
    exe = os.environ.get("VD_E2E_CHROMIUM")
    return await playwright.chromium.launch(**({"executable_path": exe} if exe else {}))


def _local_asset_routes():
    leaflet = os.path.join(ASSETS, "package", "dist")
    fonts = os.path.join(ASSETS, "inter", "package", "files")
    font_css = "\n".join(
        f"@font-face{{font-family:'Inter';font-style:normal;font-weight:{w};font-display:swap;"
        f"src:url(https://fonts.gstatic.com/local/inter-{w}.woff2) format('woff2');}}" for w in (400, 500, 600, 700))

    def read(path, mode="r"):
        with open(path, mode) as f:
            return f.read()

    async def leaflet_img(route):
        name = route.request.url.rsplit("/", 1)[-1]
        await route.fulfill(status=200, content_type="image/png", body=read(os.path.join(leaflet, "images", name), "rb"))

    async def font(route):
        weight = route.request.url.rsplit("-", 1)[-1].split(".")[0]
        await route.fulfill(status=200, content_type="font/woff2",
                            body=read(os.path.join(fonts, f"inter-latin-{weight}-normal.woff2"), "rb"))

    return [
        ("**/leaflet.min.js", lambda r: r.fulfill(status=200, content_type="application/javascript",
                                                  body=read(os.path.join(leaflet, "leaflet.js")))),
        ("**/leaflet.min.css", lambda r: r.fulfill(status=200, content_type="text/css",
                                                   body=read(os.path.join(leaflet, "leaflet.css")))),
        ("**/ajax/libs/leaflet/1.9.4/images/*", leaflet_img),
        ("https://fonts.googleapis.com/**", lambda r: r.fulfill(status=200, content_type="text/css", body=font_css)),
        ("https://fonts.gstatic.com/local/**", font),
    ]


async def prepare(page):
    """Collects page errors in page.errors and keeps the page off the
    network: OpenStreetMap tiles are never fetched (the app's own base map
    is used), and with VD_E2E_ASSETS set the CDN files come from disk."""
    page.errors = []
    page.on("pageerror", lambda e: page.errors.append(str(e)))
    if ASSETS:
        for pattern, handler in _local_asset_routes():
            await page.route(pattern, handler)
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
