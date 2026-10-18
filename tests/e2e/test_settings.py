"""Generation settings: the item tables, probability sliders and what they do to /select."""
import asyncio
import json
import os
import urllib.request

import psycopg2

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
        # ACARS: the logon code is saved on the server, shown masked, never sent back
        await page.fill("#acarsLogon", "bad code")
        await page.click("button[onclick*=saveAcarsLogon]")
        check("an invalid Hoppie code is refused", await page.is_visible("#acarsLogonError"))
        await page.fill("#acarsLogon", "WRONGCODE9")
        await page.click("button[onclick*=saveAcarsLogon]"); await page.wait_for_timeout(600)
        await page.click("#acarsTestBtn"); await page.wait_for_timeout(800)
        check("a code Hoppie rejects is reported", "doesn't recognise" in await page.text_content("#acarsStatus"))
        await page.wait_for_timeout(5200)                                     # the test is rate limited
        await page.fill("#acarsLogon", "TESTLOGON1")
        await page.click("button[onclick*=saveAcarsLogon]"); await page.wait_for_timeout(600)
        saved = await page.text_content("#acarsSaved")
        await page.click("#acarsTestBtn"); await page.wait_for_timeout(800)
        check("a good code is saved masked and the test connects",
              "N1" in saved and "TESTLOG" not in saved and "Connected" in await page.text_content("#acarsStatus"), saved)
        raw = urllib.request.urlopen(BASE + "/settings").read().decode() + urllib.request.urlopen(BASE + "/acars/settings").read().decode()
        check("the code never comes back to the browser", "TESTLOGON1" not in raw)
        await page.locator(".panel:has(#acarsLogon)").screenshot(path=shot("settings_acars.png"))

        # ACARS through SayIntentions: the network choice, the key (kept on the server) and a test telex
        await page.click("#acarsNetworkRow label:has-text('SAYINTENTIONS')"); await page.wait_for_timeout(500)
        check("choosing SayIntentions shows its key field instead of Hoppie's",
              await page.is_visible("#siKey") and not await page.is_visible("#acarsLogon"))
        await page.fill("#siKey", "TESTSIKEY0255")
        await page.click("button[onclick*=saveSiKey]"); await page.wait_for_timeout(600)
        await page.wait_for_timeout(5200)                                     # the test is rate limited
        await page.click("#siTestBtn"); await page.wait_for_timeout(800)
        raw = urllib.request.urlopen(BASE + "/acars/settings").read().decode()
        check("the SayIntentions key is saved masked, never sent back, and the test telex goes",
              "255" in await page.text_content("#siSaved") and "TESTSIKEY0255" not in raw
              and "test telex" in await page.text_content("#acarsStatus"), await page.text_content("#acarsStatus"))
        await page.locator(".panel:has(#siKey)").screenshot(path=shot("settings_acars_si.png"))
        await page.click("#acarsNetworkRow label:has-text('HOPPIE')"); await page.wait_for_timeout(500)
        check("back on Hoppie, its code is still there", await page.is_visible("#acarsLogon") and "N1" in await page.text_content("#acarsSaved"))

        # Sign in with Navigraph (a fake one): the account shows by name, its tokens stay on the server
        check("the Navigraph panel offers the sign-in",
              await page.is_visible("#navigraphSignIn") and await page.text_content("#navigraphState") == "Not connected")
        check("the sign-in button carries Navigraph's mark",
              await page.evaluate("document.querySelector('#navigraphSignIn img').naturalWidth > 0"))
        await page.locator("#navigraphPanel").screenshot(path=shot("settings_navigraph_off.png"))
        await page.click("#navigraphSignIn")
        await page.wait_for_selector("#navigraphDisconnect:not([hidden])")
        check("back from Navigraph: connected by name, in Account settings",
              page.url.endswith("/app") and "e2e_pilot" in await page.text_content("#navigraphState")
              and "Connected" in await page.text_content("#navigraphStatus"), page.url)
        raw = urllib.request.urlopen(BASE + "/navigraph/status").read().decode() + urllib.request.urlopen(BASE + "/settings").read().decode()
        check("Navigraph tokens never reach the browser", "e2e_pilot" in raw and "e2e-refresh" not in raw and "e2e-access" not in raw)
        await page.locator("#navigraphPanel").screenshot(path=shot("settings_navigraph.png"))
        page.once("dialog", lambda d: asyncio.ensure_future(d.accept()))
        await page.click("#navigraphDisconnect"); await page.wait_for_timeout(600)
        check("disconnecting offers the sign-in again",
              await page.is_visible("#navigraphSignIn") and await page.text_content("#navigraphStatus") == "Disconnected")
        await page.goto(BASE + "/app?navigraph=cancelled"); await page.wait_for_selector("#navigraphStatus:not(:empty)")
        check("a cancelled sign-in is reported", "cancelled" in await page.text_content("#navigraphStatus"))

        # Report a problem, from the header: the message reaches the database with the page and recent errors
        posted = []
        page.on("request", lambda r: posted.append(r.post_data) if r.url.endswith("/client-errors") else None)
        await page.evaluate("""window.dispatchEvent(new ErrorEvent('error', {message: 'TypeError: e2e is broken',
            filename: location.origin + '/static/js/app.js', lineno: 12, colno: 3}))""")
        await page.evaluate("window.dispatchEvent(new ErrorEvent('error', {message: 'extension noise', filename: 'chrome-extension://x/y.js'}))")
        await page.wait_for_timeout(500)
        check("our own script errors go to error monitoring, an extension's don't",
              len(posted) == 1 and json.loads(posted[0])["source"] == "/static/js/app.js:12:3", posted)
        await page.click(".app-report")
        check("the report form opens, without the flight option when there's no flight",
              await page.is_visible("#reportMessage") and not await page.is_visible("#reportFlightRow"))
        await page.select_option("#reportCategory", "bug")
        await page.fill("#reportMessage", "short")
        await page.click("#reportSend")
        check("a too-short report is refused", "at least 10" in await page.text_content("#reportStatus"))
        await page.fill("#reportMessage", "The MEL list in settings doesn't scroll on my laptop")
        await page.locator(".report-modal").screenshot(path=shot("report_form.png"))
        await page.click("#reportSend")
        await page.wait_for_selector("#reportStatus.ok")
        reference = (await page.text_content("#reportStatus")).split()[-1]
        with psycopg2.connect(os.environ["DATABASE_URL"]) as conn, conn.cursor() as cur:
            cur.execute("SELECT category, message, context FROM problem_reports WHERE upper(left(id, 6)) = %s", (reference,))
            row = cur.fetchone()
        check("the report is stored with the page and our own errors only",
              row and row[0] == "bug" and row[2]["view"] == "profile" and row[2]["errors"] and "e2e is broken" in row[2]["errors"][0]
              and len(row[2]["errors"]) == 1 and row[2]["user_agent"], row)
        await page.keyboard.press("Escape")
        check("Escape closes the form", not await page.is_visible("#reportMessage"))

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
