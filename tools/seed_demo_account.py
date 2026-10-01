"""Creates (or resets) a demo pilot account with a realistic logbook, for
testing the app end to end: Profile, Stats, PIREP log.

Run it where the app's environment variables are set, e.g. a Render Shell:

    python tools/seed_demo_account.py --email you+vddemo@gmail.com

It needs SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY and DATABASE_URL. The
account is created already confirmed, so no email is sent. Running it again
with the same email resets that account: new password, profile rewritten,
and its PIREPs and active flight replaced.

Options:
    --password   set the password (default: a random one, printed at the end)
    --flights    how many PIREPs to file (default 170)
    --months     how far back the logbook goes (default 20)
    --base       home base ICAO (default EGKK)
    --seed       random seed, for a repeatable logbook (default 7)

While the access lock is on, add the email to ALLOWED_EMAILS on Render, or
the account will be sent to the access-restricted page after signing in.
"""

import argparse
import os
import random
import secrets
import sys
import uuid
from datetime import date, datetime, timedelta, timezone

import requests
from psycopg2.extras import Json, execute_values

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as A  # noqa: E402  (loads the active carriers' routes and fleets)
import auth  # noqa: E402
import db  # noqa: E402

DELAYS = {
    "87": "Airport facilities: stands, ramp congestion",
    "81": "ATFM: en-route demand/capacity",
    "93": "Aircraft rotation: late arrival of aircraft",
    "16": "Passenger and baggage: missing passenger",
    "63": "Late crew boarding or departure procedures",
    "71": "Weather: departure station",
}
MEL_SYSTEMS = ["APU", "Fuel pump", "Pack 2", "Weather radar", "Cabin PA"]


def hm(minutes):
    minutes %= 1440
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def ensure_user(email, password):
    """Creates the Supabase user (confirmed), or resets the password of an
    existing one. Returns the user id."""
    resp = requests.post(
        f"{auth.SUPABASE_URL}/auth/v1/admin/users",
        headers=auth._service_headers(),
        json={"email": email, "password": password, "email_confirm": True},
        timeout=auth.AUTH_TIMEOUT,
    )
    if resp.status_code < 400:
        return resp.json()["id"], True

    page = 1
    while True:
        users = auth.admin_list_users(page=page)
        match = next((u for u in users if (u.get("email") or "").lower() == email.lower()), None)
        if match:
            break
        if len(users) < 200:
            auth._raise_for_gotrue_error(resp)
        page += 1
    resp = requests.put(
        f"{auth.SUPABASE_URL}/auth/v1/admin/users/{match['id']}",
        headers=auth._service_headers(),
        json={"password": password, "email_confirm": True, "ban_duration": "none"},
        timeout=auth.AUTH_TIMEOUT,
    )
    if resp.status_code >= 400:
        auth._raise_for_gotrue_error(resp)
    return match["id"], False


def build_pireps(user_id, count, months, base, rng):
    routes = [r for code in A.ACTIVE_CARRIER_CODES for r in A.CARRIERS[code]["routes"]
              if r.get("scheduled_departure_local")]
    from_base = [r for r in routes if base in (r["departure_icao"], r["arrival_icao"])]
    today = date.today()
    span = max(30, months * 30)
    rows = []
    for _ in range(count):
        r = rng.choice(from_base if from_base and rng.random() < 0.7 else routes)
        carrier = r["carrier"]
        fleet = A.CARRIERS[carrier]["fleet_by_type"]
        ac_type = r.get("aircraft_type") if r.get("aircraft_type") in fleet else \
            rng.choices(list(fleet), weights=[f.get("weight", 1) for f in fleet.values()])[0]
        day = today - timedelta(days=int(rng.triangular(1, span, span * 0.1)))

        dur = int(r.get("duration_minutes") or 120)
        sobt = rng.randint(5 * 60, 21 * 60)
        sibt = sobt + dur
        dep_delay = max(-5, int(rng.gauss(8, 14))) if rng.random() < 0.85 else rng.randint(30, 95)
        aobt = sobt + dep_delay
        atot = aobt + rng.randint(9, 18)
        aldt = atot + max(30, dur - 22 + rng.randint(-12, 10))
        abit = aldt + rng.randint(4, 11)
        no_actuals = rng.random() < 0.06   # some pilots skip the off-block times

        seats = fleet[ac_type]["seats"]
        lf = round(rng.uniform(0.78, 1.0), 2)
        delay = None
        if dep_delay > 15:
            code = rng.choice(list(DELAYS))
            delay = {"iata_code": code, "description": DELAYS[code], "duration_range_minutes": [10, 30]}
        leg = {
            "flight_number": r["flight_number"], "carrier": carrier, "aircraft_type": ac_type,
            "sobt": hm(sobt) + "Z", "sibt": hm(sibt) + "Z", "eet_minutes": dur - 20,
            "pax_count": int(seats * lf), "seat_capacity": seats, "load_factor": lf,
            "cargo_weight_kg": rng.randint(400, 1800), "cost_index": rng.randint(5, 35),
            "delay": delay,
            "mel": {"id": "mel-03", "system": rng.choice(MEL_SYSTEMS)} if rng.random() < 0.12 else None,
            "departure_info": {"icao": r["departure_icao"]}, "arrival_info": {"icao": r["arrival_icao"]},
        }
        trip = int(dur * rng.uniform(38, 46))
        landing = rng.randint(2000, 3000)
        ofp = {"weight_unit": "kgs", "plan_trip_fuel": str(trip), "plan_landing_fuel": str(landing),
               "block_fuel": str(trip + landing + rng.randint(1200, 2500))}
        submitted = datetime(day.year, day.month, day.day, tzinfo=timezone.utc) + timedelta(minutes=abit + 25)
        pirep = {"aobt": "N/A" if no_actuals else hm(aobt), "atot": "N/A" if no_actuals else hm(atot),
                 "aldt": hm(aldt), "abit": hm(abit), "afad": landing + rng.randint(-400, 500),
                 "submitted_at": submitted.isoformat().replace("+00:00", "Z")}
        prefix = r.get("callsign_prefix") or A.CARRIERS[carrier]["callsign_prefix"]
        callsign = f"{prefix}{rng.randint(10, 99)}{rng.choice('ABCDEFGH')}"
        rows.append((uuid.uuid4().hex, r["flight_number"], callsign, r["departure_icao"], r["arrival_icao"],
                     day, submitted, user_id, Json({"leg": leg, "ofp": ofp, "loadsheet": None, "pirep": pirep})))
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--email", required=True)
    parser.add_argument("--password")
    parser.add_argument("--flights", type=int, default=170)
    parser.add_argument("--months", type=int, default=20)
    parser.add_argument("--base", default="EGKK")
    parser.add_argument("--seed", type=int, default=7)
    args = parser.parse_args()

    if not auth.admin_available():
        sys.exit("SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY must be set")
    if not db.db_available():
        sys.exit("DATABASE_URL must be set")
    db.init_db()

    password = args.password or secrets.token_urlsafe(12)
    user_id, created = ensure_user(args.email.strip(), password)
    base = args.base.strip().upper()

    settings = db.get_settings(user_id)
    settings["profile"].update({
        "username": "demo_pilot", "first_name": "Alex", "last_name": "Morgan",
        "birth_date": "1990-04-12", "nationality": "United Kingdom", "preferred_base": base,
        "simulator": "MSFS2024", "aircraft_owned": [], "onboarding_complete": True,
    })
    db.save_settings(settings, user_id)

    rows = build_pireps(user_id, args.flights, args.months, base, random.Random(args.seed))
    with db.get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM pireps WHERE user_id = %s", (user_id,))
            cur.execute("DELETE FROM active_flights WHERE user_id = %s", (user_id,))
            execute_values(cur, """
                INSERT INTO pireps (id, flight_number, callsign, departure_icao, arrival_icao,
                                    flight_date, submitted_at, user_id, detail) VALUES %s
            """, rows)
        conn.commit()

    print(f"{'Created' if created else 'Reset'} demo account with {len(rows)} PIREPs")
    print(f"  email:    {args.email.strip()}")
    print(f"  password: {password}")
    print(f"  user id:  {user_id}")
    allowed = auth.ADMIN_EMAILS | auth.ALLOWED_EMAILS
    if not auth.OPEN_ACCESS and allowed and args.email.strip().lower() not in allowed:
        print("  NOTE: add this email to ALLOWED_EMAILS, or the access lock will block it")


if __name__ == "__main__":
    main()
