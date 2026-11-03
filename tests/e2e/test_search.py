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

        # Clicking an airport on the map: with a departure set, it becomes the
        # destination or the new departure; with none, either
        await ready(page, "?from=EGKK")
        await page.evaluate("onMapAirportClick(mapAirportEntries.find(x => x.a.icao !== 'EGKK').a, null)")
        buttons = await page.evaluate("[...document.querySelectorAll('.map-popup .mp-actions button')].map(b => [b.textContent.trim(), b.className])")
        check("with a departure set: SET AS DESTINATION, then SET AS NEW DEPARTURE in grey",
              [b[0].split(' (')[0] for b in buttons] == ["SET AS DESTINATION", "SET AS NEW DEPARTURE"] and "ghost" in buttons[1][1], buttons)
        check("no airport-click mode control above the map", await page.locator("input[name=mapPickMode]").count() == 0)
        await page.click("#wxCatToggle"); await page.wait_for_timeout(300)
        boxes = await page.evaluate("[...document.querySelectorAll('#mapLegend span')].map(s => { const r = s.getBoundingClientRect(); return [Math.round(r.x), Math.round(r.y)]; })")
        await page.click("#wxCatToggle"); await page.wait_for_timeout(300)
        after = await page.evaluate("[...document.querySelectorAll('#mapLegend span')].map(s => { const r = s.getBoundingClientRect(); return [Math.round(r.x), Math.round(r.y)]; })")
        check("the legend keeps its place when the METAR layer comes and goes", boxes == after and len(boxes) > 6)

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
        detail = await page.inner_text("#detailCell")
        check("the preview recommends the aircraft and shows the load factor, with no aircraft choice",
              "RECOMMENDED AIRCRAFT" in detail and "EXPECTED LOAD FACTOR" in detail and "EXPECTED PAX" not in detail
              and await page.locator("#detailCell select").count() == 0, detail[:300])
        current = await page.evaluate("expandedDetailData.legs[0].aircraft_type")
        other = "32Q" if current != "32Q" else "319"

        await page.click(".confirm-row button")
        await page.wait_for_selector(".af-top")
        steps = await page.evaluate("[...document.querySelectorAll('.af-flow-step b')].map(e => e.textContent)")
        check("CONFIRM opens the active flight with its three steps", steps == ["PLAN WITH SIMBRIEF", "SIGN LOADSHEET", "FILE PIREP"], steps)
        await page.wait_for_selector("#sbTypeSel-0")
        label = await page.evaluate("document.getElementById('sbTypeSel-0').selectedOptions[0].textContent")
        check("the PLAN tab offers the airline's fleet, the assigned type recommended", current in label and "RECOMMENDED" in label, label)
        lf = await page.evaluate("loadActiveFlight().legs.map(l => l.load_factor)")
        await page.select_option("#sbTypeSel-0", other)
        await page.wait_for_function(f"loadActiveFlight().legs.every(l => l.aircraft_type === '{other}')", timeout=10000)
        fl = await page.evaluate("loadActiveFlight()")
        check("choosing another type changes every leg, at the same load factor, with pax for its seats",
              len({l["seat_capacity"] for l in fl["legs"]}) == 1 and all(l["recommended_type"] == current for l in fl["legs"])
              and all(l["pax_count"] <= l["seat_capacity"] for l in fl["legs"]) and [l["load_factor"] for l in fl["legs"]] == lf,
              [(l["aircraft_type"], l["pax_count"], l["seat_capacity"]) for l in fl["legs"]])
        await page.locator(".af-tabs").screenshot(path=shot("plan_aircraft.png"))
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
