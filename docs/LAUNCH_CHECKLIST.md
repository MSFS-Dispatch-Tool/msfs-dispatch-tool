# Launch checklist

Points to settle before VirtualDispatch opens beyond the closed beta. Tick
them off here as they're done.

## Legal and contact

- [ ] Make sure privacy@virtualdispatch.app and legal@virtualdispatch.app
      receive mail: the Privacy Policy, Cookie Policy and Terms of Service
      give them as the contact addresses.
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
- [ ] FLASK_SECRET_KEY set and kept stable: Navigraph tokens and Hoppie
      codes are encrypted with keys derived from it.

## Hosting

- [x] Render: Settings > Health Check Path set to `/healthz`, so a deploy
      that doesn't start never replaces the running one.
- [x] Render: a start command sized for the plan, e.g.
      `gunicorn app:app --workers 2 --threads 4 --timeout 60` (each worker
      takes about 90 MB).
- [ ] Supabase: backups that can actually be restored (the free plan has
      none to rely on; Pro has daily backups), and a plan that doesn't pause
      the project after a week without use.
- [ ] Supabase: DATABASE_URL points at the connection pooler, not a direct
      connection, so the workers don't run out of database connections.
