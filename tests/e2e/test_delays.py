"""Delays across a two-leg trip: delay coding in the PIREP and the knock-on into leg 2."""
import asyncio

from playwright.async_api import async_playwright

from common import BASE, Checks, launch, prepare, shot

check = Checks()


async def main():
    async with async_playwright() as p:
        b = await launch(p)
        page = await b.new_page(viewport={"width": 1600, "height": 950}); await prepare(page)
        page.on("dialog", lambda d: asyncio.ensure_future(d.dismiss()))
        alerts = []
        page.on("dialog", lambda d: alerts.append(d.message))
        await page.goto(BASE + "/app?from=EGKK&legs=RT"); await page.wait_for_timeout(2500)
        rows = page.locator("tr.itin-row")
        idx = 0
        await rows.nth(idx).locator("button:has-text('SELECT')").click(); await page.wait_for_timeout(2500)
        await page.locator("button:has-text('CONFIRM')").first.click(); await page.wait_for_timeout(2000)
        fl = await page.evaluate("loadActiveFlight()")
        check("active flight with 2 legs", fl and len(fl["legs"]) == 2, str(len(fl["legs"]) if fl else None))
        d0 = fl["legs"][0].get("delay")
        check("rolled delay has sampled minutes inside range", d0 is None or (d0["duration_range_minutes"][0] <= d0["minutes"] <= d0["duration_range_minutes"][1]))
        # force a known state: leg0 own delay 20 (code 81), leg1 own delay 10 (code 17); both legs ready for PIREP
        await page.evaluate("""() => { const f = loadActiveFlight();
          const setDelay = (leg, code, mins) => { const before = legExpectedDelayMinutes(leg);
            leg.delay = {iata_code: code, description: code === '81' ? 'ATFM due to ATC en-route demand/capacity' : 'Catering order, late or incorrect order given to supplier', duration_range_minutes: [5, 60], minutes: mins};
            leg.expected_delay_minutes = mins; ['eobt','etot','eldt','eibt'].forEach(x => leg[x] = shiftZulu(leg[x], mins - before)); };
          setDelay(f.legs[0], '81', 20); setDelay(f.legs[1], '17', 10);
          f.leg_state.forEach(st => { st.simbrief = 'received'; st.ofp = {weight_unit: 'kgs'}; st.loadsheet_signed = true; st.loadsheet = {zfw: 1}; });
          saveAndRender(f); }""")
        await page.wait_for_timeout(800)
        fl = await page.evaluate("loadActiveFlight()")
        L0, L1 = fl["legs"]
        zm = lambda s: int(s[:2]) * 60 + int(s[3:5])
        # Fill PIREP leg 0: off-block SOBT+27 (so a 27-min delay to code), in-block 50 min after SIBT -> reactionary
        await page.evaluate("selectRecapLeg(0)"); await page.wait_for_timeout(300)
        await page.evaluate("selectRecapTab('next')"); await page.wait_for_timeout(300)
        sobt0, sibt0, sobt1 = zm(L0["sobt"]), zm(L0["sibt"]), zm(L1["sobt"])
        aobt = sobt0 + 27; abit = sibt0 + 50
        async def fill(id_, m):
            await page.fill(f"#{id_}-hh", str((m // 60) % 24)); await page.fill(f"#{id_}-mm", str(m % 60))
        await fill("aobt-0", aobt)
        await page.wait_for_timeout(300)
        box_open = await page.evaluate("document.getElementById('delaycoding-0').classList.contains('open')")
        check("delay coding opens for a 27-min late off-block", box_open)
        pre = await page.evaluate("[0,1].map(k => [document.getElementById('dc-code-0-'+k).value, document.getElementById('dc-min-0-'+k).value])")
        check("prefilled with the predicted code 81 for 27 min", pre[0] == ["81", "27"], str(pre))
        await page.screenshot(path=shot("pirep_coding.png"), full_page=False)
        # Wrong sum blocks submit
        await page.fill("#dc-min-0-0", "20")
        await page.dispatch_event("#dc-min-0-0", "input")
        await page.wait_for_timeout(200)
        check("sum warning shown", "must add up" in await page.inner_text("#dc-sum-0"))
        await fill("atot-0", aobt + 12); await fill("aldt-0", abit - 6); await fill("abit-0", abit)
        await page.fill("#afad-0", "2500"); await page.check("#normal-0")
        await page.click("#pirep-0 button:has-text('SUBMIT PIREP')"); await page.wait_for_timeout(500)
        check("submit blocked when minutes don't add up", any("add up" in a for a in alerts), str(alerts[-1:]))
        # second row: code 63 for 7 min
        await page.select_option("#dc-code-0-1", "63"); await page.fill("#dc-min-0-1", "7"); await page.dispatch_event("#dc-min-0-1", "input")
        await page.click("#pirep-0 button:has-text('SUBMIT PIREP')"); await page.wait_for_timeout(1000)
        fl = await page.evaluate("loadActiveFlight()")
        p0 = fl["leg_state"][0]["pirep"]
        check("PIREP stored the two codes", p0 and p0.get("delay_codes") and [c["code"] for c in p0["delay_codes"]] == ["81", "63"], str(p0 and p0.get("delay_codes")))
        L1n = fl["legs"][1]
        expect_r = max(0, ((abit + 30 - sobt1 + 720) % 1440) - 720)
        check("reactionary 93 computed from actual in-block + 30 min turnaround", (L1n.get("reactionary") or {}).get("minutes", 0) == expect_r, f"expected {expect_r}")
        check("expected delay = max(reactionary, own 10)", L1n["expected_delay_minutes"] == max(expect_r, 10))
        check("EOBT shifted accordingly", zm(L1n["eobt"]) == (sobt1 + max(expect_r, 10)) % 1440, L1n["eobt"])
        await page.evaluate("selectRecapLeg(1); selectRecapTab('briefing')"); await page.wait_for_timeout(400)
        await page.screenshot(path=shot("leg2_briefing.png"))
        # server PIREP has delay codes; stats uses main code 81
        st = await page.evaluate("fetch('/stats/flights').then(r => r.json())")
        last = st["flights"][-1]
        check("stats row uses pilot main code", last["delay_code"] == "81" and last["delay_coded"] is True, f'{last["delay_code"]} {last.get("delay_coded")}')
        # SimBrief prepare uses expected delay for leg 2
        req = []
        page.on("request", lambda r: req.append(r.url) if "redirect-url" in r.url else None)
        await page.evaluate("doPrepareLeg(1)"); await page.wait_for_timeout(800)
        check("SimBrief gets leg-2 expected delay + EOBT", req and f"delay_minutes={L1n['expected_delay_minutes']}" in req[0] and ("eobt=" + L1n["eobt"][:2]) in req[0], req[:1])
        check.done(page)
        await b.close()
asyncio.run(main())
