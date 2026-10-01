"""Demo pilot accounts: a confirmed Supabase user with a filled-in profile
and a realistic logbook, for testing Profile, Stats and the PIREP log.

Created from the admin page (/admin/users) or tools/seed_demo_account.py.
Demo accounts are recorded in the demo_accounts table, which does two
things: they get past the access lock without being added to
ALLOWED_EMAILS, and only they can be reset - an existing real account
with flights of its own is never overwritten.
"""

import random
import uuid
from datetime import date, datetime, timedelta, timezone

import requests
from psycopg2.extras import Json, execute_values

import auth
import db

DELAYS = {
    "87": "Airport facilities: stands, ramp congestion",
    "81": "ATFM: en-route demand/capacity",
    "93": "Aircraft rotation: late arrival of aircraft",
    "16": "Passenger and baggage: missing passenger",
    "63": "Late crew boarding or departure procedures",
    "71": "Weather: departure station",
}
MEL_SYSTEMS = ["APU", "Fuel pump", "Pack 2", "Weather radar", "Cabin PA"]

PROFILE = {
    "username": "demo_pilot", "first_name": "Alex", "last_name": "Morgan",
    "birth_date": "1990-04-12", "nationality": "United Kingdom",
    "simulator": "MSFS2024", "aircraft_owned": [], "onboarding_complete": True,
}


class DemoError(Exception):
    pass


def _hm(minutes):
    minutes %= 1440
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def _find_user(email):
    page = 1
    while True:
        users = auth.admin_list_users(page=page)
        match = next((u for u in users if (u.get("email") or "").lower() == email.lower()), None)
        if match or len(users) < 200:
            return match
        page += 1


def _ensure_user(email, password):
    """Creates the Supabase user (already confirmed, so no email is sent),
    or resets an existing demo account's password. Returns (id, created)."""
    resp = requests.post(
        f"{auth.SUPABASE_URL}/auth/v1/admin/users",
        headers=auth._service_headers(),
        json={"email": email, "password": password, "email_confirm": True},
        timeout=auth.AUTH_TIMEOUT,
    )
    if resp.status_code < 400:
        return resp.json()["id"], True

    user = _find_user(email)
    if not user:
        auth._raise_for_gotrue_error(resp)
    if not is_demo_account(user["id"]) and _pirep_count(user["id"]):
        raise DemoError(f"{email} is an existing account with its own flights, so it can't become a demo account")
    resp = requests.put(
        f"{auth.SUPABASE_URL}/auth/v1/admin/users/{user['id']}",
        headers=auth._service_headers(),
        json={"password": password, "email_confirm": True, "ban_duration": "none"},
        timeout=auth.AUTH_TIMEOUT,
    )
    if resp.status_code >= 400:
        auth._raise_for_gotrue_error(resp)
    return user["id"], False


def _pirep_count(user_id):
    with db.get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM pireps WHERE user_id = %s", (user_id,))
            return cur.fetchone()[0]


def is_demo_account(user_id):
    try:
        with db.get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1 FROM demo_accounts WHERE user_id = %s", (user_id,))
                return cur.fetchone() is not None
    except Exception:
        return False


def build_pireps(user_id, carriers, count, months, base, rng):
    """`carriers` is app.CARRIERS (code -> {routes, fleet_by_type, ...})."""
    routes = [r for c in carriers.values() for r in c["routes"] if r.get("scheduled_departure_local")]
    from_base = [r for r in routes if base in (r["departure_icao"], r["arrival_icao"])]
    today = date.today()
    span = max(30, months * 30)
    rows = []
    for _ in range(count):
        r = rng.choice(from_base if from_base and rng.random() < 0.7 else routes)
        carrier = carriers[r["carrier"]]
        fleet = carrier["fleet_by_type"]
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
            "flight_number": r["flight_number"], "carrier": r["carrier"], "aircraft_type": ac_type,
            "sobt": _hm(sobt) + "Z", "sibt": _hm(sibt) + "Z", "eet_minutes": dur - 20,
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
        pirep = {"aobt": "N/A" if no_actuals else _hm(aobt), "atot": "N/A" if no_actuals else _hm(atot),
                 "aldt": _hm(aldt), "abit": _hm(abit), "afad": landing + rng.randint(-400, 500),
                 "submitted_at": submitted.isoformat().replace("+00:00", "Z")}
        prefix = r.get("callsign_prefix") or carrier["callsign_prefix"]
        callsign = f"{prefix}{rng.randint(10, 99)}{rng.choice('ABCDEFGH')}"
        rows.append((uuid.uuid4().hex, r["flight_number"], callsign, r["departure_icao"], r["arrival_icao"],
                     day, submitted, user_id, Json({"leg": leg, "ofp": ofp, "loadsheet": None, "pirep": pirep})))
    return rows


def seed_account(email, password, carriers, flights=170, months=20, base="EGKK", seed=7):
    """Creates or resets the demo account. Returns {"user_id", "created", "flights"}."""
    if not auth.admin_available():
        raise DemoError("SUPABASE_SERVICE_ROLE_KEY is not configured")
    if not db.db_available():
        raise DemoError("DATABASE_URL is not configured")
    email = email.strip()
    base = (base or "EGKK").strip().upper()
    user_id, created = _ensure_user(email, password)

    settings = db.get_settings(user_id)
    settings["profile"].update({**PROFILE, "preferred_base": base})
    db.save_settings(settings, user_id)

    rows = build_pireps(user_id, carriers, flights, months, base, random.Random(seed))
    with db.get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO demo_accounts (user_id, email) VALUES (%s, %s)
                ON CONFLICT (user_id) DO UPDATE SET email = EXCLUDED.email
            """, (user_id, email))
            cur.execute("DELETE FROM pireps WHERE user_id = %s", (user_id,))
            cur.execute("DELETE FROM active_flights WHERE user_id = %s", (user_id,))
            execute_values(cur, """
                INSERT INTO pireps (id, flight_number, callsign, departure_icao, arrival_icao,
                                    flight_date, submitted_at, user_id, detail) VALUES %s
            """, rows)
        conn.commit()
    return {"user_id": user_id, "created": created, "flights": len(rows)}
