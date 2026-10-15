"""Static assets, the profile page, the landing page video, the legal pages
and the glossary."""
import asyncio
import os

import psycopg2
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
            fill = await lp.evaluate("[innerHeight, Math.round(document.querySelector('.hero').getBoundingClientRect().height)]")
            check(f"the hero video fills the whole window at {width}px", fill[1] >= fill[0], fill)
            if expect is None:
                ok = src is None
            else:
                ok = src is not None and expect in src and (("-960" in src) == (width < 900))
            check(f"landing video at {width}px, motion {motion}", ok, src)
            await ctx.close()

        await page.goto(BASE + "/")
        html = await page.content()
        check("landing claims match the app", "Filter by weather" not in html and "oversold" in html and "SAMPLE DATA" in html)

        # The demo below How it works runs on sample data in the page
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
        # Every page loads its font and map library from this site: nothing goes to another server
        ctx = await browser.new_context(viewport={"width": 1400, "height": 900})
        own = await prepare(await ctx.new_page())
        outside = []
        own.on("request", lambda r: outside.append(r.url) if not r.url.startswith(BASE) and not r.url.startswith("data:")
               and "tile.openstreetmap.org" not in r.url else None)
        for path in ("/", "/app", "/login", "/privacy"):
            await own.goto(BASE + path, wait_until="networkidle")
        inter = await own.evaluate("document.fonts.check('600 14px Inter') && [...document.fonts].some(f => f.family.replace(/\"/g, '') === 'Inter' && f.status === 'loaded')")
        check("pages load nothing from other servers, and the Inter font is served by the app", not outside and inter, outside[:3])
        await ctx.close()

        # The waitlist, in the website's closing section while sign-ups are invitation-only
        await page.goto(BASE + "/#waitlist")
        await page.fill("#waitlistForm input[name=email]", "e2e.waitlist@example.com")
        await page.click("#waitlistForm button")
        check("joining the waitlist needs the consent box", await page.text_content("#waitlistStatus") == "" and await page.is_visible("#waitlistForm"))
        await page.check("#waitlistForm input[name=consent]")
        await page.click("#waitlistForm button")
        await page.wait_for_selector("#waitlistStatus.ok")
        with psycopg2.connect(os.environ["DATABASE_URL"]) as conn, conn.cursor() as cur:
            cur.execute("SELECT consent FROM waitlist WHERE email = 'e2e.waitlist@example.com'")
            row = cur.fetchone()
            cur.execute("DELETE FROM waitlist WHERE email = 'e2e.waitlist@example.com'")
        check("the waitlist keeps the email with the consent given", row and "join VirtualDispatch" in row[0]
              and not await page.is_visible("#waitlistForm"), row)
        await page.locator(".cta-band").screenshot(path=shot("waitlist.png"))

        await page.goto(BASE + "/privacy")
        html = await page.content()
        check("privacy policy lists the data and services", all(k in html for k in ("SimBrief", "Turnstile", "profile photo", "OpenStreetMap", "Sentry"))
              and "Google Fonts" not in html)

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
        shown = await tp.evaluate("[...document.querySelectorAll('.tour-shot.active')].filter(i => getComputedStyle(i).display !== 'none').map(i => i.getAttribute('src').split('?')[0].split('/').pop())")
        check("the website opens at night with night screenshots", await tp.evaluate("document.documentElement.dataset.theme") == "dark" and shown == ["tour-intro-dark.webp"], shown)
        poster = await tp.evaluate("document.getElementById('heroVideo').getAttribute('poster')")
        check("the hero shows the night video's first frame while it loads", "hero-dark-poster.webp" in poster, poster)
        await tp.click(".theme-toggle")
        poster = await tp.evaluate("document.getElementById('heroVideo').getAttribute('poster')")
        check("the hero poster follows the switch", "hero-light-poster.webp" in poster, poster)
        shown = await tp.evaluate("[...document.querySelectorAll('.tour-shot.active')].filter(i => getComputedStyle(i).display !== 'none').map(i => i.getAttribute('src').split('?')[0].split('/').pop())")
        check("the website's switch brings back day screenshots", shown == ["tour-intro.webp"], shown)
        await tp.click(".theme-toggle")
        await tp.goto(BASE + "/login")
        check("the sign-in page has the switch and opens at night",
              await tp.locator(".theme-toggle").count() == 1 and await tp.evaluate("getComputedStyle(document.body).backgroundColor") != "rgb(243, 244, 246)")
        await ctx.close()
        check.done(page)
        await browser.close()


asyncio.run(main())
