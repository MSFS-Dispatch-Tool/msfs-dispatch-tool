"""ATFM slots, curfews, crew duty and the knock-on delay after a late first leg."""
import asyncio
from datetime import date, datetime
from zoneinfo import ZoneInfo

from playwright.async_api import async_playwright

from common import BASE, Checks, launch, prepare, shot

check = Checks()

# Belfast City's curfew (23:00 local) in UTC today: 22:00Z in summer, 23:00Z
# in winter. The times this suite types in are worked out from it and from
# the flight's own schedule, so it passes in either season.
CURFEW_Z = datetime.combine(date.today(), datetime.strptime("23:00", "%H:%M").time(), ZoneInfo("Europe/London")).astimezone(ZoneInfo("UTC"))
CURFEW_MIN = CURFEW_Z.hour * 60 + CURFEW_Z.minute


async def main():
    async with async_playwright() as p:
        b = await launch(p)
        page = await b.new_page(viewport={"width": 1600, "height": 950}); await prepare(page)
        alerts = []
        page.on("dialog", lambda d: (alerts.append(d.message), asyncio.ensure_future(d.dismiss())))
        await page.goto(BASE + "/app?from=EGKK&to=EGAC&legs=RT"); await page.wait_for_timeout(2500)
        rows = page.locator("tr.itin-row")
        n = await rows.count()
        idx = 0
        for k in range(n):
            if "U2765" in await rows.nth(k).inner_text(): idx = k; break
        await rows.nth(idx).locator("button:has-text('SELECT')").click(); await page.wait_for_timeout(2500)
        detail = await page.inner_text("#detailCell")
        a0 = await page.evaluate("expandedDetailData.legs[0].atfm")
        ct = int(a0["ctot"][:2]) * 60 + int(a0["ctot"][3:5]); hm = lambda m: f"{(m // 60) % 24:02d}:{m % 60:02d}Z"
        check("preview shows slot with window and regulation", f"SLOT {a0['ctot']}" in detail and f"TAKE-OFF {hm(ct - 5)}-{hm(ct + 10)}" in detail and "LONDON ACC" in detail, a0["ctot"])
        check("preview shows Belfast City curfew margin", f"BELFAST CITY LANDING LIMIT 23:00 LT ({CURFEW_Z:%H:%M}Z)" in detail)
        d1 = await page.evaluate("expandedDetailData.legs[1]")
        d0 = await page.evaluate("expandedDetailData.legs[0]")
        check("leg 2: no own delay or slot, expected knock-on = leg 1 delay (30-min turnaround)", d1["delay"] is None and d1["atfm"] is None and d1["reactionary"]["expected"] and d1["reactionary"]["minutes"] == d0["expected_delay_minutes"], str(d1.get("reactionary")))
        check("preview labels the expected knock-on", "LATE INBOUND ACFT EXPECTED FROM PREVIOUS SECTOR" in detail)
        check("preview shows crew duty", "CREW REPORT 14:30 LT" in detail and "MAX FDP 12H 15M, 2 SECTORS" in detail)
        if await page.locator(".nogo-banner").count():
            await page.click("#swapAircraftBtn"); await page.wait_for_timeout(1500)
        await page.locator(".confirm-row button").click(); await page.wait_for_timeout(2000)
        await page.evaluate("""() => { const f = loadActiveFlight();
          f.leg_state.forEach(st => { st.simbrief = 'available'; st.ofp = {weight_unit: 'kgs'}; st.loadsheet_signed = true; st.loadsheet = {zfw: 1}; st.pirep_open = true; });
          saveAndRender(f); }""")
        await page.wait_for_timeout(500)
        fl = await page.evaluate("loadActiveFlight()")
        L0 = fl["legs"][0]
        zm = lambda t: int(t[:2]) * 60 + int(t[3:5])
        async def fill(id_, m):
            m %= 1440
            await page.fill(f"#{id_}-hh", str(m // 60)); await page.fill(f"#{id_}-mm", str(m % 60))
        # leg 1: off-block at SOBT+40 (delay coding), take-off 6 minutes after
        # the CTOT (slot met), in-block 10 minutes before the Belfast City
        # curfew: so late that leg 2 can't take off before it
        sobt = zm(L0["sobt"])
        atot = zm(L0["atfm"]["ctot"]) + 6
        abit = CURFEW_MIN - 10
        await page.evaluate("selectRecapLeg(0); selectRecapTab('pirep')"); await page.wait_for_timeout(300)
        await fill("aobt-0", sobt + 40); await page.wait_for_timeout(200)
        await fill("atot-0", atot); await fill("aldt-0", abit - 10); await fill("abit-0", abit)
        await page.fill("#afad-0", "2400"); await page.check("#normal-0")
        await page.click("#afNextBtn"); await page.wait_for_timeout(1000)
        fl = await page.evaluate("loadActiveFlight()")
        p0 = fl["leg_state"][0]["pirep"]
        exp_delta = 6
        check("PIREP records slot compliance vs CTOT", p0 and p0.get("slot") == {"ctot": L0["atfm"]["ctot"], "delta": exp_delta, "met": -5 <= exp_delta <= 10}, str(p0 and p0.get("slot")))
        L1 = fl["legs"][1]
        knock_on = (abit + 30 - zm(L1["sobt"])) % 1440    # in-block + the 30-minute turnaround, against leg 2's schedule
        check("leg 2 knock-on replaced by the actual one after the PIREP", L1["reactionary"]["expected"] is False and L1["reactionary"]["minutes"] == knock_on and L1["atfm"] is None, str(L1["reactionary"]))
        await page.evaluate("selectRecapLeg(1); selectRecapTab('briefing')"); await page.wait_for_timeout(400)
        brief = await page.inner_text("#af-pane-briefing")
        check("leg 2 take-off after Belfast City curfew is flagged as a cancellation", "AFTER BELFAST CITY TAKE-OFF LIMIT 23:00 LT" in brief and "FLIGHT CANCELLED" in brief)
        check("briefing shows the actual (not expected) knock-on", f"DELAY +{knock_on} MIN" in brief and "LATE INBOUND ACFT FROM PREVIOUS SECTOR" in brief)
        check("duty check still shown with expected FDP", "EXPECTED FDP" in brief.upper())
        nogo_red = await page.evaluate("[...document.querySelectorAll('#af-pane-briefing .sheet-row.nogo')].length")
        check("curfew breach rendered as red", nogo_red >= 1)
        await page.screenshot(path=shot("ops_briefing.png"))
        log = await page.evaluate("fetch('/pireps').then(r => r.json())")
        check("PIREP log entry carries slot", any((r.get("pirep") or {}).get("slot") for r in log))
        check.done(page)
        await b.close()
asyncio.run(main())
