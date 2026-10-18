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
- [ ] Ask Navigraph to confirm that keeping OFP figures (fuel, route) in the
      pilot's own logbook is fine (3.1b allows caching copies only).

## Services

- [x] SENTRY_DSN set in Render; error retention at most 90 days (the
      Privacy Policy says so).
- [ ] NAVIGRAPH_CLIENT_ID and NAVIGRAPH_CLIENT_SECRET set in Render.
- [ ] SAYINTENTIONS_DEVELOPER_KEY set in Render (SEND TO AIRCRAFT through
      SayIntentions; the option stays hidden without it).
- [ ] FLASK_SECRET_KEY set and kept stable: Navigraph tokens, Hoppie codes
      and SayIntentions keys are encrypted with keys derived from it.

## Hosting

- [x] Render: Settings > Health Check Path set to `/healthz`, so a deploy
      that doesn't start never replaces the running one.
- [x] Render: a start command sized for the plan, e.g.
      `gunicorn app:app --workers 2 --threads 4 --timeout 60` (each worker
      takes about 90 MB).
- [ ] Supabase: move to Pro when VirtualDispatch's paid plan goes live
      (decided 15 Oct 2026). Pro keeps daily backups for 7 days and never
      pauses the project. Until then, accepted risks: no backups to restore
      from, and the project pauses after about a week without use (restore it
      from the Supabase dashboard; the data is kept).
- [x] Supabase: DATABASE_URL points at the connection pooler, not a direct
      connection, so the workers don't run out of database connections
      (transaction mode, port 6543: the app uses no prepared statements,
      session settings or advisory locks, which that mode doesn't support).
