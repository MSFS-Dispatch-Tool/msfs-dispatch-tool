"""
Postgres access for SimDispatch - currently just the PIREP log.

Connects to whatever DATABASE_URL points at (this app expects a Supabase
Postgres project, but any Postgres works). If DATABASE_URL isn't set,
db_available() returns False and callers should degrade explicitly
(see /pireps in app.py) rather than silently falling back to local disk -
Render's free-tier disk is ephemeral, so a silent file fallback would
just recreate the durability problem this module exists to fix.
"""

import os
import uuid
from datetime import datetime, timezone

import psycopg2
import psycopg2.extras

DATABASE_URL = os.environ.get("DATABASE_URL")


def db_available():
    return bool(DATABASE_URL)


def get_connection():
    return psycopg2.connect(DATABASE_URL)


def init_db():
    """Creates the pireps table if it doesn't exist yet. Called once at
    app startup; failures are caught by the caller so a bad/missing
    DATABASE_URL doesn't take down the rest of the app."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS pireps (
                    id TEXT PRIMARY KEY,
                    flight_number TEXT,
                    callsign TEXT,
                    departure_icao TEXT,
                    arrival_icao TEXT,
                    aldt TEXT,
                    abit TEXT,
                    afad NUMERIC,
                    no_new_mel_or_non_normal BOOLEAN,
                    submitted_at TIMESTAMPTZ NOT NULL
                )
            """)
        conn.commit()


def _row_to_record(row):
    record = dict(row)
    submitted_at = record.get("submitted_at")
    if isinstance(submitted_at, datetime):
        record["submitted_at"] = submitted_at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    afad = record.get("afad")
    if afad is not None:
        record["afad"] = float(afad)
    return record


def list_pireps():
    with get_connection() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM pireps ORDER BY submitted_at DESC")
            rows = cur.fetchall()
    return [_row_to_record(r) for r in rows]


def create_pirep(payload):
    submitted_at_raw = payload.get("submitted_at")
    try:
        submitted_at = datetime.fromisoformat(submitted_at_raw.replace("Z", "+00:00")) if submitted_at_raw else None
    except (ValueError, AttributeError):
        submitted_at = None
    if submitted_at is None:
        submitted_at = datetime.now(timezone.utc)

    record = {
        "id": uuid.uuid4().hex,
        "flight_number": payload.get("flight_number", ""),
        "callsign": payload.get("callsign", ""),
        "departure_icao": payload.get("departure_icao", ""),
        "arrival_icao": payload.get("arrival_icao", ""),
        "aldt": payload.get("aldt", ""),
        "abit": payload.get("abit", ""),
        "afad": payload.get("afad"),
        "no_new_mel_or_non_normal": bool(payload.get("no_new_mel_or_non_normal")),
        "submitted_at": submitted_at,
    }
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO pireps (id, flight_number, callsign, departure_icao, arrival_icao,
                                     aldt, abit, afad, no_new_mel_or_non_normal, submitted_at)
                VALUES (%(id)s, %(flight_number)s, %(callsign)s, %(departure_icao)s, %(arrival_icao)s,
                        %(aldt)s, %(abit)s, %(afad)s, %(no_new_mel_or_non_normal)s, %(submitted_at)s)
            """, record)
        conn.commit()
    return _row_to_record(record)


def delete_pirep(pirep_id):
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM pireps WHERE id = %s", (pirep_id,))
            deleted = cur.rowcount
        conn.commit()
    return deleted > 0
