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

import contextlib
import os
import threading
import time
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
        "preferred_base": "", "simulator": "", "onboarding_complete": False,
        # Aircraft type codes (carrier fleet.type values, e.g. "738",
        # "320") the pilot has told the app they actually fly/own in the
        # simulator - drives which aircraft a flight can be assigned
        # (see generator.assign_aircraft_type). Empty means "no
        # preference stated", not "flies nothing" - assignment falls
        # back to the operating carrier's whole fleet, weighted by
        # real-world prevalence, rather than refusing to assign anything.
        "aircraft_owned": [],
    },
    # probability: the pilot's own chance per trip, 0-100 (%), or None for
    # the realistic default (see generator.py and techlog.py).
    "generation": {
        "delay": {"enabled": True, "disabled_codes": [], "probability": None},
        "lmc": {"enabled": True, "disabled_ids": [], "probability": None},
        "mel": {"enabled": True, "disabled_ids": [], "probability": None},
        "cdl": {"enabled": True, "disabled_ids": [], "probability": None},
    },
}


def db_available():
    return bool(DATABASE_URL)


# Connections are reused instead of opened per query: against a hosted
# Postgres every new connection costs a TLS and auth handshake. Each worker
# process keeps a few; one idle longer than POOL_IDLE_SECONDS is replaced
# (the server or its pooler may have dropped it), and one that errored is
# thrown away rather than handed out again.
POOL_SIZE = 4
POOL_IDLE_SECONDS = 60
_pool = []
_pool_pid = None
_pool_lock = threading.Lock()


def _take_connection():
    global _pool, _pool_pid
    now = time.monotonic()
    with _pool_lock:
        if _pool_pid != os.getpid():   # a forked worker never reuses its parent's sockets
            _pool, _pool_pid = [], os.getpid()
        while _pool:
            conn, last_used = _pool.pop()
            if not conn.closed and now - last_used < POOL_IDLE_SECONDS:
                return conn
            _close_quietly(conn)
    return psycopg2.connect(DATABASE_URL)


def _return_connection(conn):
    with _pool_lock:
        if _pool_pid == os.getpid() and len(_pool) < POOL_SIZE and not conn.closed:
            _pool.append((conn, time.monotonic()))
            return
    _close_quietly(conn)


def _close_quietly(conn):
    try:
        conn.close()
    except Exception:
        pass


@contextlib.contextmanager
def get_connection():
    """A connection for one unit of work: committed when the block ends,
    rolled back if it raises, then returned to the pool."""
    conn = _take_connection()
    try:
        yield conn
        conn.commit()
    except Exception:
        try:
            conn.rollback()
            healthy = True
        except Exception:
            healthy = False
        if healthy and conn.closed == 0:
            _return_connection(conn)
        else:
            _close_quietly(conn)
        raise
    _return_connection(conn)


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
            # One row per account: the flight currently being flown (NULL
            # once finished/discarded). rev increments on every write so a
            # device can only overwrite the version it last saw.
            cur.execute("""
                CREATE TABLE IF NOT EXISTS active_flights (
                    user_id TEXT PRIMARY KEY,
                    data JSONB,
                    rev INTEGER NOT NULL DEFAULT 0,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )
            """)
            # Demo pilot accounts created from the admin page (see demo.py).
            cur.execute("""
                CREATE TABLE IF NOT EXISTS demo_accounts (
                    user_id TEXT PRIMARY KEY,
                    email TEXT,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )
            """)
            # Problems pilots report from the app (Report a problem), read
            # on the admin page. context: the page, browser and recent
            # errors; flight: the active flight, if the pilot attached it.
            cur.execute("""
                CREATE TABLE IF NOT EXISTS problem_reports (
                    id TEXT PRIMARY KEY,
                    user_id TEXT,
                    email TEXT,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    category TEXT NOT NULL,
                    message TEXT NOT NULL,
                    context JSONB,
                    flight JSONB,
                    status TEXT NOT NULL DEFAULT 'new'
                )
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_problem_reports_created ON problem_reports (created_at DESC)")
            # Pilots allowed into the app while access is locked, managed on
            # the admin page (on top of ADMIN_EMAILS / ALLOWED_EMAILS).
            cur.execute("""
                CREATE TABLE IF NOT EXISTS allowed_pilots (
                    email TEXT PRIMARY KEY,
                    note TEXT NOT NULL DEFAULT '',
                    added_by TEXT NOT NULL DEFAULT '',
                    added_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )
            """)
            # Visitors waiting for an invitation, from the website's form,
            # with the consent text they ticked and when.
            cur.execute("""
                CREATE TABLE IF NOT EXISTS waitlist (
                    email TEXT PRIMARY KEY,
                    consent TEXT NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )
            """)
        conn.commit()


APP_TABLES = ("pireps", "app_settings", "active_flights", "demo_accounts", "problem_reports", "allowed_pilots", "waitlist")


def enable_row_security():
    """Row-level security on, with no policies, on every table of the app.
    Supabase also serves the public schema through its automatic data API
    to anyone holding the project's public key; with row security on and no
    policy allowing anything, that API returns nothing from these tables.
    This server is unaffected: it connects as the tables' owner, which row
    security doesn't apply to."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            for table in APP_TABLES:
                cur.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")


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


def delete_user_data(user_id):
    """Removes every row this app itself stores for an account (PIREPs,
    settings, problem reports) - called when an account is deleted, so closing the
    Supabase Auth user doesn't leave orphaned rows behind here."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM pireps WHERE user_id = %s", (user_id,))
            cur.execute("DELETE FROM app_settings WHERE id = %s", (user_id,))
            cur.execute("DELETE FROM active_flights WHERE user_id = %s", (user_id,))
            cur.execute("DELETE FROM demo_accounts WHERE user_id = %s", (user_id,))
            cur.execute("DELETE FROM problem_reports WHERE user_id = %s", (user_id,))
        conn.commit()


def get_active_flight(user_id):
    """{"flight": <dict or None>, "rev": int} - rev 0 means never saved."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT data, rev FROM active_flights WHERE user_id = %s", (user_id,))
            row = cur.fetchone()
    return {"flight": row[0], "rev": row[1]} if row else {"flight": None, "rev": 0}


def save_active_flight(user_id, flight, base_rev):
    """Stores `flight` (None clears it) only if the caller's copy was based
    on the latest revision. Returns (saved, current) - on a conflict nothing
    is written and `current` is the newer copy another device saved."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO active_flights (user_id, data, rev, updated_at)
                VALUES (%s, %s, 1, now())
                ON CONFLICT (user_id) DO UPDATE
                    SET data = EXCLUDED.data, rev = active_flights.rev + 1, updated_at = now()
                    WHERE active_flights.rev = %s
                RETURNING rev
            """, (user_id, Json(flight) if flight is not None else None, base_rev))
            row = cur.fetchone()
        conn.commit()
    if row:
        return True, {"flight": flight, "rev": row[0]}
    return False, get_active_flight(user_id)


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
    """Replaces the profile and generation settings; anything else stored
    alongside them (the ACARS logon code, the Navigraph link) is kept."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO app_settings (id, data) VALUES (%s, %s)
                ON CONFLICT (id) DO UPDATE
                    SET data = (app_settings.data - 'profile' - 'generation') || EXCLUDED.data
            """, (user_id, Json(data)))
        conn.commit()
    return get_settings(user_id)


def get_acars(user_id):
    """The pilot's ACARS settings ({"logon": ...}), stored with their other
    settings but never part of what /settings sends to the browser."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT data -> 'acars' FROM app_settings WHERE id = %s", (user_id,))
            row = cur.fetchone()
    return (row[0] if row and isinstance(row[0], dict) else None) or {}


def get_navigraph(user_id):
    """The pilot's Navigraph link ({"name", "access", "refresh",
    "expires_at", "connected_at"}, tokens encrypted), stored with their
    other settings but never part of what /settings sends to the browser."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT data -> 'navigraph' FROM app_settings WHERE id = %s", (user_id,))
            row = cur.fetchone()
    return (row[0] if row and isinstance(row[0], dict) else None) or {}


def save_navigraph(link, user_id):
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO app_settings (id, data) VALUES (%s, jsonb_build_object('navigraph', %s::jsonb))
                ON CONFLICT (id) DO UPDATE SET data = app_settings.data || jsonb_build_object('navigraph', %s::jsonb)
            """, (user_id, Json(link), Json(link)))


@contextlib.contextmanager
def locked_navigraph(user_id):
    """The pilot's Navigraph link, row-locked until the block ends, so two
    requests (on any worker) never refresh the same tokens at once: a
    refresh token may only be good for one use. Yields (link, save)."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT data -> 'navigraph' FROM app_settings WHERE id = %s FOR UPDATE", (user_id,))
            row = cur.fetchone()

            def save(link):
                cur.execute("UPDATE app_settings SET data = data || jsonb_build_object('navigraph', %s::jsonb) WHERE id = %s",
                            (Json(link), user_id))
            yield (row[0] if row and isinstance(row[0], dict) else None) or {}, save


def seal_plain_acars_logons(seal, logon_pattern):
    """Encrypts the Hoppie logon codes saved before codes were encrypted:
    a stored value that is still a plain code (logon_pattern) is replaced by
    seal(code). Returns how many were encrypted."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id, data -> 'acars' ->> 'logon' FROM app_settings "
                        "WHERE data -> 'acars' ->> 'logon' ~ %s FOR UPDATE", (logon_pattern,))
            rows = cur.fetchall()
            for user_id, code in rows:
                cur.execute("UPDATE app_settings SET data = jsonb_set(data, '{acars,logon}', to_jsonb(%s::text)) WHERE id = %s",
                            (seal(code), user_id))
    return len(rows)


def save_acars(acars, user_id):
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO app_settings (id, data) VALUES (%s, jsonb_build_object('acars', %s::jsonb))
                ON CONFLICT (id) DO UPDATE SET data = app_settings.data || jsonb_build_object('acars', %s::jsonb)
            """, (user_id, Json(acars), Json(acars)))
        conn.commit()


REPORT_STATUSES = ("new", "seen", "fixed")


def create_report(user_id, email, category, message, context, flight):
    report_id = uuid.uuid4().hex
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO problem_reports (id, user_id, email, category, message, context, flight)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
            """, (report_id, user_id, email, category, message, Json(context),
                  Json(flight) if flight is not None else None))
    return report_id


def _report_row(row, with_flight=False):
    report = {"id": row[0], "user_id": row[1], "email": row[2],
              "created_at": row[3].astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
              "category": row[4], "message": row[5], "context": row[6] or {}, "status": row[7], "has_flight": row[8]}
    if with_flight:
        report["flight"] = row[9]
    return report


_REPORT_COLUMNS = "id, user_id, email, created_at, category, message, context, status, flight IS NOT NULL"


def list_reports(limit=200):
    """Newest first, without the attached flights (fetch one with get_report)."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(f"SELECT {_REPORT_COLUMNS} FROM problem_reports ORDER BY created_at DESC LIMIT %s", (limit,))
            rows = cur.fetchall()
    return [_report_row(r) for r in rows]


def get_report(report_id):
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(f"SELECT {_REPORT_COLUMNS}, flight FROM problem_reports WHERE id = %s", (report_id,))
            row = cur.fetchone()
    return _report_row(row, with_flight=True) if row else None


def set_report_status(report_id, status):
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE problem_reports SET status = %s WHERE id = %s", (status, report_id))
            return cur.rowcount > 0


def count_new_reports():
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM problem_reports WHERE status = 'new'")
            return cur.fetchone()[0]


def _iso(ts):
    return ts.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def list_allowed_pilots():
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT email, note, added_by, added_at FROM allowed_pilots ORDER BY added_at DESC")
            rows = cur.fetchall()
    return [{"email": r[0], "note": r[1], "added_by": r[2], "added_at": _iso(r[3])} for r in rows]


def allowed_pilot_emails():
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT email FROM allowed_pilots")
            return {r[0] for r in cur.fetchall()}


def add_allowed_pilot(email, note, added_by):
    """Adds (or updates the note of) an allowed pilot; email lower-cased.
    Their waitlist entry, if any, is removed: they're in."""
    email = email.strip().lower()
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO allowed_pilots (email, note, added_by) VALUES (%s, %s, %s)
                ON CONFLICT (email) DO UPDATE SET note = EXCLUDED.note
            """, (email, note, added_by))
            cur.execute("DELETE FROM waitlist WHERE email = %s", (email,))


def remove_allowed_pilot(email):
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM allowed_pilots WHERE email = %s", (email.strip().lower(),))
            return cur.rowcount > 0


def join_waitlist(email, consent):
    """Adds a visitor to the waitlist. Signing up again changes nothing
    (the first consent stays on record). Returns True if it's new."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO waitlist (email, consent) VALUES (%s, %s) ON CONFLICT (email) DO NOTHING",
                        (email.strip().lower(), consent))
            return cur.rowcount > 0


def list_waitlist():
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT email, consent, created_at FROM waitlist ORDER BY created_at")
            rows = cur.fetchall()
    return [{"email": r[0], "consent": r[1], "created_at": _iso(r[2])} for r in rows]


def remove_from_waitlist(email):
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM waitlist WHERE email = %s", (email.strip().lower(),))
            return cur.rowcount > 0
