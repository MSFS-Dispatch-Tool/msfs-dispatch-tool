"""Generation settings: the item tables, probability sliders and what they do to /select."""
import asyncio
import json
import urllib.request

from playwright.async_api import async_playwright

from common import BASE, Checks, launch, prepare, shot

check = Checks()


async def main():
    async with async_playwright() as p:
        b = await launch(p)
        page = await b.new_page(viewport={"width": 1600, "height": 1000}); await prepare(page)
        await page.goto(BASE + "/app"); await page.wait_for_timeout(1500)
        await page.evaluate("renderAccountSettings()"); await page.wait_for_timeout(1500)
        labels = await page.evaluate("['delay','lmc','mel','cdl'].map(k => document.getElementById('gen-'+k+'-prob-val').textContent)")
        check("four sliders, realistic by default", all("realistic" in l for l in labels), labels)
        counts = await page.evaluate("['delay','lmc','mel','cdl'].map(k => document.querySelectorAll('#gen-'+k+'-items tbody tr:not(.gen-group)').length)")
        options = json.load(urllib.request.urlopen(BASE + "/generation-options"))
        expected = [len(options[k]) for k in ("delay", "lmc", "mel", "cdl")]
        check("tables list every item", counts == expected, f"{counts} vs {expected}")
        await page.evaluate("document.querySelectorAll('.leg-block details').forEach(d => d.open = true)")
        mel_text = await page.inner_text("#gen-mel-items")
        check("MEL rows show effect and actions", "Cruise no higher than FL250 (set in SimBrief)" in mel_text and "at least 3,402 kg at take-off" in mel_text and "Cruise above FL250 not allowed" in mel_text and "(O)" not in mel_text and "(M)" not in mel_text)
        delay_text = await page.inner_text("#gen-delay-items")
        check("delay rows show range, slot and actions", "Comes with a take-off slot from air traffic control" in delay_text and "pick the reason in your flight report" in delay_text and "ATC flow restriction en route" in delay_text)
        el = page.locator("#gen-mel-items tbody tr:not(.gen-group)").nth(0)
        await el.scroll_into_view_if_needed()
        await page.screenshot(path=shot("settings_mel.png"))
        # set MEL 100, delay 0, LMC 0; uncheck first CDL
        for k, v in (("mel", 100), ("delay", 0), ("lmc", 0)):
            await page.evaluate(f"(() => {{ const i = document.getElementById('gen-{k}-prob'); i.value = {v}; i.dispatchEvent(new Event('input')); }})()")
        check("moving a slider marks it as your setting", "your setting" in await page.inner_text("#gen-mel-prob-val"))
        await page.locator("#gen-cdl-items input.gen-item").first.uncheck()
        await page.click("button[onclick*=saveGenerationSettings]"); await page.wait_for_timeout(1000)
        st = json.load(urllib.request.urlopen(BASE + "/settings"))["generation"]
        check("saved: probabilities and disabled item", st["mel"]["probability"] == 100 and st["delay"]["probability"] == 0 and st["lmc"]["probability"] == 0 and st["cdl"]["probability"] is None and len(st["cdl"]["disabled_ids"]) == 1, {k: st[k].get("probability") for k in st})
        mels = [len(json.load(urllib.request.urlopen(BASE + "/select?flights=U2765,U2766"))["legs"][0]["mels"]) for _ in range(15)]
        delays = [json.load(urllib.request.urlopen(BASE + "/select?flights=FR113"))["legs"][0]["delay"] for _ in range(15)]
        check("MEL 100%: every trip has at least one item", min(mels) >= 1, mels)
        check("delay 0%: no leg-1 delays", all(d is None for d in delays))
        await page.evaluate("renderAccountSettings()"); await page.wait_for_timeout(1500)
        check("reload shows saved values", "100%" in await page.inner_text("#gen-mel-prob-val") and "your setting" in await page.inner_text("#gen-mel-prob-val"))
        await page.click("#gen-mel-prob ~ button"); await page.click("#gen-delay-prob ~ button"); await page.click("#gen-lmc-prob ~ button")
        await page.click("button[onclick*=saveGenerationSettings]"); await page.wait_for_timeout(1000)
        st = json.load(urllib.request.urlopen(BASE + "/settings"))["generation"]
        check("REALISTIC resets to the default (null)", all(st[k]["probability"] is None for k in ("mel", "delay", "lmc")))
        await page.evaluate("document.querySelectorAll('.leg-block details').forEach(d => d.open = true)")
        await page.locator("#gen-cdl-items input.gen-item").first.check(); await page.click("button[onclick*=saveGenerationSettings]"); await page.wait_for_timeout(800)
        mob = await b.new_page(viewport={"width": 390, "height": 844}); await prepare(mob)
        await mob.goto(BASE + "/app"); await mob.wait_for_timeout(1200); await mob.evaluate("renderAccountSettings()"); await mob.wait_for_timeout(1500)
        await mob.evaluate("document.querySelectorAll('.leg-block details').forEach(d => d.open = true)")
        await mob.locator("#gen-mel-prob").scroll_into_view_if_needed()
        await mob.screenshot(path=shot("settings_mobile.png"))
        overflow = await mob.evaluate("document.documentElement.scrollWidth > window.innerWidth")
        check("no horizontal page scroll on phones", not overflow)
        check.done(page)
        await b.close()
asyncio.run(main())
