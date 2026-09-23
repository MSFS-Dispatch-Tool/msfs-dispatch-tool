"""
Postgres access for VirtualDispatch - currently just the PIREP log.

Connects to whatever DATABASE_URL points at (this app expects a Supabase
Postgres project, but any Postgres works). If DATABASE_URL isn't set,
db_available() returns False and callers should degrade explicitly
(see /pireps in app.py) rather than silently falling back to local disk -
Render's free-tier disk is ephemeral, so a silent file fallback would
just recreate the durability problem this module exists to fix.

A filed PIREP stores the ENTIRE leg record (schedule, weather, OFP,
loadsheet, delay, etc.) as it stood at PIREP time, not just the PIREP
fields themselves - the PIREP log is meant to reopen exactly what the
active-flight page showed for that leg, not just ALDT/ABIT/AFAD.
"""

import os
import uuid
from datetime import date, datetime, timezone

import psycopg2
import psycopg2.extras
from psycopg2.extras import Json

DATABASE_URL = os.environ.get("DATABASE_URL")

# Used whenever settings can't be read (DATABASE_URL unset, no row saved
# yet, or a query error) - "everything on" matches the app's behavior
# before these toggles existed, so a missing/broken settings store never
# silently changes what gets generated.
DEFAULT_SETTINGS = {
    "profile": {
        "username": "", "photo_url": "",
        "first_name": "", "last_name": "", "birth_date": "", "nationality": "",
        "preferred_base": "", "onboarding_complete": False,
    },
    "generation": {
        "delay": {"enabled": True, "disabled_codes": []},
        "lmc": {"enabled": True, "disabled_ids": []},
        "mel": {"enabled": True, "disabled_ids": []},
    },
}


def db_available():
    return bool(DATABASE_URL)


def get_connection():
    return psycopg2.connect(DATABASE_URL)


def init_db():
    """Creates the pireps table if it doesn't exist yet, and adds the
    flight_date/detail columns if this is an older table from before
    they existed. Called once at app startup; failures are caught by
    the caller so a bad/missing DATABASE_URL doesn't take down the rest
    of the app."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS pireps (
                    id TEXT PRIMARY KEY,
                    flight_number TEXT,
                    callsign TEXT,
                    departure_icao TEXT,
                    arrival_icao TEXT,
                    submitted_at TIMESTAMPTZ NOT NULL
                )
            """)
            cur.execute("ALTER TABLE pireps ADD COLUMN IF NOT EXISTS flight_date DATE")
            cur.execute("ALTER TABLE pireps ADD COLUMN IF NOT EXISTS detail JSONB")
            # user_id is the Supabase Auth user's UUID (text, not a FK -
            # the auth users table lives in Supabase's own "auth" schema,
            # not one this app manages). Nullable so PIREPs filed before
            # accounts existed don't become orphaned/unreadable.
            cur.execute("ALTER TABLE pireps ADD COLUMN IF NOT EXISTS user_id TEXT")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_pireps_user_id ON pireps (user_id)")
            cur.execute("""
                CREATE TABLE IF NOT EXISTS app_settings (
                    id TEXT PRIMARY KEY,
                    data JSONB NOT NULL
                )
            """)
        conn.commit()


def _row_to_record(row):
    """Flattens a DB row (flat columns + a detail JSONB blob) back into
    the shape the frontend expects: leg/ofp/loadsheet/pirep alongside
    the flat summary fields."""
    record = {
        "id": row["id"],
        "flight_number": row["flight_number"],
        "callsign": row["callsign"],
        "departure_icao": row["departure_icao"],
        "arrival_icao": row["arrival_icao"],
        "flight_date": row["flight_date"].isoformat() if row["flight_date"] else None,
    }
    submitted_at = row["submitted_at"]
    record["submitted_at"] = submitted_at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z") \
        if isinstance(submitted_at, datetime) else submitted_at
    detail = row.get("detail") or {}
    record["leg"] = detail.get("leg")
    record["ofp"] = detail.get("ofp")
    record["loadsheet"] = detail.get("loadsheet")
    record["pirep"] = detail.get("pirep")
    return record


def list_pireps(user_id):
    with get_connection() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM pireps WHERE user_id = %s ORDER BY submitted_at DESC", (user_id,))
            rows = cur.fetchall()
    return [_row_to_record(r) for r in rows]


def create_pirep(payload, user_id):
    """payload is the raw JSON body from POST /pireps - flight_number/
    callsign/departure_icao/arrival_icao plus leg/ofp/loadsheet/pirep
    (the full leg record as held client-side at PIREP time)."""
    pirep = payload.get("pirep") or {}
    submitted_at_raw = pirep.get("submitted_at")
    try:
        submitted_at = datetime.fromisoformat(submitted_at_raw.replace("Z", "+00:00")) if submitted_at_raw else None
    except (ValueError, AttributeError):
        submitted_at = None
    if submitted_at is None:
        submitted_at = datetime.now(timezone.utc)

    row = {
        "id": uuid.uuid4().hex,
        "flight_number": payload.get("flight_number", ""),
        "callsign": payload.get("callsign", ""),
        "departure_icao": payload.get("departure_icao", ""),
        "arrival_icao": payload.get("arrival_icao", ""),
        "flight_date": date.today(),
        "submitted_at": submitted_at,
        "user_id": user_id,
        "detail": Json({
            "leg": payload.get("leg"),
            "ofp": payload.get("ofp"),
            "loadsheet": payload.get("loadsheet"),
            "pirep": pirep,
        }),
    }
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO pireps (id, flight_number, callsign, departure_icao, arrival_icao,
                                     flight_date, submitted_at, user_id, detail)
                VALUES (%(id)s, %(flight_number)s, %(callsign)s, %(departure_icao)s, %(arrival_icao)s,
                        %(flight_date)s, %(submitted_at)s, %(user_id)s, %(detail)s)
            """, row)
        conn.commit()

    with get_connection() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM pireps WHERE id = %s", (row["id"],))
            saved = cur.fetchone()
    return _row_to_record(saved)


def delete_pirep(pirep_id, user_id):
    """Scoped to user_id so one account can never delete another's PIREP
    even if it guesses/enumerates an id."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM pireps WHERE id = %s AND user_id = %s", (pirep_id, user_id))
            deleted = cur.rowcount
        conn.commit()
    return deleted > 0


def get_settings(user_id):
    """Merged with DEFAULT_SETTINGS so an older/partial saved row (e.g.
    missing a category added later) never crashes a caller that expects
    every key to be present. Settings are keyed by the account's own
    user_id - each pilot gets their own profile/generation toggles."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT data FROM app_settings WHERE id = %s", (user_id,))
            row = cur.fetchone()
    saved = row[0] if row else {}

    merged = {
        "profile": {**DEFAULT_SETTINGS["profile"], **(saved.get("profile") or {})},
        "generation": {},
    }
    for category, defaults in DEFAULT_SETTINGS["generation"].items():
        merged["generation"][category] = {**defaults, **((saved.get("generation") or {}).get(category) or {})}
    return merged


def save_settings(data, user_id):
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO app_settings (id, data) VALUES (%s, %s)
                ON CONFLICT (id) DO UPDATE SET data = EXCLUDED.data
            """, (user_id, Json(data)))
        conn.commit()
    return get_settings(user_id)


def get_stats(user_id):
    """Total filed PIREPs and total flight minutes for one account, the
    latter summed from each leg's own eet_minutes (the scheduled/
    expected airborne time) - the app never captures an actual takeoff
    time, only ALDT/ABIT, so this is the honest figure to total rather
    than something presented as a measured actual.

    Also returns per-airport visit counts (departure_icao/arrival_icao
    are flat columns, not in the detail JSONB, since every PIREP has
    them) and a flights-per-month series - both feed the user page's
    destinations map and flights chart. Airport metadata (name/lat/lon)
    isn't looked up here - app.py owns that reference data - so this
    just returns ICAO codes and counts."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT detail, departure_icao, arrival_icao, flight_date FROM pireps WHERE user_id = %s",
                (user_id,),
            )
            rows = cur.fetchall()
    total_flights = len(rows)
    total_minutes = 0
    airport_counts = {}
    month_counts = {}
    for detail, departure_icao, arrival_icao, flight_date in rows:
        leg = (detail or {}).get("leg") or {}
        eet = leg.get("eet_minutes")
        if isinstance(eet, (int, float)):
            total_minutes += eet
        for icao in (departure_icao, arrival_icao):
            if icao:
                airport_counts[icao] = airport_counts.get(icao, 0) + 1
        if flight_date:
            month_key = flight_date.strftime("%Y-%m")
            month_counts[month_key] = month_counts.get(month_key, 0) + 1

    airports = [{"icao": icao, "count": count} for icao, count in sorted(airport_counts.items())]
    monthly = [{"month": month, "flights": count} for month, count in sorted(month_counts.items())]

    return {
        "total_flights": total_flights,
        "total_flight_minutes": total_minutes,
        "airports": airports,
        "monthly": monthly,
    }
