"""737 fuel pump items: the fuel minimum and centre-tank limit at loadsheet signing."""
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
        await page.evaluate("selectRecapTab('tech')"); await page.wait_for_timeout(400)
        tech = await page.inner_text("#af-pane-tech")
        check("MEL tab: installed/required, FAA source, MMEL provisos", "Official MEL wording" in tech and "Load at least 3,402 kg in each main tank" in tech and "(M)" not in tech and "INSTALLED" not in tech, "")
        await page.screenshot(path=shot("mel738_tab.png"))
        await page.evaluate("""() => { const f = loadActiveFlight(); const st = f.leg_state[0];
          st.simbrief = 'received'; st.ofp = {weight_unit: 'kgs'}; saveAndRender(f); }""")
        await page.wait_for_timeout(500)
        await page.evaluate("selectRecapLeg(0); selectRecapTab('next')"); await page.wait_for_timeout(300)
        for f, v in (("zfw", "60000"), ("fob", "5000"), ("pax", "180"), ("crew", "6")):
            await page.fill(f"#ls-{f}-0", v)
        await page.click("#loadsheet-0 button:has-text('SIGN LOADSHEET')"); await page.wait_for_timeout(600)
        signed = await page.evaluate("loadActiveFlight().leg_state[0].loadsheet_signed")
        check("loadsheet below MEL fuel minimum asks, and cancelling keeps it unsigned", dialogs and "below the MEL take-off minimum of 6,804 kg" in dialogs[0] and not signed, dialogs[:1])
        await page.fill("#ls-fob-0", "9000")
        await page.click("#loadsheet-0 button:has-text('SIGN LOADSHEET')"); await page.wait_for_timeout(600)
        check("FOB above the centre-tank-empty limit warns too", len(dialogs) >= 2 and "7,830 kg the main tanks hold" in dialogs[1], dialogs[1:2])
        check.done(page)
        await b.close()
asyncio.run(main())
