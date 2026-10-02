"""737 fuel pump items: the fuel minimum and centre-tank limit on the loadsheet."""
import asyncio

from playwright.async_api import async_playwright

from common import BASE, Checks, launch, prepare, shot

check = Checks()


async def main():
    async with async_playwright() as p:
        b = await launch(p)
        page = await b.new_page(viewport={"width": 1600, "height": 950}); await prepare(page)
        dialogs = []
        async def on_dialog(d):
            dialogs.append(d.message); await (d.dismiss() if len(dialogs) == 1 else d.accept())
        page.on("dialog", lambda d: asyncio.ensure_future(on_dialog(d)))
        await page.goto(BASE + "/app?from=EIDW&legs=RT&airline=RYR"); await page.wait_for_timeout(2500)
        rows = page.locator("tr.itin-row")
        idx = 0
        for k in range(await rows.count()):
            if "FR" in await rows.nth(k).inner_text(): idx = k; break
        await rows.nth(idx).locator("button:has-text('SELECT')").click(); await page.wait_for_timeout(2500)
        detail = await page.inner_text("#detailCell")
        check("preview: FAA item numbers and fuel minimum", "Main tank fuel pump, aft" in detail and "minimum 6,804 kg at take-off" in detail and "Centre tank unusable" in detail, "")
        check("preview: pack FL250 sent to SimBrief", "cruise no higher than FL250" in detail)
        await page.locator(".confirm-row button").click(); await page.wait_for_timeout(2000)
        tech = await page.inner_text("#af-pane-briefing")
        check("MEL tab: installed/required, FAA source, MMEL provisos", "Official MEL wording" in tech and "Load at least 3,402 kg in each main tank" in tech and "(M)" not in tech and "INSTALLED" not in tech, "")
        await page.screenshot(path=shot("mel738_tab.png"))
        # Loadsheet: an OFP with too little take-off fuel for the pump minimum
        await page.route("**/loadsheet/extras*", lambda r: r.fulfill(status=200, content_type="application/json",
                         body='{"lmc_event": null, "dangerous_goods": {"id": "dg-05", "label": "No dangerous goods on this sector"}}'))
        ofp = {"weight_unit": "kgs", "dow": "41500", "pax_weight": "84", "payload": "16000", "est_zfw": "57500", "max_zfw": "62732",
               "max_tow": "79015", "max_ldw": "66360", "takeoff_fuel": "5000", "taxi_fuel": "200", "trip_fuel": "3000", "epax": "180"}
        await page.evaluate("""(ofp) => { const f = loadActiveFlight(); const st = f.leg_state[0];
          st.simbrief = 'available'; st.ofp = ofp; saveAndRender(f); }""", ofp)
        await page.wait_for_timeout(400)
        await page.click("#af-pane-loadsheet button:has-text('ASK FOR LOADSHEET')"); await page.wait_for_timeout(600)
        pane = await page.inner_text("#af-pane-loadsheet")
        check("loadsheet flags take-off fuel below the MEL minimum", "below the MEL minimum of 6,804 KG" in pane, pane[-400:])
        check("SIGN LOADSHEET stays off until the fuel is sorted", await page.locator("#signLoadsheetBtn-0").is_disabled())
        await page.click("#af-pane-loadsheet button:has-text('UPLIFT FUEL')"); await page.wait_for_timeout(400)
        sheet = await page.inner_text("#loadsheet-0")
        check("uplift brings the take-off fuel to the minimum", "TAKE OFF FUEL               6900" in sheet and "FUEL UPLIFT" in sheet, sheet[:900])
        check("then the loadsheet can be signed", not await page.locator("#signLoadsheetBtn-0").is_disabled())
        await page.screenshot(path=shot("mel738_loadsheet.png"))
        # Centre tank unusable: block fuel above what the main tanks hold
        await page.evaluate("""() => { const f = loadActiveFlight(); const st = f.leg_state[0];
          st.ofp = Object.assign({}, st.ofp, {takeoff_fuel: '9000'}); st.ls.decisions = {}; saveAndRender(f); }""")
        await page.wait_for_timeout(400)
        pane = await page.inner_text("#af-pane-loadsheet")
        check("block fuel above the main tanks asks for a defuel", "the main tanks hold (centre tank unusable). Defuel 1,400 KG" in pane, pane[-400:])
        check.done(page)
        await b.close()
asyncio.run(main())
