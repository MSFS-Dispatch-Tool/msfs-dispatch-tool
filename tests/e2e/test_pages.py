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
        check("landing claims match the app", "Filter by weather" not in html and "oversold" in html and "SAMPLE DATA" in html)

        # How it works: a card per feature with a real screenshot, opening full size
        await page.evaluate("document.getElementById('how-it-works').scrollIntoView()")
        check("six feature cards", await page.locator(".fcard").count() == 6)
        await page.click(".fcard-shot >> nth=0")
        src = await page.evaluate("document.getElementById('shotDialog').open && document.getElementById('shotDialogImg').getAttribute('src')")
        check("a feature screenshot opens full size", bool(src) and "features/routes.webp" in src, src)
        await page.keyboard.press("Escape")

        # The demo below runs on sample data in the page
        await page.evaluate("document.getElementById('demo').scrollIntoView()")
        await page.wait_for_function("document.querySelectorAll('#demoMap .dm-route').length > 5")
        before = await page.evaluate("[+document.getElementById('demoCount').textContent, document.querySelectorAll('#demoMap .dm-route').length]")
        await page.select_option("#demoFilters select[name=airline]", "WZZ")
        await page.select_option("#demoFilters select[name=time]", "morning")
        after = await page.evaluate("[+document.getElementById('demoCount').textContent, document.querySelectorAll('#demoMap .dm-route').length]")
        check("the route demo's filters narrow the list and the map", 0 < after[0] < before[0] and 0 < after[1] < before[1], (before, after))
        await page.select_option("#demoFilters select[name=dep]", "EIDW")
        check("a base with no Wizz Air flights shows none", await page.evaluate("document.getElementById('demoCount').textContent") == "0")
        await page.click("#demoLights .light:nth-child(3)")
        detail = await page.text_content("#demoLightDetail")
        check("a status light opens its details", detail.startswith("TECH LOG") and "MEL" in detail, detail[:60])
        await page.click("#demoRoll")
        await page.wait_for_timeout(1300)
        check("rolling another leg changes the lights", await page.text_content("#demoLegNote") != "" and "FR113" in await page.text_content("#demoLegNote"))
        await page.click("#demoAccept")
        check("accepting the gate change issues loadsheet edition 02", "EDNO 02" in await page.text_content("#demoSheet"))
        flights = await page.evaluate("+document.querySelector('#demoKpis .kpi b').textContent")
        await page.click("#demoRanges button[data-range='90']")
        fewer = await page.evaluate("+document.querySelector('#demoKpis .kpi b').textContent")
        check("the logbook's range narrows the stats", 0 < fewer < flights, (flights, fewer))
        await page.screenshot(path=shot("landing_features.png"), full_page=True)
        await page.goto(BASE + "/privacy")
        html = await page.content()
        check("privacy policy lists the data and services", all(k in html for k in ("SimBrief", "Turnstile", "profile photo", "OpenStreetMap")))

        # Day / night: the app's header switch repaints the map in place, and
        # the choice follows the visitor to the website and the sign-in page.
        ctx = await browser.new_context(viewport={"width": 1600, "height": 950})
        tp = await prepare(await ctx.new_page())
        await tp.goto(BASE + "/app?from=EGKK", wait_until="networkidle")
        await tp.wait_for_function("searchStatus === 'ready' && basemapLandLayers.size > 0")
        check("day is the default", await tp.evaluate("document.documentElement.dataset.theme") == "light")
        await tp.click(".app-theme")
        state = await tp.evaluate("""[document.documentElement.dataset.theme, localStorage.getItem('vd-theme'),
            getComputedStyle(document.querySelector('.app-logo .for-dark')).display,
            [...basemapLandLayers].every(g => g.getLayers()[0].options.fillColor === BASEMAP_LAND.dark.fillColor),
            getComputedStyle(document.body).backgroundColor]""")
        check("the header switch turns the app to night, map included",
              state[:4] == ["dark", "dark", "block", True] and state[4] == "rgb(10, 13, 16)", state)
        await tp.screenshot(path=shot("night_search.png"))
        await tp.goto(BASE + "/")
        check("the website opens at night", await tp.evaluate("document.documentElement.dataset.theme") == "dark")
        poster = await tp.evaluate("document.getElementById('heroVideo').getAttribute('poster')")
        check("the hero shows the night video's first frame while it loads", "hero-dark-poster.webp" in poster, poster)
        await tp.click(".theme-toggle")
        poster = await tp.evaluate("document.getElementById('heroVideo').getAttribute('poster')")
        check("the hero poster follows the switch", "hero-light-poster.webp" in poster, poster)
        await tp.click(".theme-toggle")
        await tp.goto(BASE + "/login")
        check("the sign-in page has the switch and opens at night",
              await tp.locator(".theme-toggle").count() == 1 and await tp.evaluate("getComputedStyle(document.body).backgroundColor") != "rgb(243, 244, 246)")
        await ctx.close()
        check.done(page)
        await browser.close()


asyncio.run(main())
