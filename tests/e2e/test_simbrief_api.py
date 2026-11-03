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
              options[0] == f"SimBrief standard {info['type']}" and len(options) >= 2
              and all(info["type"] in o for o in options), options)
        check("the leg starts with the airframe labelled for its airline", "(EZY - " in selected, selected)
        check("step 1 says the OFP is generated on SimBrief, with a GENERATE OFP button",
              "Generates the OFP on SimBrief" in await page.text_content("#sbSendNote-0")
              and (await page.text_content("#sbSendBtn-0")).strip() == "GENERATE OFP")
        await page.locator(".af-flow").screenshot(path=shot("simbrief_airframe_picker.png"))

        await page.click("#sbSendBtn-0")
        await page.wait_for_function("loadActiveFlight().leg_state[0].simbrief === 'available'", timeout=10000)
        sent = json.load(urllib.request.urlopen(BASE + "/__simbrief/last"))
        leg = await page.evaluate("loadActiveFlight().legs[0]")
        state = await page.evaluate("loadActiveFlight().leg_state[0]")
        check("the plan is generated with the chosen airframe and the leg's own options",
              sent["type"] == info["selected"] and sent["orig"] == leg["departure_info"]["icao"]
              and sent["dest"] == leg["arrival_info"]["icao"] and sent["static_id"] == state["simbrief_static_id"], sent)
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
              "G-EZ" in done and "RWY 06" in done and "FL340" in done and "OFP generated 18:33Z" in done, done)
        await page.screenshot(path=shot("simbrief_ofp_in.png"))

        # SimBrief refuses: the pilot is told and simbrief.com opens as before
        await page.click(".af-flow-step.done button:has-text('NEW OFP')"); await page.wait_for_selector("#sbAirframeSel-0")
        await page.evaluate("simbriefApiLegs[simbriefApiKey(loadActiveFlight(), 0)].selected = ''")
        urllib.request.urlopen(urllib.request.Request(BASE + "/__simbrief/refuse", method="POST"))
        await page.wait_for_timeout(10500)                                    # one plan per 10 seconds
        await page.click("#sbSendBtn-0")
        await page.wait_for_function("loadActiveFlight().leg_state[0].simbrief === 'sent'", timeout=10000)
        sent = json.load(urllib.request.urlopen(BASE + "/__simbrief/last"))
        check("SimBrief's standard aircraft is sent by its type", sent["type"] == info["type"], sent["type"])
        check("a refusal says why and opens SimBrief instead",
              alerts and "Invalid aircraft type" in alerts[-1] and "Opening SimBrief instead" in alerts[-1] and len(popups) == 1, alerts)
        check.done(page)
        await browser.close()
asyncio.run(main())
