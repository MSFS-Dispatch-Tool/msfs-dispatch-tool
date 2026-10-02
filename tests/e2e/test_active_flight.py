"""The active flight workflow: the four steps, the loadsheet with the
captain's decisions on last-minute changes and dangerous goods, and the
PIREP form."""
import asyncio
import json
import re

from playwright.async_api import async_playwright

from common import BASE, Checks, launch, prepare, shot

check = Checks()

OFP = {"static_id": "", "origin_icao": "EGKK", "destination_icao": "EGAC", "weight_unit": "kgs", "registration": "G-EZWX",
       "icao_type": "A320", "callsign": "EZY81AB", "route": "SAM L620 HON UL10 WAL L10 IOM DCT BELZU", "cost_index": "12",
       "initial_altitude_ft": "36000", "dow": "42750", "pax_weight": "84", "payload": "15400", "est_zfw": "58150",
       "max_zfw": "62500", "max_tow": "73500", "max_ldw": "64500", "takeoff_fuel": "6200", "taxi_fuel": "220",
       "trip_fuel": "2900", "est_tow": "64350", "est_ldw": "61450", "block_fuel": "6420", "plan_landing_fuel": "3300",
       "epax": "171", "navlog": []}
EXTRAS = {"lmc_event": {"id": "lmc-04", "type": "cargo_change", "description": "Extra bags checked in at the gate", "delta": 60, "unit": "kg"},
          "dangerous_goods": {"id": "dg-02", "label": "Dry ice (UN1845, class 9), 40 kg for perishable cargo, hold 1",
                              "notoc": True, "un": "UN1845", "class": "9", "weight_kg": 40, "hold": "1"}}


async def main():
    async with async_playwright() as p:
        browser = await launch(p)
        ctx = await browser.new_context(viewport={"width": 1500, "height": 940})
        await ctx.add_init_script("localStorage.setItem('simdispatch_simbrief_username', 'skyline_ops');"
                                  "localStorage.setItem('simdispatch_simbrief_aircraft_notice_dismissed', '1');")
        page = await prepare(await ctx.new_page())
        page.on("dialog", lambda d: asyncio.ensure_future(d.accept()))
        await page.route("**/simbrief/redirect-url*", lambda r: r.fulfill(status=200, content_type="application/json", body='{"url": "about:blank"}'))
        await page.route("**/simbrief/ofp*", lambda r: r.fulfill(status=200, content_type="application/json", body=json.dumps(OFP)))
        extras_urls = []

        async def extras(route):
            extras_urls.append(route.request.url)
            await route.fulfill(status=200, content_type="application/json", body=json.dumps(EXTRAS))
        await page.route("**/loadsheet/extras*", extras)

        await page.goto(BASE + "/app?from=EGKK&to=EGAC&legs=RT", wait_until="networkidle")
        await page.wait_for_function("searchStatus === 'ready'")
        await page.evaluate("toggleItinerary(itinKey(tableVisibleItineraries[0]))")
        await page.wait_for_selector(".confirm-row button", timeout=20000)
        if await page.locator("#swapAircraftBtn").count():
            await page.click("#swapAircraftBtn")
            await page.wait_for_timeout(1500)
        await page.click(".confirm-row button")
        await page.wait_for_selector(".af-top")

        now = await page.evaluate("document.querySelector('.af-flow-step.now b').textContent")
        check("step 1 is SEND TO SIMBRIEF and opens on the briefing", now == "SEND TO SIMBRIEF" and await page.locator("#af-pane-briefing").is_visible())
        check("the steps are a tracker, not buttons", await page.locator(".af-flow button").count() == 0)
        check("SEND TO SIMBRIEF sits at the bottom of the briefing", await page.evaluate("document.querySelector('#af-pane-briefing').lastElementChild.textContent.includes('SEND TO SIMBRIEF')"))
        widths = await page.evaluate("[...document.querySelectorAll('.af-tabbar button')].map(b => Math.round(b.getBoundingClientRect().width))")
        check("the three tabs are the same width", len(set(widths)) == 1, widths)
        check("no schedule table under the map", await page.locator(".af-route table").count() == 0)
        order = await page.evaluate("[...document.querySelectorAll('#af-pane-briefing .sheet-h')].map(h => h.textContent)")
        check("briefing sections in tile order, crew last", order[:3] == ["FLIGHT", "TIMING", "TECHNICAL LOG"] and order[-1] == "CREW", order)
        head = await page.evaluate("[...document.querySelectorAll('#brief-timing .sheet-colhead span')].map(e => e.textContent)")
        check("timing has scheduled and estimated columns", head[1:] == ["SCHEDULED", "ESTIMATED"], head)
        await page.click(".bsum-tile:has-text('CREW DUTY')")
        await page.wait_for_timeout(700)
        check("a summary tile scrolls to its section", await page.evaluate("document.getElementById('af-pane-briefing').scrollTop") > 300)
        check("map note waits for the OFP", "Waiting for the SimBrief OFP" in await page.inner_text(".af-map-note"))
        check("loadsheet waits for the OFP", await page.locator("#af-pane-loadsheet button:has-text('ASK FOR LOADSHEET')").is_disabled())
        check("PIREP waits for the loadsheet", await page.locator("#af-pane-pirep button:has-text('FILL PIREP')").is_disabled())

        await page.click("#af-pane-briefing button:has-text('SEND TO SIMBRIEF')")
        await page.wait_for_timeout(500)
        check("step 2 FETCH OFP is next", await page.evaluate("document.querySelector('.af-flow-step.now b').textContent") == "FETCH OFP")
        await page.click("#af-pane-briefing button:has-text('FETCH OFP')")
        await page.wait_for_timeout(600)
        check("with the OFP in, the loadsheet tab opens", await page.locator("#af-pane-loadsheet").is_visible())
        brief = await page.inner_text("#af-pane-briefing")
        check("briefing shows the OFP figures", "SAM L620 HON" in brief and "FL360" in brief and "58,150" in brief, brief[-500:])

        await page.click("#af-pane-loadsheet button:has-text('ASK FOR LOADSHEET')")
        await page.wait_for_timeout(600)
        check("asking for the loadsheet tells load control whether the leg is full", extras_urls and "full=" in extras_urls[0], extras_urls[:1])
        sheet = await page.inner_text("#loadsheet-0")
        line = lambda label: (re.search(label + r" +(\d+)", sheet) or [None, None])[1]
        check("loadsheet built from the OFP", line("ZERO FUEL WEIGHT ACT") == "58150" and line("TAKE OFF WEIGHT ACT") == "64350"
              and line("PASSENGER/CABIN BAG") == "14364" and "G-EZWX" in sheet, sheet)
        check("changes pending until the captain decides", sheet.count("PENDING CAPTAIN DECISION") == 2)
        check("SIGN LOADSHEET is off while items are open", await page.locator("#signLoadsheetBtn-0").is_disabled())

        await page.click("#af-pane-loadsheet .ls-item:nth-child(1) button:has-text('DECLINE')")
        await page.click("#af-pane-loadsheet button:has-text('ACCEPT NOTOC')")
        await page.wait_for_timeout(300)
        sheet = await page.inner_text("#loadsheet-0")
        check("declined bags stay off, the accepted NOTOC goes on", line("ZERO FUEL WEIGHT ACT") == "58190" and "DECLINED" in sheet and "UN1845 CL 9 40 KG HOLD 1 ACCEPTED" in sheet, sheet)
        check("a change makes it edition 02", "02\n" in sheet.split("FROM/TO")[0])
        await page.screenshot(path=shot("af_loadsheet.png"))

        # A late change heavier than the margin can't be accepted
        await page.evaluate("""() => { const f = loadActiveFlight(); f.leg_state[0].ls.lmc_event = {id: 'lmc-06', type: 'cargo_change',
            description: 'Late hold cargo', delta: 9000, unit: 'kg'}; f.leg_state[0].ls.decisions = {dg: 'accept'}; saveAndRender(f); }""")
        await page.wait_for_timeout(300)
        check("a change over the weight limits can only be declined", await page.locator("#af-pane-loadsheet .ls-item:nth-child(1) button:has-text('ACCEPT')").is_disabled())
        await page.click("#af-pane-loadsheet .ls-item:nth-child(1) button:has-text('DECLINE')")
        await page.wait_for_timeout(300)

        await page.click("#signLoadsheetBtn-0")
        await page.wait_for_timeout(600)
        fl = await page.evaluate("loadActiveFlight()")
        ls = fl["leg_state"][0]["loadsheet"]
        check("signed loadsheet keeps its figures and text", fl["leg_state"][0]["loadsheet_signed"] and ls["zfw"] == 58190 and "SIGNED BY THE CAPTAIN" in ls["text"], str({k: ls.get(k) for k in ("zfw", "tow", "edition")}))
        check("step 4 FILE PIREP is next, on the PIREP tab", await page.evaluate("document.querySelector('.af-flow-step.now b').textContent") == "FILE PIREP" and await page.locator("#af-pane-pirep").is_visible())

        await page.click("#af-pane-pirep button:has-text('FILL PIREP')")
        await page.wait_for_timeout(300)
        await page.click("#af-pane-pirep button:has-text('SEND TO AIRCRAFT')")
        check("SEND TO AIRCRAFT is a placeholder for now", await page.locator("#sta-note-0").is_visible())
        L0 = fl["legs"][0]
        zm = lambda t: int(t[:2]) * 60 + int(t[3:5])

        async def fill(id_, m):
            await page.fill(f"#{id_}-hh", str((m // 60) % 24))
            await page.fill(f"#{id_}-mm", str(m % 60))
        sobt, sibt = zm(L0["sobt"]), zm(L0["sibt"])
        await fill("aobt-0", sobt)
        await fill("atot-0", sobt + 12)
        await fill("aldt-0", sibt - 8)
        await fill("abit-0", sibt)
        await page.fill("#afad-0", "3200")
        await page.check("#normal-0")
        await page.fill("#remarks-0", "Light chop over the Irish Sea")
        await page.screenshot(path=shot("af_pirep.png"))
        await page.click("#af-pane-pirep button:has-text('SIGN PIREP')")
        await page.wait_for_timeout(1000)
        fl = await page.evaluate("loadActiveFlight()")
        p0 = fl["leg_state"][0]["pirep"]
        check("PIREP keeps the pilot's remarks", p0 and p0.get("remarks") == "Light chop over the Irish Sea", str(p0))
        log = await page.evaluate("fetch('/pireps').then(r => r.json())")
        check("the logbook entry carries the loadsheet and remarks",
              any((r.get("pirep") or {}).get("remarks") and (r.get("loadsheet") or {}).get("text") for r in log))
        await page.evaluate("selectRecapLeg(0); selectRecapTab('pirep')")
        check("the filed PIREP reads back as a document", "PILOT REMARKS" in await page.inner_text("#af-pane-pirep"))
        check.done(page)
        await browser.close()


asyncio.run(main())
