# Launch checklist

Points to settle before VirtualDispatch opens beyond the closed beta. Tick
them off here as they're done.

## Legal and contact

- [x] Make sure privacy@virtualdispatch.app and legal@virtualdispatch.app
      receive mail: the Privacy Policy, Cookie Policy and Terms of Service
      give them as the contact addresses.
      Done with Cloudflare Email Routing: both forward to the owner's inbox.
- [ ] Have the Privacy Policy, Cookie Policy and Terms of Service reviewed
      once by someone qualified in GDPR (the operator is in Italy).

## Navigraph / SimBrief (Developer Terms v1.0, 16 Dec 2021)

- [ ] Before any material change to how the app uses Navigraph (new
      endpoints, callback_url, performance data...), tell Navigraph at least
      15 business days ahead (Developer Terms 8.2).
- [ ] Keep the integration in use: Navigraph may end it after 90 days with
      no API use (13.2b).
- [ ] Never charge for SimBrief features on their own (4.4b), never use what
      Navigraph provides to build a user or marketing database (4.4c) or to
      study pilots' usage (5.3).
- [ ] Read Navigraph's Brand Guidelines (navigraph.com/legal/brand-guidelines)
      and check the sign-in button and any mention of Navigraph or SimBrief
      on the website against them (6.3).
- [x] Ask Navigraph to confirm that keeping OFP figures (fuel, route) in the
      pilot's own logbook is fine (3.1b allows caching copies only).
      Confirmed by Navigraph (Markus), Nov 2026.

## Services

- [x] SENTRY_DSN set in Render; error retention at most 90 days (the
      Privacy Policy says so).
- [x] NAVIGRAPH_CLIENT_ID and NAVIGRAPH_CLIENT_SECRET set in Render.
- [x] SEND TO SIMBRIEF through SimBrief's API (plan generated with the
      pilot's saved airframe, OFP straight back) is on for every pilot signed
      in with Navigraph, Nov 2026. SIMBRIEF_API_GENERATE=off in Render turns
      it off for everyone; pilots then go through simbrief.com.
- [ ] SAYINTENTIONS_DEVELOPER_KEY set in Render before other pilots join
      (SEND TO AIRCRAFT through SayIntentions stays hidden without it; the
      SAYINTENTIONS ATIS runways send it once set).
- [x] Runways from ATIS (atis.py): terms checked, 3 Nov 2026.
      VATSIM: the data feed is public, needs no key and refreshes every 15
      seconds; its use falls under the VATSIM User Agreement. Read at most
      once a minute; no member data is stored.
      IVAO: the Whazzup feed is free to use without approval, at most once
      every 3 minutes, or IVAO may block the server's address (rule D.4, as
      quoted by client libraries; IVAO's wiki was not reachable to confirm).
      Read at most once every 3 minutes.
      SayIntentions: SAPI's documentation asks for the app's developer key
      on every request. getWX answers without it, and the SAYINTENTIONS ATIS
      source is offered meanwhile (one pilot, Nov 2026); the key goes into
      SAYINTENTIONS_DEVELOPER_KEY above before VirtualDispatch opens to
      other pilots.
- [x] Commercial use of the networks' data: not applicable. VirtualDispatch
      is free, with no paid plan (decided 4 Nov 2026). If that ever changes,
      ask VATSIM (tech@vatsim.net), IVAO (DevOps) and SayIntentions first:
      none of the terms found covers commercial use.
- [ ] FLASK_SECRET_KEY set and kept stable: Navigraph tokens, Hoppie codes
      and SayIntentions keys are encrypted with keys derived from it.

## Hosting

- [x] Render: Settings > Health Check Path set to `/healthz`, so a deploy
      that doesn't start never replaces the running one.
- [x] Render: a start command sized for the plan, e.g.
      `gunicorn app:app --workers 2 --threads 4 --timeout 60` (each worker
      takes about 90 MB).
- [ ] Supabase: decide whether to move to Pro before other pilots join.
      VirtualDispatch is free (decided 4 Nov 2026), so Pro would be a running
      cost with no revenue. Pro keeps daily backups for 7 days and never
      pauses the project. On the free plan, accepted risks: no backups to
      restore from, and the project pauses after about a week without use
      (restore it from the Supabase dashboard; the data is kept).
- [x] Supabase: DATABASE_URL points at the connection pooler, not a direct
      connection, so the workers don't run out of database connections
      (transaction mode, port 6543: the app uses no prepared statements,
      session settings or advisory locks, which that mode doesn't support).
