"""Flight search filters, the briefing's aircraft, and what CONFIRM sends."""
import asyncio

from playwright.async_api import async_playwright

from common import BASE, Checks, launch, prepare, shot

check = Checks()


async def ready(page, query):
    await page.goto(BASE + "/app" + query, wait_until="networkidle")
    await page.wait_for_function("searchStatus === 'ready'")


async def main():
    async with async_playwright() as p:
        browser = await launch(p)
        page = await prepare(await browser.new_page(viewport={"width": 1600, "height": 950}))

        await ready(page, "?from=EGKK")
        total = await page.evaluate("filteredItineraries().length")
        await page.select_option("#depTimeFilter", "morning")
        deps = await page.evaluate("filteredItineraries().map(i => i.first_departure_zulu)")
        check("departure filter keeps only 06-12Z departures",
              deps and all(6 <= int(x[:2]) < 12 for x in deps) and len(deps) < total, f"{len(deps)}/{total}")
        check("the page URL carries the filter", "dep=morning" in page.url, page.url)
        chips = await page.locator(".crit-chip").all_inner_texts()
        check("a DEPARTS chip is shown", any("DEPARTS" in c for c in chips), chips)
        await page.select_option("#arrTimeFilter", "afternoon")
        pairs = await page.evaluate("filteredItineraries().map(i => [i.first_departure_zulu, i.last_arrival_zulu])")
        check("departure and arrival filters combine",
              all(6 <= int(d[:2]) < 12 and 12 <= int(a[:2]) < 18 for d, a in pairs), len(pairs))

        await ready(page, "?from=EGKK&dep=evening&arr=night")
        values = await page.evaluate("[document.getElementById('depTimeFilter').value, document.getElementById('arrTimeFilter').value]")
        check("filters are restored from the URL", values == ["evening", "night"], values)
        await page.evaluate("resetResultFilters()")
        values = await page.evaluate("[searchState.depTime, searchState.arrTime]")
        check("reset clears the time filters", values == ["all", "all"], values)

        # One airframe flies the whole trip: an easyJet round trip (no type in
        # the schedule data) shows the same aircraft on both legs, and
        # changing it changes both. With no aircraft in the profile, every
        # easyJet type can be assigned and picked.
        await page.evaluate("""async () => { const s = await (await fetch('/settings')).json();
            s.profile.aircraft_owned = [];
            await fetch('/settings', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(s)}); }""")
        await ready(page, "?from=EGKK&airline=EZY&legs=RT")
        same = []
        for i in range(6):
            await page.evaluate("expandedDetailData = null")
            await page.evaluate(f"toggleItinerary(itinKey(tableVisibleItineraries[{i}]))")
            await page.wait_for_function("expandedDetailData && expandedDetailData.legs")
            same.append(await page.evaluate("new Set(expandedDetailData.legs.map(l => l.aircraft_type)).size === 1"))
            if i < 5:
                await page.evaluate("toggleItinerary(expandedKey)")
        check("every leg of a trip gets the same aircraft", all(same), same)
        current = await page.evaluate("expandedDetailData.legs[0].aircraft_type")
        other = "32Q" if current != "32Q" else "319"
        await page.locator(".aircraft-reassign-select").first.select_option(other)
        await page.wait_for_function(f"expandedDetailData.legs.every(l => l.aircraft_type === '{other}')", timeout=10000)
        seats = await page.evaluate("expandedDetailData.legs.map(l => l.seat_capacity)")
        check("changing the aircraft changes every leg", len(set(seats)) == 1, seats)
        await page.screenshot(path=shot("search_aircraft.png"))

        confirm_urls = []
        page.on("request", lambda r: confirm_urls.append(r.url) if "/confirm" in r.url else None)
        await page.click(".confirm-row button")
        await page.wait_for_timeout(1500)
        check("CONFIRM tells the server whether the first leg is full", confirm_urls and "full=" in confirm_urls[0], confirm_urls[:1])
        await page.evaluate("""async () => { const r = await (await fetch('/active-flight')).json();
            await fetch('/active-flight', {method: 'PUT', headers: {'Content-Type': 'application/json'},
              body: JSON.stringify({flight: null, base_rev: r.rev})}); clearActiveFlight(); }""")

        mobile = await prepare(await browser.new_page(viewport={"width": 390, "height": 844}))
        await mobile.goto(BASE + "/app?from=EGKK", wait_until="networkidle")
        overflow = await mobile.evaluate("document.documentElement.scrollWidth > window.innerWidth")
        check("no horizontal page scroll on phones", not overflow)
        check.done(page, mobile)
        await browser.close()


asyncio.run(main())
