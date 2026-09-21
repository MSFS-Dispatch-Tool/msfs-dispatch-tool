"""
Postgres access for SimDispatch - currently just the PIREP log.

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


def list_pireps():
    with get_connection() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM pireps ORDER BY submitted_at DESC")
            rows = cur.fetchall()
    return [_row_to_record(r) for r in rows]


def create_pirep(payload):
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
                                     flight_date, submitted_at, detail)
                VALUES (%(id)s, %(flight_number)s, %(callsign)s, %(departure_icao)s, %(arrival_icao)s,
                        %(flight_date)s, %(submitted_at)s, %(detail)s)
            """, row)
        conn.commit()

    with get_connection() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM pireps WHERE id = %s", (row["id"],))
            saved = cur.fetchone()
    return _row_to_record(saved)


def delete_pirep(pirep_id):
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM pireps WHERE id = %s", (pirep_id,))
            deleted = cur.rowcount
        conn.commit()
    return deleted > 0
