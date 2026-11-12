"""SEND TO SIMBRIEF through SimBrief's API: a pilot signed in with Navigraph
picks one of their saved airframes, the plan is generated in one click and
the OFP lands in the briefing; a refusal falls back to simbrief.com."""
import asyncio
import json
import urllib.request

from playwright.async_api import async_playwright

from common import BASE, Checks, launch, prepare, shot

check = Checks()


async def main():
    async with async_playwright() as p:
        browser = await launch(p)
        ctx = await browser.new_context(viewport={"width": 1500, "height": 940})
        await ctx.add_init_script("localStorage.setItem('simdispatch_simbrief_username', 'skyline_ops');"
                                  "localStorage.setItem('simdispatch_simbrief_aircraft_notice_dismissed', '1');")
        page = await prepare(await ctx.new_page())
        alerts = []
        page.on("dialog", lambda d: (alerts.append(d.message), asyncio.ensure_future(d.accept())))
        popups = []
        page.on("popup", lambda pop: popups.append(pop))
        await page.route("**/simbrief/redirect-url*", lambda r: r.fulfill(status=200, content_type="application/json", body='{"url": "about:blank"}'))

        # Signed in with Navigraph (the fake one)
        await page.goto(BASE + "/app"); await page.wait_for_timeout(1200)
        await page.evaluate("renderAccountSettings()"); await page.wait_for_selector("#navigraphSignIn:not([hidden])")
        await page.click("#navigraphSignIn")
        await page.wait_for_selector("#navigraphDisconnect:not([hidden])")

        # An easyJet rotation Gatwick - Belfast City, confirmed
        await page.goto(BASE + "/app?from=EGKK&to=EGAC&legs=RT", wait_until="networkidle")
        await page.wait_for_function("searchStatus === 'ready'")
        await page.evaluate("toggleItinerary(itinKey(tableVisibleItineraries[0]))")
        await page.wait_for_selector(".confirm-row button", timeout=20000)
        if await page.locator("#swapAircraftBtn").count():
            await page.click("#swapAircraftBtn"); await page.wait_for_timeout(1500)
        await page.click(".confirm-row button")
        await page.wait_for_selector("#sbAirframeSel-0")

        info = await page.evaluate("simbriefApiLegs[simbriefApiKey(loadActiveFlight(), 0)]")
        options = await page.evaluate("[...document.querySelectorAll('#sbAirframeSel-0 option')].map(o => o.textContent)")
        selected = await page.evaluate("document.getElementById('sbAirframeSel-0').selectedOptions[0].textContent")
        check("the picker lists SimBrief's standard aircraft and the pilot's airframes of the leg's type",
              options[0] == f"SIMBRIEF STANDARD {info['type']}" and len(options) >= 2
              and all(info["type"] in o for o in options), options)
        check("the leg starts with the airframe labelled for its airline", "(EZY - " in selected, selected)
        check("the plan says the OFP is generated on SimBrief, and the bar's action is GENERATE OFP",
              "OFP GENERATED ON SIMBRIEF" in await page.text_content("#sbSendNote-0")
              and "GENERATE OFP" in await page.text_content("#afNextBtn"))
        await page.wait_for_selector("#sbRunwaySel-0")
        rwys = await page.inner_text("#sbRunways-0")
        check("RUNWAYS reads VATSIM by default: Gatwick's ATIS, nothing at Belfast City",
              await page.eval_on_selector("#sbRunwaySel-0", "s => s.value") == "vatsim"
              and "EGKK DEP 08R VATSIM ATIS F" in rwys and "NO VATSIM ATIS RWY. SIMBRIEF BY WIND" in rwys, rwys)
        await page.select_option("#sbRunwaySel-0", "sayintentions"); await page.wait_for_timeout(500)
        rwys = await page.inner_text("#sbRunways-0")
        check("switching to SayIntentions reads its ATIS", "08R SAYINTENTIONS ATIS O" in rwys, rwys)
        await page.select_option("#sbRunwaySel-0", "vatsim"); await page.wait_for_timeout(500)
        await page.wait_for_selector("#sbAltnSel-0")
        altns = await page.evaluate("[...document.querySelectorAll('#sbAltnSel-0 option')].map(o => o.textContent.toUpperCase())")
        groups = await page.evaluate("[...document.querySelectorAll('#sbAltnSel-0 optgroup')].map(g => g.label)")
        check("ALTERNATE offers easyJet's airports near Belfast City, nearest base first, or SimBrief's choice",
              altns[0] == "EGAA BELFAST · 12 NM" and any(a.startswith("EGPF GLASGOW") for a in altns)
              and altns[-1] == "SIMBRIEF CHOOSES" and groups[0] == "AIRLINE BASES" and groups[-1] == "SIMBRIEF", (altns, groups))
        ofp = await page.inner_text("#sbOfp-0")
        check("OFP options start in KG with flight maps on and stepclimbs off",
              await page.eval_on_selector("input[name=sbUnits-0]:checked", "r => r.value") == "KGS"
              and await page.is_checked("[data-opt=maps]") and not await page.is_checked("[data-opt=stepclimbs]")
              and "RUNWAY ANALYSIS" in ofp and "FIR NOTAMS" in ofp and "LAYOUT\nEZY" in ofp, ofp)
        await page.select_option("#sbAltnSel-0", "EGPF")
        await page.check("[data-opt=stepclimbs]")
        check("a changed option applies to this plan only until it's saved as default",
              "CHANGED FOR THIS PLAN ONLY" in await page.inner_text("#sbOfpSave-0") and await page.is_enabled("#sbOfpSaveBtn-0"))
        await page.click("#sbOfpSaveBtn-0"); await page.wait_for_selector("#sbOfpSaveBtn-0:disabled")
        check("SAVE AS DEFAULT keeps the options for the next plans",
              "SAVED" in await page.inner_text("#sbOfpSave-0")
              and (await page.evaluate("fetch('/simbrief/plan-options?flight=U2803').then(r => r.json())"))["options"]["stepclimbs"] is True)
        await page.locator(".af-tabs").screenshot(path=shot("simbrief_airframe_picker.png"))

        await page.click("#afNextBtn")
        await page.wait_for_function("loadActiveFlight().leg_state[0].simbrief === 'available'", timeout=10000)
        sent = json.load(urllib.request.urlopen(BASE + "/__simbrief/last"))
        leg = await page.evaluate("loadActiveFlight().legs[0]")
        state = await page.evaluate("loadActiveFlight().leg_state[0]")
        check("the plan is generated with the chosen airframe and the leg's own options",
              sent["type"] == info["selected"] and sent["orig"] == leg["departure_info"]["icao"]
              and sent["dest"] == leg["arrival_info"]["icao"] and sent["static_id"] == state["simbrief_static_id"], sent)
        check("the ATIS runway goes into the plan, none where there's no ATIS", sent.get("origrwy") == "08R" and "destrwy" not in sent, sent)
        check("the chosen alternate, easyJet's layout and the OFP options go into the plan",
              sent.get("altn") == "EGPF" and sent.get("planformat") == "EZY" and sent.get("units") == "KGS"
              and sent.get("maps") == "detail" and str(sent.get("stepclimbs")) == "1" and str(sent.get("notams")) == "1", sent)
        check("the departure time goes inside the date", sent["date"].endswith(" - " + leg["eobt"][:5]) and "deph" not in sent,
              (sent["date"], leg["eobt"]))
        check("the OFP lands in the briefing, with no SimBrief tab opened",
              state["ofp"]["source"] == "api" and state["ofp"]["registration"].startswith("G-EZ") and popups == [] and alerts == [],
              (state["ofp"].get("registration"), popups, alerts))
        check("the briefing stays on screen, from the top, with the OFP in it",
              await page.locator("#af-pane-briefing").is_visible() and await page.evaluate("document.getElementById('af-pane-briefing').scrollTop") == 0
              and "RUNWAYS" in await page.inner_text("#brief-ofp"))
        done = await page.inner_text(".af-flow-step.done")
        check("step 1 sums up the OFP: registration, runways, cruise, generated time",
              "G-EZ" in done and "RWY 06" in done and "FL340" in done and "OFP 18:33Z" in done, done)
        await page.screenshot(path=shot("simbrief_ofp_in.png"))

        # SimBrief refuses: the pilot is told and simbrief.com opens as before
        await page.click("#af-tab-plan"); await page.wait_for_selector("#sbAirframeSel-0")
        check("back on the PLAN tab, the bar offers a new OFP and the current one is summed up",
              "GENERATE NEW OFP" in await page.text_content("#afNextBtn") and "CURRENT OFP" in await page.inner_text("#af-pane-plan"))
        await page.evaluate("simbriefApiLegs[simbriefApiKey(loadActiveFlight(), 0)].selected = ''")
        urllib.request.urlopen(urllib.request.Request(BASE + "/__simbrief/refuse", method="POST"))
        await page.wait_for_timeout(10500)                                    # one plan per 10 seconds
        await page.click("#afNextBtn")
        await page.wait_for_function("loadActiveFlight().leg_state[0].simbrief === 'sent'", timeout=10000)
        sent = json.load(urllib.request.urlopen(BASE + "/__simbrief/last"))
        check("SimBrief's standard aircraft is sent by its type", sent["type"] == info["type"], sent["type"])
        await page.evaluate("fetch('/simbrief/plan-options', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({options: {stepclimbs: false}})})")   # back to the defaults
        check("a refusal says why and opens SimBrief instead",
              alerts and "INVALID AIRCRAFT TYPE" in alerts[-1] and "OPENING SIMBRIEF" in alerts[-1] and len(popups) == 1, alerts)
        check.done(page)
        await browser.close()
asyncio.run(main())
