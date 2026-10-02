"""A320 deferred items: NO-GO in thunderstorms, the aircraft swap, procedure cards and SimBrief inputs."""
import asyncio

from playwright.async_api import async_playwright

from common import BASE, Checks, launch, prepare, shot

check = Checks()


async def main():
    async with async_playwright() as p:
        b = await launch(p)
        page = await b.new_page(viewport={"width": 1600, "height": 950}); await prepare(page)
        await page.goto(BASE + "/app?from=EGKK&legs=RT"); await page.wait_for_timeout(2500)
        await page.locator("tr.itin-row").first.locator("button:has-text('SELECT')").click(); await page.wait_for_timeout(2500)
        banner = page.locator(".nogo-banner")
        check("NO-GO banner shown for weather radar + forecast TS", await banner.count() == 1 and "Weather radar inoperative with thunderstorms" in await banner.inner_text() and "AIRCRAFT NOT DISPATCHABLE" in await banner.inner_text())
        confirm = page.locator(".confirm-row button")
        check("CONFIRM disabled while NO-GO", await confirm.is_disabled())
        txt = await page.inner_text("#detailCell")
        check("preview lists MEL items, CDL and SimBrief inputs", all(s in txt for s in ["MEL 34-41-01", "Air conditioning pack", "CDL ", "Included in your SimBrief plan", "FL315"]))
        await page.locator(".nogo-banner").scroll_into_view_if_needed()
        await page.screenshot(path=shot("mel_nogo.png"))
        d0 = await page.evaluate("expandedDetailData.legs[0]")
        eobt_before, exp_before = d0["eobt"], d0.get("expected_delay_minutes") or 0
        leg2_before = await page.evaluate("expandedDetailData.legs[1].expected_delay_minutes || 0")
        await page.click("#swapAircraftBtn"); await page.wait_for_timeout(2000)
        check("after swap: no NO-GO, CONFIRM enabled", await page.locator(".nogo-banner").count() == 0 and not await confirm.is_disabled())
        d0 = await page.evaluate("expandedDetailData.legs[0]")
        ac = d0.get("aircraft_change") or {}
        check("swap adds code 46 delay of 20-45 min on leg 1", ac.get("iata_code") == "46" and 20 <= ac.get("minutes", 0) <= 45, str(ac))
        check("leg-1 expected delay and EOBT moved by the swap delay", d0["expected_delay_minutes"] == exp_before + ac["minutes"], f'{eobt_before} -> {d0["eobt"]}')
        leg2_after = await page.evaluate("expandedDetailData.legs[1].expected_delay_minutes || 0")
        check("swap delay carries into leg 2 (30-min turnaround)", leg2_after == max(leg2_before, d0["expected_delay_minutes"]), f"{leg2_before} -> {leg2_after}")
        check("swapped aircraft has no weather radar item", all(m["system"] != "Weather radar" for m in d0["mels"]), str([m["system"] for m in d0["mels"]]))
        await page.screenshot(path=shot("mel_swapped.png"))
        await confirm.click(); await page.wait_for_timeout(2000)
        tech = await page.inner_text("#af-pane-briefing")
        check("briefing technical log shows the procedure cards", "Cruise above FL315 not allowed" in tech and "fix within" not in tech.lower() and "CDL " in tech and "(M)" not in tech and "(O)" not in tech)
        check("only three tabs: briefing, loadsheet, PIREP", await page.evaluate("[...document.querySelectorAll('.af-tabbar button')].map(b => b.textContent).join(',')") == "BRIEFING,LOADSHEET,PIREP")
        await page.screenshot(path=shot("mel_tab.png"))
        check("defect steps are a plain list, no tick boxes", await page.locator("#af-pane-briefing .tcard input").count() == 0
              and await page.locator("#af-pane-briefing .tcard-steps li").count() > 0)
        await page.evaluate("selectRecapTab('briefing')"); await page.wait_for_timeout(200)
        brief = await page.inner_text("#af-pane-briefing")
        check("briefing shows DELAY 46", "Aircraft change" in brief)
        req = []
        page.on("request", lambda r: req.append(r.url) if "redirect-url" in r.url else None)
        await page.evaluate("doPrepareLeg(0)"); await page.wait_for_timeout(800)
        u = req[0] if req else ""
        check("SimBrief request carries max_fl, extra fuel and remarks", "max_fl=315" in u and "extra_fuel_min=" in u and "remarks=MEL" in u, u[:300])
        r = await page.evaluate(f"fetch('{u.replace(BASE, '')}').then(r => r.json())")
        url = r.get("url", "")
        check("SimBrief URL has fl=FL315, addedfuel in minutes, manualrmk", "fl=FL315" in url and "addedfuel_units=min" in url and "manualrmk=MEL" in url, url[-200:])
        from urllib.parse import parse_qs, urlparse
        rmk = parse_qs(urlparse(url).query).get("manualrmk", [""])[0]
        check("SimBrief remarks carry every defect in full, one line each",
              rmk.count("\n") >= 4 and "MEL PROVISOS:" in rmk and "CREW ACTIONS:" in rmk and "PACK" in rmk and "CDL " in rmk, rmk[:400])
        opts = await page.evaluate("fetch('/generation-options').then(r => r.json())")
        check("generation options include CDL and hide never-rolled items", len(opts["cdl"]) == 16 and not any(m["id"].endswith("mel-05") for m in opts["mel"]), f'{len(opts["cdl"])} cdl, {len(opts["mel"])} mel')
        check.done(page)
        await b.close()
asyncio.run(main())
