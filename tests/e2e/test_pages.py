"""Static assets, the profile page, the landing page video, the legal pages
and the glossary."""
import asyncio

from playwright.async_api import async_playwright

from common import BASE, Checks, launch, prepare, shot

check = Checks()


async def main():
    async with async_playwright() as p:
        browser = await launch(p)
        page = await prepare(await browser.new_page(viewport={"width": 1600, "height": 950}))
        requests = []
        page.on("request", lambda r: requests.append(r.url))
        await page.goto(BASE + "/app", wait_until="networkidle")
        check("app.js and app.css load with a content version",
              any("/static/js/app.js?v=" in u for u in requests) and any("/static/css/app.css?v=" in u for u in requests))

        requests.clear()
        await page.evaluate("renderUserPage()")
        await page.wait_for_timeout(2500)
        check("profile loads its stats from /stats/flights only",
              any("/stats/flights" in u for u in requests) and not any(u.rstrip("/").endswith("/stats") for u in requests))
        bars = await page.evaluate("document.querySelectorAll('#monthlyChart [title]').length")
        markers = await page.evaluate("document.querySelectorAll('#destinationsMap .leaflet-interactive, #destinationsMap .leaflet-marker-icon').length")
        check("monthly chart and destinations map render", bars > 0 and markers > 0, f"bars={bars} markers={markers}")
        await page.screenshot(path=shot("profile.png"))

        await page.goto(BASE + "/app", wait_until="networkidle")
        await page.evaluate("openGlossary()")
        terms = await page.evaluate("document.querySelectorAll('.glossary-entry').length")
        check("glossary lists its terms", terms >= 40, terms)
        await page.fill("#glossaryFilter", "oversold")
        visible = await page.evaluate("[...document.querySelectorAll('.glossary-entry')].filter(e => e.offsetParent).length")
        check("glossary search narrows the list", visible == 1, visible)

        for width, expect, motion in ((1600, "hero-", "no-preference"), (390, "-960.mp4", "no-preference"), (1600, None, "reduce")):
            ctx = await browser.new_context(viewport={"width": width, "height": 900}, reduced_motion=motion)
            lp = await prepare(await ctx.new_page())
            await lp.goto(BASE + "/")
            await lp.wait_for_timeout(1500)
            src = await lp.evaluate("document.getElementById('heroVideo').getAttribute('src')")
            if expect is None:
                ok = src is None
            else:
                ok = src is not None and expect in src and (("-960" in src) == (width < 900))
            check(f"landing video at {width}px, motion {motion}", ok, src)
            await ctx.close()

        await page.goto(BASE + "/")
        html = await page.content()
        check("landing claims match the app", "Filter by weather" not in html and "oversold" in html)
        await page.goto(BASE + "/privacy")
        html = await page.content()
        check("privacy policy lists the data and services", all(k in html for k in ("SimBrief", "Turnstile", "profile photo", "OpenStreetMap")))
        check.done(page)
        await browser.close()


asyncio.run(main())
