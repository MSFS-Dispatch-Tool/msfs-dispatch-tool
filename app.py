"""
VirtualDispatch - Flask app.
"""

from flask import Flask, render_template, request, jsonify, session, redirect, url_for
import json
import os
import re
import secrets
import threading
import time
from concurrent.futures import ThreadPoolExecutor
import requests
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urlencode
import db
import auth
import wxmap
import blog
import flightstats
import webperf
import demo
import techlog
import ops
import generator
from generator import (
    resolve_airport, find_round_trip_pairs, find_itineraries, flight_number_digits,
    generate_leg_conditions, roll_delay, generate_callsign, generate_loadsheet_extras,
    assign_aircraft_type, pax_and_cargo, sample_delay_minutes
)
from timeutils import (
    resolve_leg_times, resolve_leg_schedule, format_zulu, turnaround_minutes,
    turnaround_shift, simbrief_date_str, TAXI_IN_MINUTES, MIN_TURNAROUND_MINUTES
)

app = Flask(__name__)
webperf.init_app(app)  # gzip + fingerprinted, long-cached static files

# Session signing key - set FLASK_SECRET_KEY in the environment so
# sessions (and therefore logins) survive a restart/redeploy. Without
# it, a random key is generated at boot and every existing session is
# invalidated the next time the process restarts.
app.secret_key = os.environ.get("FLASK_SECRET_KEY") or secrets.token_hex(32)
app.permanent_session_lifetime = timedelta(days=30)

# Per-user accounts via Supabase Auth (see auth.py) instead of the old
# single shared-password gate. If Supabase isn't configured (auth env
# vars unset), the gate is off entirely - matches the old APP_PASSWORD-
# unset behavior, so a fresh checkout without secrets configured still
# runs locally.
PUBLIC_ENDPOINTS = {"login", "signup", "auth_callback", "auth_session", "access_restricted",
                    "resend_verification_route", "request_password_reset_route", "static",
                    "index", "blog_index", "blog_post", "legal_privacy", "legal_terms", "legal_cookies"}


# The date the privacy, cookie and terms pages last changed. Update it
# whenever one of them does.
LEGAL_UPDATED = "October 1, 2026"


@app.context_processor
def inject_template_globals():
    today = date.today()
    return {
        "current_year": today.year,
        "signups_open": auth.signups_open(),
        "legal_updated": LEGAL_UPDATED,
    }


def current_user():
    return session.get("user")


def current_user_id():
    """The PIREP/settings scoping key. Falls back to a fixed sentinel
    when Supabase Auth isn't configured (local dev without those env
    vars set) so the app still works pre-account-system, single-user,
    same as it did before this migration."""
    user = current_user()
    return user["id"] if user else "local"


@app.before_request
def require_login():
    if not auth.auth_available():
        return
    if request.endpoint in PUBLIC_ENDPOINTS:
        return
    user = current_user()
    if not user:
        return redirect(url_for("login", next=request.path))
    if session.get("expires_at", 0) <= _now_ts():
        refreshed = _try_refresh()
        if not refreshed:
            session.clear()
            return redirect(url_for("login", next=request.path))
    if session.get("must_reset_password") and request.endpoint not in ("reset_password", "logout"):
        return redirect(url_for("reset_password"))
    # Access lock (see auth.email_allowed): a signed-in account that isn't
    # on the allowlist sees only the restricted page (and can sign out).
    if not _has_access(user) and request.endpoint != "logout":
        if request.accept_mimetypes.best == "application/json" or request.method != "GET":
            return jsonify({"error": "This account doesn't have access to VirtualDispatch yet."}), 403
        return redirect(url_for("access_restricted"))


def _has_access(user):
    """The access lock: allowlisted emails, plus demo accounts created from
    the admin page (checked only when the email isn't allowlisted)."""
    return auth.email_allowed(user.get("email")) or demo.is_demo_account(user.get("id"))


def _now_ts():
    return int(datetime.now(timezone.utc).timestamp())


def _store_session(token_response):
    session.clear()
    session["access_token"] = token_response["access_token"]
    session["refresh_token"] = token_response["refresh_token"]
    session["expires_at"] = _now_ts() + int(token_response.get("expires_in", 3600)) - 30
    session["user"] = {
        "id": token_response["user"]["id"],
        "email": token_response["user"]["email"],
    }
    session.permanent = True


def _try_refresh():
    refresh_token = session.get("refresh_token")
    if not refresh_token:
        return False
    try:
        token_response = auth.refresh_session(refresh_token)
        _store_session(token_response)
        return True
    except auth.AuthError:
        return False


def _auth_redirect_to():
    return request.url_root.rstrip("/") + url_for("auth_callback")


_PASSWORD_MIN_LENGTH = 8


def _signup_result_indicates_existing_user(result):
    """Supabase's signup endpoint returns HTTP 200 for an email that's
    already registered and confirmed too - it doesn't error, to avoid
    leaking which emails have accounts (enumeration protection). The
    documented tell is an empty "identities" list on the returned user
    (a genuinely new signup always has exactly one identity, the
    email/password one just created)."""
    if not isinstance(result, dict):
        return False
    user = result.get("user", result)
    if not isinstance(user, dict) or not user.get("id"):
        return False
    identities = user.get("identities")
    return identities is not None and len(identities) == 0


def _validate_password(password, confirm):
    if password != confirm:
        return "Passwords do not match."
    if len(password) < _PASSWORD_MIN_LENGTH:
        return f"Password must be at least {_PASSWORD_MIN_LENGTH} characters."
    if not re.search(r"[A-Za-z]", password):
        return "Password must include at least one letter."
    if not re.search(r"[0-9]", password):
        return "Password must include at least one number."
    if not re.search(r"[^A-Za-z0-9]", password):
        return "Password must include at least one symbol."
    return None


def _verify_turnstile_from_form():
    if not auth.turnstile_available():
        return True
    token = request.form.get("cf-turnstile-response", "")
    return auth.verify_turnstile(token, remote_ip=request.remote_addr)


def _needs_onboarding(user_id):
    if not db.db_available():
        return False
    try:
        return not db.get_settings(user_id)["profile"].get("onboarding_complete")
    except Exception:
        return False


def _post_login_redirect(user_id, next_url=None):
    if _needs_onboarding(user_id):
        return url_for("onboarding")
    return next_url or url_for("dispatch_app")


@app.route("/access-restricted")
def access_restricted():
    user = current_user()
    if not user:
        return redirect(url_for("login"))
    if _has_access(user):
        return redirect(url_for("dispatch_app"))
    return render_template("login.html", mode="restricted", signup_email=user.get("email")), 403


@app.route("/signup", methods=["GET", "POST"])
def signup():
    error = None
    if not auth.signups_open():
        # Closed while access is locked (auth.OPEN_ACCESS): no form, and a
        # POST sent anyway creates nothing.
        return render_template("login.html", mode="signup_closed"), (403 if request.method == "POST" else 200)
    if request.method == "POST":
        email = (request.form.get("email") or "").strip()
        password = request.form.get("password") or ""
        confirm = request.form.get("password_confirm") or ""
        if not _verify_turnstile_from_form():
            error = "Captcha verification failed. Please try again."
        else:
            error = _validate_password(password, confirm)
        if not error:
            try:
                result = auth.sign_up(email, password, redirect_to=_auth_redirect_to())
                if _signup_result_indicates_existing_user(result):
                    error = "That email is already registered. Try signing in, or use \"Forgot password?\" if you don't remember your password."
                else:
                    # A dedicated "check your email" screen, not the signup
                    # form again with a banner on top - there's nothing left
                    # to fill in, and the Resend button only makes sense
                    # once there's actually a pending verification email.
                    return render_template("login.html", mode="signup", just_signed_up=True, signup_email=email,
                                            turnstile_site_key=auth.TURNSTILE_SITE_KEY)
            except auth.AuthError as exc:
                error = exc.message
    return render_template("login.html", mode="signup", error=error,
                            turnstile_site_key=auth.TURNSTILE_SITE_KEY)


@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    notice = request.args.get("notice")
    if request.method == "POST":
        email = (request.form.get("email") or "").strip()
        password = request.form.get("password") or ""
        remember = request.form.get("remember") == "on"
        if not _verify_turnstile_from_form():
            error = "Captcha verification failed. Please try again."
        else:
            try:
                token_response = auth.sign_in(email, password)
                _store_session(token_response)
                session.permanent = remember
                return redirect(_post_login_redirect(token_response["user"]["id"], request.args.get("next")))
            except auth.AuthError as exc:
                error = exc.message
    return render_template("login.html", mode="login", error=error, notice=notice,
                            turnstile_site_key=auth.TURNSTILE_SITE_KEY)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/auth/reset-password", methods=["GET", "POST"])
def reset_password():
    if not session.get("must_reset_password"):
        return redirect(url_for("dispatch_app"))
    error = None
    if request.method == "POST":
        password = request.form.get("password") or ""
        confirm = request.form.get("password_confirm") or ""
        error = _validate_password(password, confirm)
        if not error:
            try:
                auth.update_password(session["access_token"], password)
                session.pop("must_reset_password", None)
                return redirect(_post_login_redirect(current_user()["id"]))
            except auth.AuthError as exc:
                error = exc.message
    return render_template("auth_reset_password.html", error=error)


@app.route("/auth/callback")
def auth_callback():
    """Supabase's emailed verification/reset link redirects here with the
    session tokens in the URL FRAGMENT (#access_token=...), which never
    reaches the server - so this just serves a page whose JS reads the
    fragment and hands the tokens to /auth/session to establish the
    Flask session."""
    return render_template("auth_callback.html")


@app.route("/auth/session", methods=["POST"])
def auth_session():
    payload = request.get_json(silent=True) or {}
    access_token = payload.get("access_token")
    refresh_token = payload.get("refresh_token")
    if not access_token or not refresh_token:
        return jsonify({"error": "Missing tokens."}), 400
    user = auth.get_user(access_token)
    if not user:
        return jsonify({"error": "Invalid or expired link."}), 400
    session.clear()
    session["access_token"] = access_token
    session["refresh_token"] = refresh_token
    session["expires_at"] = _now_ts() + int(payload.get("expires_in", 3600)) - 30
    session["user"] = {"id": user["id"], "email": user["email"]}
    session.permanent = True
    if payload.get("type") == "recovery":
        # A password-reset link, not a normal login - the pilot must set
        # a new password before they can do anything else with this
        # session (enforced in require_login), rather than landing in
        # the app still on the password they just asked to replace.
        session["must_reset_password"] = True
        return jsonify({"ok": True, "redirect": url_for("reset_password")})
    return jsonify({"ok": True, "redirect": _post_login_redirect(user["id"])})


_USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{3,20}$")
_ALLOWED_PHOTO_TYPES = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}
_MAX_PHOTO_BYTES = 2 * 1024 * 1024


def _validate_onboarding_form(form):
    username = (form.get("username") or "").strip()
    if not username:
        return None, "Username is required."
    if not _USERNAME_RE.match(username):
        return None, "Username must be 3-20 characters: letters, numbers, or underscore only."
    return username, None


def _handle_photo_upload(photo_file):
    """Validates and uploads an optional profile-photo file. Returns
    (photo_url, error) - photo_url is None if no file was given or the
    upload is skipped (e.g. Storage not configured), in which case the
    caller should leave the existing photo_url untouched."""
    if not photo_file or not photo_file.filename:
        return None, None
    content_type = photo_file.mimetype
    if content_type not in _ALLOWED_PHOTO_TYPES:
        return None, "Photo must be a JPEG, PNG, or WebP image."
    data = photo_file.read()
    if len(data) > _MAX_PHOTO_BYTES:
        return None, "Photo must be 2MB or smaller."
    if not auth.admin_available():
        return None, None
    try:
        ext = _ALLOWED_PHOTO_TYPES[content_type]
        return auth.upload_avatar(f"{current_user_id()}.{ext}", data, content_type), None
    except auth.AuthError as exc:
        return None, exc.message


@app.route("/onboarding", methods=["GET", "POST"])
def onboarding():
    if not db.db_available():
        return redirect(url_for("dispatch_app"))

    error = None
    if request.method == "POST":
        username, error = _validate_onboarding_form(request.form)
        photo_url = None
        if not error:
            photo_url, error = _handle_photo_upload(request.files.get("photo"))

        if not error:
            existing = get_settings_safe()
            profile = dict(existing["profile"])
            all_fleet_types = {t for code in ACTIVE_CARRIER_CODES for t in CARRIERS[code]["fleet_by_type"]}
            profile.update({
                "username": username,
                "first_name": (request.form.get("first_name") or "").strip(),
                "last_name": (request.form.get("last_name") or "").strip(),
                "birth_date": request.form.get("birth_date") or "",
                "nationality": request.form.get("nationality") or "",
                "preferred_base": (request.form.get("preferred_base") or "").strip().upper(),
                "aircraft_owned": [t for t in request.form.getlist("aircraft_owned") if t in all_fleet_types],
                "onboarding_complete": True,
            })
            if photo_url:
                profile["photo_url"] = photo_url
            db.save_settings({"profile": profile, "generation": existing["generation"]}, current_user_id())
            return redirect(url_for("dispatch_app"))

    fleet_by_carrier = [
        {"name": CARRIERS[code]["name"], "fleet": sorted(CARRIERS[code]["fleet_by_type"].keys())}
        for code in ACTIVE_CARRIER_CODES
    ]
    return render_template("onboarding.html", error=error, countries=countries, fleet_by_carrier=fleet_by_carrier)


@app.route("/auth/resend", methods=["POST"])
def resend_verification_route():
    email = (request.form.get("email") or "").strip()
    try:
        auth.resend_verification(email, redirect_to=_auth_redirect_to())
        notice = f"Verification email re-sent to {email}."
    except auth.AuthError as exc:
        return render_template("login.html", mode="signup", just_signed_up=True, signup_email=email, error=exc.message)
    return render_template("login.html", mode="login", notice=notice)


@app.route("/auth/reset", methods=["GET", "POST"])
def request_password_reset_route():
    notice = None
    error = None
    if request.method == "POST":
        email = (request.form.get("email") or "").strip()
        if not email:
            error = "Enter your email address."
        else:
            try:
                auth.request_password_reset(email, redirect_to=_auth_redirect_to())
            except auth.AuthError:
                pass  # never reveal whether an email is registered
            notice = f"If {email} has an account, a password reset link was sent."
    return render_template("auth_forgot_password.html", notice=notice, error=error)


# ---------------------------------------------------------------------
# Admin: read-only user list plus ban/unban/delete, via Supabase's
# service-role Admin API (auth.py). Gated on ADMIN_EMAILS, not on any
# role stored in this app's own DB, since account identity itself lives
# in Supabase Auth.
# ---------------------------------------------------------------------

def _is_admin():
    user = current_user()
    return bool(user) and user["email"].strip().lower() in auth.ADMIN_EMAILS


@app.route("/admin/users")
def admin_users_page():
    if not auth.auth_available() or not _is_admin():
        return redirect(url_for("dispatch_app"))
    return render_template("admin_users.html")


@app.route("/admin/api/users", methods=["GET"])
def admin_list_users_route():
    if not auth.auth_available() or not _is_admin():
        return jsonify({"error": "Forbidden."}), 403
    if not auth.admin_available():
        return jsonify({"error": "SUPABASE_SERVICE_ROLE_KEY is not configured."}), 502
    users = auth.admin_list_users()
    return jsonify([{
        "id": u["id"],
        "email": u.get("email"),
        "created_at": u.get("created_at"),
        "last_sign_in_at": u.get("last_sign_in_at"),
        "email_confirmed_at": u.get("email_confirmed_at"),
        "banned_until": u.get("banned_until"),
    } for u in users])


@app.route("/admin/api/users/<user_id>/ban", methods=["POST"])
def admin_ban_user_route(user_id):
    if not auth.auth_available() or not _is_admin():
        return jsonify({"error": "Forbidden."}), 403
    payload = request.get_json(silent=True) or {}
    auth.admin_set_banned(user_id, bool(payload.get("banned")))
    return jsonify({"ok": True})


@app.route("/admin/api/users/<user_id>", methods=["DELETE"])
def admin_delete_user_route(user_id):
    if not auth.auth_available() or not _is_admin():
        return jsonify({"error": "Forbidden."}), 403
    if current_user() and current_user()["id"] == user_id:
        return jsonify({"error": "Can't delete your own account from here."}), 400
    auth.admin_delete_user(user_id)
    if db.db_available():
        db.delete_user_data(user_id)
    return jsonify({"ok": True})


@app.route("/admin/api/demo-account", methods=["POST"])
def admin_demo_account_route():
    """Creates or resets a demo pilot with a seeded logbook (see demo.py)."""
    if not auth.auth_available() or not _is_admin():
        return jsonify({"error": "Forbidden."}), 403
    payload = request.get_json(silent=True) or {}
    email = (payload.get("email") or "").strip()
    password = payload.get("password") or ""
    if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
        return jsonify({"error": "Enter a valid email address"}), 400
    if len(password) < 8:
        return jsonify({"error": "The password needs at least 8 characters"}), 400
    try:
        flights = max(1, min(1000, int(payload.get("flights") or 170)))
    except (TypeError, ValueError):
        return jsonify({"error": "Number of flights must be a number"}), 400
    try:
        result = demo.seed_account(email, password, CARRIERS, flights=flights,
                                   base=payload.get("base") or "EGKK")
    except (demo.DemoError, auth.AuthError) as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify({"ok": True, "email": email, **result})


DATA_DIR = os.path.join(os.path.dirname(__file__), "data")


def load_json(filename):
    path = os.path.join(DATA_DIR, filename)
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def load_carrier(code):
    """Loads one carrier's config + route network from data/carriers/<code>/,
    plus the MEL list for each aircraft type in its fleet (shared under
    data/aircraft/<group>/ since a MEL set belongs to the airframe/addon,
    not the airline painted on it - two carriers flying the same type,
    or several variants of the same family via "mel_group" on a fleet
    entry, reuse the same MEL data instead of each needing its own copy).

    Every route gets a "carrier" field stamped on here - once more than
    one carrier is active, that's what everything downstream (seat
    capacity, MEL set, SimBrief airline/aircraft-type, round-trip
    pairing) keys off, rather than assuming a single global carrier.

    MEL ids are namespaced by mel_group ("a320fam/mel-03") since two
    different aircraft families' MEL files both start counting from
    mel-01 - without this, combining carriers' MELs into one settings
    list (see /generation-options) would silently collide two unrelated
    failures under the same id. "738" is the one exception, left
    unprefixed: it's the only family that existed before this app had
    more than one, so real pilots already have "mel-03"-style ids saved
    in their disabled_ids settings in production - prefixing it too
    would silently un-disable everyone's existing MEL preferences.

    This is the seam a new carrier hangs off: add a data/carriers/<code>/
    folder with its own carrier.json + routes.json, and (if it's a new
    aircraft family) a data/aircraft/<group>/ folder, then add its code
    to ACTIVE_CARRIER_CODES below.
    """
    base = os.path.join("carriers", code)
    config = load_json(os.path.join(base, "carrier.json"))
    carrier_routes = load_json(os.path.join(base, "routes.json"))
    for r in carrier_routes:
        r["carrier"] = code
    fleet_by_type = {ac["type"]: ac for ac in config["fleet"]}

    mel_groups = {ac.get("mel_group", ac["type"]) for ac in fleet_by_type.values()}

    def load_group_items(filename):
        items, seen = [], set()
        for mel_group in sorted(mel_groups):
            path = os.path.join("aircraft", mel_group, filename)
            if not os.path.exists(os.path.join(DATA_DIR, path)):
                continue
            for m in load_json(path):
                namespaced_id = m["id"] if mel_group == "738" else f"{mel_group}/{m['id']}"
                if namespaced_id not in seen:
                    seen.add(namespaced_id)
                    m["id"] = namespaced_id
                    m["fleet"] = mel_group
                    items.append(m)
        return items

    # CDL items (configuration deviations: missing panels and fairings,
    # see techlog.py) are grouped and namespaced the same way.
    carrier_mels = load_group_items("mel_list.json")
    carrier_cdls = load_group_items("cdl_list.json")

    return {
        "icao": config["icao"], "iata": config["iata"], "name": config["name"],
        "callsign_prefix": config["callsign_prefix"],
        "fleet_by_type": fleet_by_type,
        "routes": carrier_routes,
        "mels": carrier_mels,
        "cdls": carrier_cdls,
    }


# See load_carrier's docstring for how a new carrier plugs in. Route/MEL
# data downstream is keyed per-route/per-carrier (via each route's own
# "carrier" field) rather than assuming a single global carrier, so
# adding one here is a data change, not a rewrite of /search, /select,
# /confirm etc.
ACTIVE_CARRIER_CODES = ("RYR", "EZY", "WZZ")
CARRIERS = {code: load_carrier(code) for code in ACTIVE_CARRIER_CODES}

routes = [r for code in ACTIVE_CARRIER_CODES for r in CARRIERS[code]["routes"]]
routes_by_flight_number = {r["flight_number"]: r for r in routes}
rt_pairing = find_round_trip_pairs(routes)

delay_codes = load_json("delay_codes.json")
lmc_events = load_json("lmc_events.json")
dangerous_goods = load_json("dangerous_goods.json")
airports = load_json("airports.json")
countries = load_json("countries.json")
# The full IATA standard delay-code list (AHM 730), for the pilot to code
# their own delays in the PIREP - delay_codes.json is only the subset the
# generator rolls.
iata_delay_codes = load_json("iata_delay_codes.json")
# Worldwide airport reference (OurAirports, public domain), separate from
# the route-network `airports` above - used only for the preferred base
# picker and destinations map, never for route search, since a pilot's
# home base can be any real airport, not just one a loaded carrier serves.
airports_world = load_json("airports_world.json")

country_by_icao = {a["icao"]: a["country"] for a in airports}
tz_by_icao = {a["icao"]: a["tz"] for a in airports}
airports_by_icao = {a["icao"]: a for a in airports}
airports_world_by_icao = {a["icao"]: a for a in airports_world}


def build_network():
    """Compact route-network summary for the flight-selection map: every
    airport with at least one route, and every directed airport pair with
    a bitmask of the carriers flying it (bit i = ACTIVE_CARRIER_CODES[i]).
    Indices instead of repeated ICAO strings keep ~6.7k pairs small."""
    carrier_bit = {code: 1 << i for i, code in enumerate(ACTIVE_CARRIER_CODES)}
    masks = {}
    for r in routes:
        key = (r["departure_icao"], r["arrival_icao"])
        masks[key] = masks.get(key, 0) | carrier_bit[r["carrier"]]
    served = sorted({icao for pair in masks for icao in pair if icao in airports_by_icao})
    index = {icao: i for i, icao in enumerate(served)}
    return {
        "carriers": list(ACTIVE_CARRIER_CODES),
        "airports": [
            {k: airports_by_icao[icao][k] for k in ("icao", "iata", "name", "city", "country", "lat", "lon")}
            for icao in served
        ],
        "routes": [[index[d], index[a], m] for (d, a), m in sorted(masks.items())
                   if d in index and a in index],
    }


network = build_network()

WEATHER_USER_AGENT = "VirtualDispatch/1.0 (personal MSFS immersion tool; not for real-world ops use)"


def carrier_for(route_leg):
    return CARRIERS[route_leg["carrier"]]


def fleet_entry_for(route_leg, owned_types=None):
    """The fleet entry for the aircraft type assigned to a route leg -
    the route's own aircraft_type when the data specifies one (true for
    100% of Ryanair's routes, ~1% of easyJet's - AirLabs doesn't report
    aircraft type for most of its schedule data), otherwise a weighted
    random pick from the pilot's owned aircraft that carrier flies (or
    its whole fleet, same weighting, if the pilot hasn't set any/none
    apply) - see generator.assign_aircraft_type."""
    fleet = carrier_for(route_leg)["fleet_by_type"]
    chosen = assign_aircraft_type(fleet, owned_types, route_leg.get("aircraft_type"))
    return fleet[chosen]


def owned_aircraft_for(settings, carrier_code):
    """The pilot's aircraft_owned list, narrowed to types that carrier's
    fleet actually flies - a saved 320 shouldn't influence a Ryanair
    assignment just because it's in the pilot's list."""
    owned = set(settings.get("profile", {}).get("aircraft_owned") or [])
    fleet = CARRIERS[carrier_code]["fleet_by_type"]
    return owned & set(fleet.keys())


def get_settings_safe():
    """Settings used by generation (delay/LMC/MEL enable + per-item
    toggles). Never raises - falls back to "everything on" (matching
    behavior from before these toggles existed) if the DB isn't
    configured or a query fails, so /select and /confirm keep working
    regardless of the settings store's health."""
    if not db.db_available():
        return db.DEFAULT_SETTINGS
    try:
        return db.get_settings(current_user_id())
    except Exception:
        return db.DEFAULT_SETTINGS


# METAR/TAF per airport, kept for 10 minutes: SELECT, an aircraft swap and a
# re-SELECT of the same trip reuse them instead of calling aviationweather.gov
# again (METARs are issued every 30-60 minutes). Only successful fetches are
# cached, so an outage is retried on the next request.
WEATHER_TTL_SECONDS = 10 * 60
_weather_cache = {}
_weather_cache_lock = threading.Lock()


def _fetch_reports(kind, ids, field):
    """{icao: raw report} from aviationweather.gov, or None if the call failed."""
    try:
        resp = requests.get(f"https://aviationweather.gov/api/data/{kind}",
                            params={"ids": ",".join(ids), "format": "json"},
                            headers={"User-Agent": WEATHER_USER_AGENT}, timeout=8)
        resp.raise_for_status()
        return {item.get("icaoId"): item.get(field) for item in resp.json()}
    except Exception:
        return None


def fetch_weather_batch(icao_list):
    unique = sorted(set(icao_list))
    now = time.monotonic()
    result, missing = {}, []
    with _weather_cache_lock:
        for icao in unique:
            hit = _weather_cache.get(icao)
            if hit and now - hit[0] < WEATHER_TTL_SECONDS:
                result[icao] = dict(hit[1])
            else:
                missing.append(icao)
    if missing:
        # METAR and TAF in parallel: two round trips cost one
        with ThreadPoolExecutor(max_workers=2) as pool:
            metars_f = pool.submit(_fetch_reports, "metar", missing, "rawOb")
            tafs_f = pool.submit(_fetch_reports, "taf", missing, "rawTAF")
            metars, tafs = metars_f.result(), tafs_f.result()
        with _weather_cache_lock:
            for icao in missing:
                wx = {"metar": (metars or {}).get(icao), "taf": (tafs or {}).get(icao)}
                result[icao] = wx
                if metars is not None and tafs is not None:
                    _weather_cache[icao] = (now, dict(wx))
    return result


def airport_info(icao):
    a = airports_by_icao.get(icao, {})
    return {
        "icao": icao,
        "iata": a.get("iata", ""),
        "name": a.get("name", "UNKNOWN"),
        "country": a.get("country", ""),
        "lat": a.get("lat"),
        "lon": a.get("lon"),
    }


def leg_summary_with_times(route, today):
    """Shared by /search (list view) and /select (detail view): airport
    info, cost-agnostic route facts, and Zulu dep/arr times."""
    dep_dt, arr_dt, arr_source = resolve_leg_times(route, tz_by_icao, today)
    # Arrival's "(+Nd)" suffix is relative to THIS leg's own departure
    # date, not "today" - a local-midnight departure at an airport east
    # of UTC legitimately lands on "yesterday" in Zulu (flagged on
    # departure), but an arrival an hour later isn't a further day
    # change and showing the same "-1d" on both just reads as a
    # contradiction.
    arr_reference = dep_dt.date() if dep_dt is not None else today
    return {
        "flight_number": route["flight_number"],
        "departure_info": airport_info(route["departure_icao"]),
        "arrival_info": airport_info(route["arrival_icao"]),
        "duration_minutes": route["duration_minutes"],
        "distance_nm": route["distance_nm"],
        "scheduled_departure_zulu": format_zulu(dep_dt, today),
        "scheduled_arrival_zulu": format_zulu(arr_dt, arr_reference),
        "arrival_time_source": arr_source,
        "_dep_dt": dep_dt, "_arr_dt": arr_dt,  # internal, stripped before jsonify
    }


def strip_internal(d):
    return {k: v for k, v in d.items() if not k.startswith("_")}


@app.route("/")
def index():
    return render_template("landing.html")


@app.route("/app")
def dispatch_app():
    counts = {
        "routes": f"{len(routes):,}",
        "mels": f"{sum(len(CARRIERS[code]['mels']) for code in ACTIVE_CARRIER_CODES):,}",
        "delay_codes": f"{len(delay_codes):,}",
        "lmc_events": len(lmc_events), "dangerous_goods": len(dangerous_goods),
    }
    user = current_user()
    return render_template("index.html", counts=counts, auth_enabled=auth.auth_available(), is_admin=_is_admin(),
                            current_user_email=(user["email"] if user else ""), countries=countries,
                           iata_delay_codes=iata_delay_codes, min_turnaround_minutes=MIN_TURNAROUND_MINUTES)


@app.route("/blog")
def blog_index():
    return render_template("blog_index.html", posts=blog.list_posts())


@app.route("/blog/<slug>")
def blog_post(slug):
    post = blog.get_post(slug)
    if not post:
        return render_template("blog_index.html", posts=blog.list_posts()), 404
    return render_template("blog_post.html", post=post)


@app.route("/privacy")
def legal_privacy():
    return render_template("legal_privacy.html")


@app.route("/terms")
def legal_terms():
    return render_template("legal_terms.html")


@app.route("/cookies")
def legal_cookies():
    return render_template("legal_cookies.html")


@app.route("/airports/search")
def airports_search():
    q = request.args.get("q", "").strip().upper()
    if not q:
        return jsonify([])
    matches = [a for a in airports if a["icao"].startswith(q) or a["iata"].startswith(q) or a["city"].upper().startswith(q)]
    return jsonify(matches[:15])


@app.route("/airports/validate")
def airports_validate():
    code = request.args.get("code", "")
    icao = resolve_airport(airports, code)
    return jsonify({"valid": icao is not None, "icao": icao})


@app.route("/carriers")
def carriers_route():
    """The active carriers (and each one's fleet), for the search page's
    AIRLINE filter and the "which aircraft do you fly" pickers in
    onboarding/account settings - a plain data list rather than a
    hardcoded frontend dropdown, so a new carrier going live (adding its
    code to ACTIVE_CARRIER_CODES) shows up here with no frontend change
    needed."""
    return jsonify([
        {
            "icao": CARRIERS[code]["icao"], "name": CARRIERS[code]["name"],
            "fleet": [{"type": t, "seats": ac["seats"], "simbrief_type": ac["simbrief_type"]}
                      for t, ac in CARRIERS[code]["fleet_by_type"].items()],
        }
        for code in ACTIVE_CARRIER_CODES
    ])


@app.route("/network")
def network_route():
    """Airports and airport pairs for the flight-selection map. Static for
    the life of the process (it only changes when route data does), so
    browsers may cache it."""
    resp = jsonify(network)
    resp.headers["Cache-Control"] = "public, max-age=3600"
    return resp


@app.route("/wx/metars")
def wx_metars():
    """Flight category + raw METAR for every network airport, for the map's
    weather overlay. Cached server-side (see wxmap), so page loads never
    fan out into aviationweather.gov requests."""
    try:
        data = wxmap.metar_categories([a["icao"] for a in network["airports"]], WEATHER_USER_AGENT)
    except Exception:
        return jsonify({"error": "Weather data is unavailable right now."}), 503
    resp = jsonify(data)
    resp.headers["Cache-Control"] = "public, max-age=300"
    return resp


@app.route("/wx/sigmets")
def wx_sigmets():
    """Current international SIGMETs over the network region, as polygons."""
    try:
        data = wxmap.current_sigmets(WEATHER_USER_AGENT)
    except Exception:
        return jsonify({"error": "SIGMET data is unavailable right now."}), 503
    resp = jsonify(data)
    resp.headers["Cache-Control"] = "public, max-age=120"
    return resp


@app.route("/airports/search/world")
def airports_search_world():
    """Same shape as /airports/search but scoped to the full worldwide
    dataset - used by the preferred base picker, which isn't limited to
    airports Ryanair actually serves."""
    q = request.args.get("q", "").strip().upper()
    if not q:
        return jsonify([])
    matches = [a for a in airports_world
               if a["icao"].startswith(q) or a["iata"].startswith(q) or a["city"].upper().startswith(q)]
    return jsonify(matches[:15])


@app.route("/search")
def search():
    origin_raw = request.args.get("origin", default="", type=str)
    destination_raw = request.args.get("destination", default="", type=str)
    if not origin_raw.strip() and not destination_raw.strip():
        # An unfiltered search is the whole network (~19 MB, ~2 s); the app
        # never sends one, so refuse it rather than let anyone trigger it
        return jsonify({"error": "Give a departure or destination airport."}), 400
    trip_type = request.args.get("trip_type", default="random", type=str)
    airline = request.args.get("airline", default="", type=str).strip().upper()
    if airline and airline not in ACTIVE_CARRIER_CODES:
        return jsonify({"error": f"Unknown carrier: {airline}"}), 400

    origin_icao = resolve_airport(airports, origin_raw) if origin_raw else None
    if origin_raw and origin_icao is None:
        return jsonify({"error": f"Unknown airport code: {origin_raw}"}), 400
    destination_icao = resolve_airport(airports, destination_raw) if destination_raw else None
    if destination_raw and destination_icao is None:
        return jsonify({"error": f"Unknown airport code: {destination_raw}"}), 400

    itineraries = find_itineraries(
        routes, rt_pairing,
        origin_icao=origin_icao, destination_icao=destination_icao,
        trip_type=trip_type
    )
    # Filtered on the output, not the input routes: rt_pairing is built
    # from the full merged route list, so handing find_itineraries a
    # pre-filtered single-carrier route list would leave it looking up
    # the other carrier's flight numbers in a dict that no longer has
    # them. Every leg of one itinerary is the same carrier (see
    # generator.find_round_trip_pairs), so checking the first is enough.
    if airline:
        itineraries = [itin for itin in itineraries if itin["legs"][0]["carrier"] == airline]

    today = date.today()
    summaries = []
    for itin in itineraries:
        legs = itin["legs"]
        leg_data = [leg_summary_with_times(leg, today) for leg in legs]

        # A round-trip pair is matched by flight-number proximity, not by
        # which one actually departs first - real rotations sometimes fly
        # the higher-numbered leg first. Reorder by actual scheduled
        # departure time whenever both are resolvable, so leg[0] is
        # always the one that departs first.
        if itin["trip_type"] == "RT" and len(leg_data) == 2 \
                and leg_data[0]["_dep_dt"] is not None and leg_data[1]["_dep_dt"] is not None \
                and leg_data[1]["_dep_dt"] < leg_data[0]["_dep_dt"]:
            legs = [legs[1], legs[0]]
            leg_data = [leg_data[1], leg_data[0]]

        # The time order is the real rotation (e.g. a Geneva-based aircraft
        # flies GVA-LGW-GVA), so the airport filters must hold after it:
        # otherwise a search "from EGKK" lists rotations that start in
        # Geneva, and the map draws their origin and turnaround swapped.
        # Each rotation still appears under its true origin/turnaround.
        if itin["trip_type"] == "RT" and (
                (origin_icao and legs[0]["departure_icao"] != origin_icao)
                or (destination_icao and legs[0]["arrival_icao"] != destination_icao)):
            continue

        # Even in the right order, scraped schedule times occasionally
        # leave too little (or a negative) turnaround between the two
        # legs of a rotation. Push the second leg's whole schedule later
        # by whatever's needed for a realistic minimum turnaround.
        if itin["trip_type"] == "RT" and len(leg_data) == 2:
            shift = turnaround_shift(leg_data[0]["_arr_dt"], leg_data[1]["_dep_dt"])
            if shift:
                leg_data[1]["_dep_dt"] += shift
                leg_data[1]["_arr_dt"] += shift
                leg_data[1]["scheduled_departure_zulu"] = format_zulu(leg_data[1]["_dep_dt"], today)
                leg_data[1]["scheduled_arrival_zulu"] = format_zulu(leg_data[1]["_arr_dt"], leg_data[1]["_dep_dt"].date())

        # turnaround between legs (only meaningful for RT, 2 legs)
        for i in range(1, len(leg_data)):
            leg_data[i]["turnaround_minutes"] = turnaround_minutes(leg_data[i - 1]["_arr_dt"], leg_data[i]["_dep_dt"])

        dep_country = country_by_icao.get(legs[0]["departure_icao"], "")
        # "away" airport for RT = leg[0] arrival; for 1W = leg[-1] arrival
        away_country = country_by_icao.get(legs[0]["arrival_icao"], "")
        # For a round trip the rotation's final airport is always back at
        # origin, so "VIA" in the UI shows the away/turnaround airport
        # instead (the outbound leg's arrival). One-way legs fly direct,
        # so there's no via airport at all.
        via_icao = legs[0]["arrival_icao"] if itin["trip_type"] == "RT" else None

        first_dep_dt = leg_data[0]["_dep_dt"]
        last_arr_dt = leg_data[-1]["_arr_dt"]
        ebt_minutes = round((last_arr_dt - first_dep_dt).total_seconds() / 60) \
            if first_dep_dt is not None and last_arr_dt is not None else None

        summaries.append({
            "trip_type": itin["trip_type"],
            "carrier": legs[0]["carrier"],
            "flight_numbers": [l["flight_number"] for l in legs],
            "path": [legs[0]["departure_icao"]] + [l["arrival_icao"] for l in legs],
            "total_minutes": sum(l["duration_minutes"] for l in legs),
            "total_distance_nm": sum(l["distance_nm"] for l in legs),
            "ebt_minutes": ebt_minutes,
            "legs": len(legs),
            "departure_icao": legs[0]["departure_icao"],
            "arrival_icao": legs[-1]["arrival_icao"],
            "via_icao": via_icao,
            "departure_country": dep_country,
            "arrival_country": away_country,
            "domestic": dep_country != "" and dep_country == away_country,
            "leg_details": [strip_internal(l) for l in leg_data],
            "first_departure_zulu": leg_data[0]["scheduled_departure_zulu"],
            "last_arrival_zulu": leg_data[-1]["scheduled_arrival_zulu"],
        })

    return jsonify({"count": len(summaries), "itineraries": summaries})


@app.route("/select")
def select():
    flights_raw = request.args.get("flights", default="", type=str)
    flight_numbers = [f.strip() for f in flights_raw.split(",") if f.strip()]

    itinerary = []
    for fn in flight_numbers:
        leg = routes_by_flight_number.get(fn)
        if leg is None:
            return jsonify({"error": f"Unknown flight number: {fn}"}), 400
        itinerary.append(leg)

    if not itinerary:
        return jsonify({"error": "No flights specified."}), 400

    today = date.today()
    icao_needed = set()
    for leg in itinerary:
        icao_needed.add(leg["departure_icao"])
        icao_needed.add(leg["arrival_icao"])
    weather = fetch_weather_batch(list(icao_needed))
    settings = get_settings_safe()

    # A MEL is an aircraft equipment status, not a per-sector event - it
    # stays deferred on the airframe until rectified, so it's rolled ONCE
    # for the whole itinerary (this app models a single airframe per
    # rotation) and applied to every leg, rather than independently
    # re-rolled per leg. Every leg of one itinerary is the same carrier
    # (round-trip pairing never crosses carriers - see generator.py), so
    # the first leg's fleet is the whole itinerary's fleet.
    owned_types = owned_aircraft_for(settings, itinerary[0]["carrier"])
    # Several deferred items at once are possible, plus a CDL deviation -
    # see techlog.roll_tech_status. Only items for the aircraft family
    # this itinerary is flown on.
    tech_status = roll_itinerary_tech(itinerary, owned_types, settings)

    legs_out = []
    leg_times = []
    prev_eibt_dt = None
    for route_leg in itinerary:
        fleet_entry = fleet_entry_for(route_leg, owned_types)
        conditions = generate_leg_conditions(
            duration_minutes=route_leg["duration_minutes"],
            seat_capacity=fleet_entry["seats"],
        )
        conditions["aircraft_type"] = fleet_entry["type"]
        conditions["mels"] = tech_status["mels"]
        conditions["cdl"] = tech_status["cdl"]
        # The first item under the old single-MEL field, for anything that
        # still reads leg.mel (stats, older saved flights).
        conditions["mel"] = tech_status["mels"][0] if tech_status["mels"] else None
        conditions["flight_number"] = route_leg["flight_number"]
        conditions["carrier"] = route_leg["carrier"]
        conditions["departure_info"] = airport_info(route_leg["departure_icao"])
        conditions["arrival_info"] = airport_info(route_leg["arrival_icao"])
        dep_weather = weather.get(route_leg["departure_icao"], {"metar": None, "taf": None})
        arr_weather = weather.get(route_leg["arrival_icao"], {"metar": None, "taf": None})
        conditions["weather"] = {"departure": dep_weather, "arrival": arr_weather}
        # Only the first leg rolls a delay of its own. From the second leg
        # on the pilot is in command of the aircraft, so the only delay
        # dispatch expects is the knock-on from the previous leg (below).
        # Weather-flagged delay codes (departure/destination weather,
        # de-icing) only enter the pool when the leg's own METAR actually
        # supports them - see generator.roll_delay - so a summer 20C
        # departure never rolls "de-icing of aircraft".
        conditions["delay"] = None if legs_out else roll_delay(
            delay_codes, settings["generation"]["delay"],
            dep_metar=dep_weather.get("metar"), arr_metar=arr_weather.get("metar"), month=today.month,
        )
        taxi_out = conditions["taxi_out_minutes"]
        schedule = resolve_leg_schedule(route_leg, tz_by_icao, today, taxi_out)
        sobt_dt, sibt_dt = schedule["_sobt_dt"], schedule["_sibt_dt"]

        # Enforce a minimum realistic turnaround against the previous leg
        # in this itinerary - scraped schedule times occasionally leave
        # too little (or a negative) turnaround. Shifts this leg's whole
        # schedule later by the deficit; never invents a different flight.
        if sobt_dt is not None and leg_times:
            prev_sibt_dt = leg_times[-1][1]
            shift = turnaround_shift(prev_sibt_dt, sobt_dt)
            if shift:
                sobt_dt += shift
                sibt_dt += shift

        if sobt_dt is not None:
            # STOT/SLDT/SIBT get their "(+Nd)" suffix relative to THIS
            # LEG's own SOBT date, not the external "today" reference.
            # SOBT itself is the one row that says how this leg's Zulu
            # clock relates to today (e.g. a local-midnight departure at
            # an eastern-of-UTC airport is legitimately "yesterday" in
            # Zulu) - repeating that same offset on every other row of
            # the same leg, when nothing actually crosses a further day
            # boundary, just reads as a contradiction ("how can arrival
            # be a different day from departure when they're an hour
            # apart?"), so those rows are only flagged if THEY cross a
            # boundary SOBT didn't already.
            sobt_date = sobt_dt.date()
            stot_dt = sobt_dt + timedelta(minutes=taxi_out)
            sldt_dt = sibt_dt - timedelta(minutes=TAXI_IN_MINUTES)
            conditions["sobt"] = format_zulu(sobt_dt, today)
            conditions["stot"] = format_zulu(stot_dt, sobt_date)
            conditions["sldt"] = format_zulu(sldt_dt, sobt_date)
            conditions["sibt"] = format_zulu(sibt_dt, sobt_date)
        else:
            sobt_date = None
            stot_dt = sldt_dt = None
            conditions["sobt"] = conditions["stot"] = conditions["sldt"] = conditions["sibt"] = None
        conditions["eet_minutes"] = schedule["eet_minutes"]
        conditions["tech"] = techlog.leg_tech(
            tech_status, route_leg["departure_icao"], route_leg["arrival_icao"],
            dep_weather, arr_weather, schedule["eet_minutes"],
        )

        # Expected (E-) times = scheduled (S-) times shifted by the leg's
        # own delay, if any (its sampled length - see
        # generator.sample_delay_minutes). No delay means expected ==
        # scheduled. Later legs can slip further once the previous leg's
        # actual in-block time is known (reactionary delay, applied in
        # the browser when that PIREP is filed). This is also what
        # /simbrief/redirect-url sends as deph/depm, not the raw SOBT.
        # Same day-suffix anchoring as above: EOBT vs today (it's the
        # same kind of figure as SOBT), ETOT/ELDT/EIBT vs this leg's own
        # SOBT date.
        # Knock-on (reactionary, code 93) delay expected from the previous
        # leg: this leg can't leave before that leg's expected in-block time
        # plus the minimum turnaround, so a delay the schedule's turnaround
        # can't absorb carries over. Replaced by the actual figure once the
        # previous leg's PIREP is in (applyReactionaryDelay in the browser).
        knock_on = 0
        if legs_out and sobt_dt is not None and prev_eibt_dt is not None:
            ready = prev_eibt_dt + timedelta(minutes=MIN_TURNAROUND_MINUTES)
            knock_on = max(0, round((ready - sobt_dt).total_seconds() / 60))
        conditions["reactionary"] = {
            "iata_code": "93", "description": "Aircraft rotation: late arrival of aircraft from the previous sector",
            "minutes": knock_on, "expected": True,
        } if knock_on else None
        delay_minutes = max(conditions["delay"]["minutes"] if conditions["delay"] else 0, knock_on)
        conditions["expected_delay_minutes"] = delay_minutes
        if sobt_dt is not None:
            delay_delta = timedelta(minutes=delay_minutes)
            conditions["eobt"] = format_zulu(sobt_dt + delay_delta, today)
            conditions["etot"] = format_zulu(stot_dt + delay_delta, sobt_date)
            conditions["eldt"] = format_zulu(sldt_dt + delay_delta, sobt_date)
            conditions["eibt"] = format_zulu(sibt_dt + delay_delta, sobt_date)
        else:
            conditions["eobt"] = conditions["etot"] = conditions["eldt"] = conditions["eibt"] = None

        # An ATFM delay comes with a slot (CTOT); curfews at either end are
        # checked in the browser against the leg's current expected times.
        conditions["atfm"] = ops.atfm_slot(conditions["delay"], conditions["departure_info"],
                                           conditions["arrival_info"], conditions["etot"])
        conditions["curfews"] = ops.leg_curfews(route_leg["departure_icao"], route_leg["arrival_icao"], tz_by_icao,
                                                sobt_dt.date() if sobt_dt else today)

        prev_eibt_dt = sibt_dt + timedelta(minutes=delay_minutes) if sibt_dt is not None else None
        leg_times.append((sobt_dt, sibt_dt))
        legs_out.append(conditions)

    for i in range(1, len(legs_out)):
        legs_out[i]["turnaround_minutes"] = turnaround_minutes(leg_times[i - 1][1], leg_times[i][0])
    # Crew duty for the whole rotation, kept on the first leg (see ops.crew_duty).
    legs_out[0]["duty"] = ops.crew_duty(leg_times[0][0], tz_by_icao.get(itinerary[0]["departure_icao"]), len(legs_out))

    return jsonify({"itinerary": itinerary, "legs": legs_out})


def roll_itinerary_tech(itinerary, owned_types, settings, exclude_ids=()):
    """MEL and CDL items for an itinerary's aircraft (one airframe flies
    the whole rotation, so they're rolled once, not per leg)."""
    carrier = carrier_for(itinerary[0])
    family = fleet_entry_for(itinerary[0], owned_types)
    group = family.get("mel_group", family["type"])
    mels = [m for m in carrier["mels"] if m.get("fleet") == group]
    cdls = [c for c in carrier["cdls"] if c.get("fleet") == group]
    return techlog.roll_tech_status(mels, cdls, settings["generation"]["mel"], settings["generation"].get("cdl"),
                                    exclude_ids=exclude_ids)


AIRCRAFT_SWAP_DELAY_RANGE = (20, 45)


@app.route("/select/swap-aircraft")
def select_swap_aircraft():
    """Dispatch swaps the aircraft when a deferred item makes a leg NO-GO
    in today's weather (see techlog.leg_tech). The replacement comes with
    its own technical status - rolled again without the offending items,
    and without any item that would ground it in the same weather - and
    the swap costs a code 46 delay (aircraft change for technical
    reasons) on the first leg. Everything else already reviewed stays."""
    flight_numbers = [f.strip() for f in request.args.get("flights", default="", type=str).split(",") if f.strip()]
    itinerary = [routes_by_flight_number.get(fn) for fn in flight_numbers]
    if not itinerary or any(leg is None for leg in itinerary):
        return jsonify({"error": "Unknown flights."}), 400
    exclude = {i for i in request.args.get("exclude", default="", type=str).split(",") if i}
    eets = request.args.get("eet", default="", type=str).split(",")
    weather = fetch_weather_batch([icao for leg in itinerary for icao in (leg["departure_icao"], leg["arrival_icao"])])
    settings = get_settings_safe()
    owned_types = owned_aircraft_for(settings, itinerary[0]["carrier"])

    def eet(i):
        try:
            return int(eets[i])
        except (IndexError, ValueError):
            return itinerary[i]["duration_minutes"]

    status, legs_tech = None, None
    for _ in range(25):
        status = roll_itinerary_tech(itinerary, owned_types, settings, exclude_ids=exclude)
        legs_tech = [techlog.leg_tech(status, leg["departure_icao"], leg["arrival_icao"],
                                      weather.get(leg["departure_icao"]), weather.get(leg["arrival_icao"]), eet(i))
                     for i, leg in enumerate(itinerary)]
        grounded = techlog.nogo_items(legs_tech)
        if not grounded:
            break
        exclude |= grounded
    minutes = sample_delay_minutes(AIRCRAFT_SWAP_DELAY_RANGE)
    return jsonify({
        "mels": status["mels"], "cdl": status["cdl"], "tech": legs_tech,
        "aircraft_change": {"iata_code": "46", "description": "Aircraft change for technical reasons", "minutes": minutes},
    })


@app.route("/select/reassign-aircraft")
def select_reassign_aircraft():
    """Recomputes pax/cargo for one leg against a pilot-chosen aircraft
    type, called when the pilot changes the auto-assigned aircraft in
    the itinerary preview (before CONFIRM). load_factor is passed back
    in from what /select already rolled and showed - changing the plane
    re-derives pax/cargo for its capacity, it doesn't silently re-roll
    how full the flight is, which the pilot already reviewed."""
    fn = request.args.get("flight_number", default="", type=str)
    aircraft_type = request.args.get("aircraft_type", default="", type=str)
    load_factor = request.args.get("load_factor", type=float)

    route_leg = routes_by_flight_number.get(fn)
    if route_leg is None:
        return jsonify({"error": f"Unknown flight number: {fn}"}), 400
    if load_factor is None or not (0 < load_factor <= 1):
        return jsonify({"error": "A valid load_factor is required."}), 400

    fleet = carrier_for(route_leg)["fleet_by_type"]
    if aircraft_type not in fleet:
        return jsonify({"error": f"{aircraft_type} isn't in this flight's fleet."}), 400

    pax_count, load_factor, cargo_weight_kg = pax_and_cargo(
        fleet[aircraft_type]["seats"], route_leg["duration_minutes"], load_factor
    )
    return jsonify({
        "aircraft_type": aircraft_type,
        "seat_capacity": fleet[aircraft_type]["seats"],
        "pax_count": pax_count,
        "load_factor": load_factor,
        "cargo_weight_kg": cargo_weight_kg,
    })


@app.route("/confirm")
def confirm():
    """
    Called when the person presses CONFIRM after reviewing a flight.
    Generates a callsign PER LEG (only happens here, never earlier -
    each flight number/sector gets its own, since real callsigns are
    per-flight, not per-rotation) and rolls dangerous goods + LMC
    together, as the loadsheet-signing moment.
    """
    flights_raw = request.args.get("flights", default="", type=str)
    flight_numbers = [f.strip() for f in flights_raw.split(",") if f.strip()]
    itinerary = [routes_by_flight_number[fn] for fn in flight_numbers if fn in routes_by_flight_number]
    if not itinerary:
        return jsonify({"error": "No flights specified."}), 400

    # A route's own callsign_prefix (e.g. easyJet's per-route EJU/EZY/EZS -
    # the real operating subsidiary) wins over the carrier's default when
    # present; Ryanair's routes don't set one, so they fall back to the
    # carrier-level "RYR" exactly as before.
    callsigns = [
        generate_callsign(route_leg.get("callsign_prefix") or carrier_for(route_leg)["callsign_prefix"])
        for route_leg in itinerary
    ]
    settings = get_settings_safe()
    full_flight = request.args.get("full", type=int)
    dg, lmc = generate_loadsheet_extras(dangerous_goods, lmc_events, settings["generation"]["lmc"],
                                        full_flight=None if full_flight is None else bool(full_flight))

    return jsonify({
        "confirmation_id": callsigns[0],  # unique enough for this tool's purposes
        "callsigns": callsigns,           # one per flight_numbers[i], same order
        "flight_numbers": flight_numbers,
        "dangerous_goods": dg,
        "lmc_event": lmc,
        "leg_status": [{"flight_number": fn, "status": "pending"} for fn in flight_numbers],
    })


# ---------------------------------------------------------------------
# PIREP log - persisted to Postgres (db.py), not local disk. Render's
# free-tier disk is ephemeral and doesn't survive a redeploy, so if
# DATABASE_URL isn't configured, these routes degrade explicitly (502)
# rather than silently writing somewhere that will just lose data again.
# ---------------------------------------------------------------------

if db.db_available():
    try:
        db.init_db()
    except Exception as exc:
        print(f"[pireps] DATABASE_URL is set but init failed: {exc}")

if auth.admin_available():
    try:
        auth.ensure_avatar_bucket()
    except Exception as exc:
        print(f"[storage] Could not ensure avatar bucket exists: {exc}")


def _require_db():
    if not db.db_available():
        return jsonify({"error": "No database configured (DATABASE_URL is unset) - the PIREP log is unavailable."}), 502
    return None


@app.route("/pireps", methods=["GET"])
def list_pireps():
    unavailable = _require_db()
    if unavailable:
        return unavailable
    return jsonify(db.list_pireps(current_user_id()))


@app.route("/pireps", methods=["POST"])
def create_pirep():
    unavailable = _require_db()
    if unavailable:
        return unavailable
    payload = request.get_json(silent=True) or {}
    record = db.create_pirep(payload, current_user_id())
    return jsonify(record), 201


@app.route("/pireps/<pirep_id>", methods=["DELETE"])
def delete_pirep(pirep_id):
    unavailable = _require_db()
    if unavailable:
        return unavailable
    if not db.delete_pirep(pirep_id, current_user_id()):
        return jsonify({"error": "PIREP not found."}), 404
    return jsonify({"deleted": pirep_id})


ACTIVE_FLIGHT_MAX_BYTES = 1_000_000


@app.route("/active-flight", methods=["GET"])
def get_active_flight():
    """The flight this account is currently flying, so it can be picked up
    on any device. The browser keeps a working copy; this is the shared one."""
    unavailable = _require_db()
    if unavailable:
        return unavailable
    return jsonify(db.get_active_flight(current_user_id()))


@app.route("/active-flight", methods=["PUT"])
def put_active_flight():
    """Body: {"flight": <object or null>, "base_rev": <rev the device last
    saw>}. A stale base_rev means another device saved in the meantime:
    409 with that newer copy, which the client adopts."""
    unavailable = _require_db()
    if unavailable:
        return unavailable
    if (request.content_length or 0) > ACTIVE_FLIGHT_MAX_BYTES:
        return jsonify({"error": "Active flight is too large."}), 413
    payload = request.get_json(silent=True) or {}
    flight = payload.get("flight")
    base_rev = payload.get("base_rev")
    if (flight is not None and not isinstance(flight, dict)) or not isinstance(base_rev, int) or base_rev < 0:
        return jsonify({"error": "Expected {flight: object|null, base_rev: int}."}), 400
    saved, current = db.save_active_flight(current_user_id(), flight, base_rev)
    return jsonify(current), (200 if saved else 409)


# ---------------------------------------------------------------------
# Pilot profile + generation settings (delay/LMC/MEL enable and
# per-item toggles), and flying stats derived from the PIREP log.
# ---------------------------------------------------------------------

@app.route("/generation-options")
def generation_options():
    """The full delay/LMC/MEL datasets, trimmed to what the settings
    page needs to render a labeled checkbox per item. MEL scenarios are
    combined across every active carrier's fleet (ids are namespaced by
    fleet - see load_carrier - so a 737 item and an A320 item never
    collide), with a "fleet" label per item so the settings page can
    group them instead of showing one flat 50-item list."""
    # Carriers flying the same family (easyJet and Wizz Air: A320) share
    # one item list, so each item is listed once.
    def unique(items):
        return list({i["id"]: i for i in items}.values())
    all_mels = unique([m for code in ACTIVE_CARRIER_CODES for m in CARRIERS[code]["mels"]])
    all_cdls = unique([c for code in ACTIVE_CARRIER_CODES for c in CARRIERS[code]["cdls"]])
    # Everything the settings tables show per item (effect, actions), plus
    # each category's realistic chance for the probability sliders.
    _, delay_chance = generator.delay_odds([d for d in delay_codes if d["weight"] > 0], date.today().month)
    mel_keys = ("id", "system", "description", "fleet", "ata", "mel_category", "interval_days", "installed", "required",
                "maintenance", "operations", "procedures", "effects", "dispatch_consequence", "component_options", "source",
                "name", "plain", "steps")
    return jsonify({
        "delay": [{"code": d["iata_code"], "description": d["description"], "plain": d.get("plain"),
                   "duration_range_minutes": d["duration_range_minutes"],
                   "weather_gated": d["iata_code"] in generator.WEATHER_GATED_CODES, "atfm": d["iata_code"] in ops.ATFM_CODES}
                  for d in delay_codes],
        "lmc": [{"id": l["id"], "description": l["description"], "type": l["type"], "delta_range": l["delta_range"],
                 "unit": l["unit"], "needs_full_flight": l.get("needs_full_flight", False),
                 "needs_free_seats": l.get("needs_free_seats", False)} for l in lmc_events],
        "mel": [{k: m.get(k) for k in mel_keys} for m in all_mels if m.get("weight", 1) > 0],
        "cdl": [{"id": c["id"], "part": c["part"], "description": c["description"], "fleet": c["fleet"], "ata": c.get("ata"),
                 "effects": c.get("effects"), "note": c.get("note"), "name": c.get("name"), "plain": c.get("plain"),
                 "steps": c.get("steps")} for c in all_cdls],
        "default_probability": {
            "delay": round(delay_chance * 100), "lmc": round(generator.LMC_PROBABILITY * 100),
            "mel": techlog.mel_default_percent(), "cdl": round(techlog.CDL_PROBABILITY * 100),
        },
    })


@app.route("/settings", methods=["GET"])
def get_settings_route():
    return jsonify(get_settings_safe())


_IDENTITY_PROFILE_FIELDS = ("username", "photo_url", "onboarding_complete")


@app.route("/settings", methods=["POST"])
def save_settings_route():
    unavailable = _require_db()
    if unavailable:
        return unavailable
    payload = request.get_json(silent=True) or {}
    # Username/photo/onboarding-state are managed by /onboarding (and,
    # eventually, real account-settings editing for username/photo) -
    # never by this general settings save, even if a client sends them.
    existing_profile = get_settings_safe()["profile"]
    incoming_profile = dict(payload.get("profile") or {})
    for field in _IDENTITY_PROFILE_FIELDS:
        incoming_profile[field] = existing_profile.get(field, db.DEFAULT_SETTINGS["profile"][field])
    if "aircraft_owned" in incoming_profile:
        all_fleet_types = {t for code in ACTIVE_CARRIER_CODES for t in CARRIERS[code]["fleet_by_type"]}
        incoming_profile["aircraft_owned"] = [
            t for t in (incoming_profile["aircraft_owned"] or []) if t in all_fleet_types
        ]
    payload["profile"] = incoming_profile
    # Probability sliders: a whole percentage 0-100, or None (realistic)
    for category in (payload.get("generation") or {}).values():
        if isinstance(category, dict) and "probability" in category:
            try:
                value = category["probability"]
                category["probability"] = None if value is None else max(0, min(100, int(round(float(value)))))
            except (TypeError, ValueError):
                category["probability"] = None
    return jsonify(db.save_settings(payload, current_user_id()))


@app.route("/account/photo", methods=["POST"])
def account_photo_route():
    unavailable = _require_db()
    if unavailable:
        return unavailable
    if not auth.admin_available():
        return jsonify({"error": "Photo storage is not configured (SUPABASE_SERVICE_ROLE_KEY unset)."}), 502
    photo_url, error = _handle_photo_upload(request.files.get("photo"))
    if error:
        return jsonify({"error": error}), 400
    if not photo_url:
        return jsonify({"error": "No photo provided."}), 400
    existing = get_settings_safe()
    profile = dict(existing["profile"])
    profile["photo_url"] = photo_url
    saved = db.save_settings({"profile": profile, "generation": existing["generation"]}, current_user_id())
    return jsonify(saved)


@app.route("/account/delete", methods=["POST"])
def account_delete_route():
    """Requires the account's current password as a second factor,
    verified via a real sign-in call - not just "are you logged in",
    since the pilot could be leaving a session open on a shared
    machine. Deletes the Supabase Auth user (which also revokes every
    session) and this app's own rows for them (PIREPs, settings)."""
    user = current_user()
    if not user:
        return jsonify({"error": "Not signed in."}), 401
    if not auth.admin_available():
        return jsonify({"error": "Account deletion is not configured (SUPABASE_SERVICE_ROLE_KEY unset)."}), 502
    password = request.form.get("password") or ""
    if not password:
        return jsonify({"error": "Enter your password to confirm."}), 400
    try:
        auth.sign_in(user["email"], password)
    except auth.AuthError:
        return jsonify({"error": "Incorrect password."}), 400
    try:
        auth.admin_delete_user(user["id"])
    except auth.AuthError as exc:
        return jsonify({"error": exc.message}), 502
    if db.db_available():
        try:
            db.delete_user_data(user["id"])
        except Exception:
            pass  # the auth account is already gone; don't block on cleanup
    session.clear()
    return jsonify({"ok": True})


@app.route("/stats/flights")
def stats_flights_route():
    """The account's whole logbook as compact rows (see flightstats) - the
    Stats page filters and aggregates them in the browser."""
    empty = {"flights": [], "airports": {}, "base": None}
    if not db.db_available():
        return jsonify(empty)
    try:
        records = db.list_pireps(current_user_id())
    except Exception:
        return jsonify(empty)
    lookup = lambda icao: airports_by_icao.get(icao) or airports_world_by_icao.get(icao)
    result = flightstats.logbook(records, lookup)
    # The pilot's base, so the profile map can show it before any flight there
    base = get_settings_safe()["profile"].get("preferred_base")
    info = lookup(base) if base else None
    result["base"] = {"icao": base, "name": info.get("name"), "lat": info.get("lat"), "lon": info.get("lon")} if info else None
    return jsonify(result)


@app.route("/simbrief/redirect-url")
def simbrief_redirect_url():
    """
    Builds a SimBrief "dispatch redirect" URL - a plain link to SimBrief's
    own dispatch form, pre-filled via query string. Opening it just shows
    the pilot a pre-filled form on simbrief.com; they still press Generate
    there themselves. No API key involved.

    Airline and aircraft type are preselected from the route's own
    operating carrier (fleet_entry_for/carrier_for) - a Ryanair leg gets
    RYR/B738, an easyJet leg gets its real operating subsidiary and
    whichever A320-family type the route specifies (falling back to a
    default type when it doesn't - see fleet_entry_for's docstring).
    Registration is deliberately left unset: SimBrief fills it
    from whichever airframe profile the pilot has set as default, and
    the actual registration used is read back from the OFP once fetched
    (see /simbrief/ofp's `registration` field) rather than preselected
    here - this app doesn't have access to a pilot's saved SimBrief
    airframes (that needs SimBrief's gated, approval-only API), and even
    if it did, nothing in a saved airframe profile says which airline it
    belongs to. acdata (individual payload/performance overrides) and
    cargo are still left out: cargo's expected units aren't confirmed
    from public docs, and acdata is airframe minutiae that belongs to
    the pilot's own SimBrief fleet entry, not this app.
    deph/depm/taxiout/taxiin ARE passed - taxi time feeds directly into
    SimBrief's fuel planning, so leaving it out was producing a fuel
    figure the taxi time on our own SOBT/STOT/SLDT/SIBT table didn't
    match.
    """
    flights_raw = request.args.get("flights", default="", type=str)
    flight_numbers = [f.strip() for f in flights_raw.split(",") if f.strip()]
    if not flight_numbers:
        return jsonify({"error": "No flights specified."}), 400

    fn = flight_numbers[0]
    route_leg = routes_by_flight_number.get(fn)
    if route_leg is None:
        return jsonify({"error": f"Unknown flight number: {fn}"}), 400

    civalue = request.args.get("civalue", type=int)
    pax = request.args.get("pax", type=int)
    taxi_out = request.args.get("taxi_out_minutes", type=int)
    # The aircraft type the pilot is actually shown (assigned at /select,
    # possibly changed via /select/reassign-aircraft) - passed through so
    # this never independently re-rolls a DIFFERENT random type than the
    # one already on screen. Only a preview call (before /select ran)
    # goes without one.
    aircraft_type = request.args.get("aircraft_type", default="", type=str).strip() or None
    if civalue is None or pax is None or taxi_out is None or aircraft_type is None:
        # Only hit when the caller doesn't already have confirmed leg
        # values (e.g. a preview, before CONFIRM has committed a
        # cost_index/pax_count/taxi_out_minutes). An already-confirmed
        # active leg's frontend call always supplies all of these, so
        # this never re-rolls numbers the pilot has already seen and
        # confirmed.
        settings = get_settings_safe()
        owned_types = owned_aircraft_for(settings, route_leg["carrier"])
        fleet_entry = fleet_entry_for(route_leg, owned_types)
        conditions = generate_leg_conditions(
            duration_minutes=route_leg["duration_minutes"],
            seat_capacity=fleet_entry["seats"],
        )
        if civalue is None:
            civalue = conditions["cost_index"]
        if pax is None:
            pax = conditions["pax_count"]
        if taxi_out is None:
            taxi_out = conditions["taxi_out_minutes"]
        if aircraft_type is None:
            aircraft_type = fleet_entry["type"]

    fleet = carrier_for(route_leg)["fleet_by_type"]
    simbrief_type = fleet[aircraft_type]["simbrief_type"] if aircraft_type in fleet \
        else next(iter(fleet.values()))["simbrief_type"]
    params = {
        "orig": route_leg["departure_icao"],
        "dest": route_leg["arrival_icao"],
        "airline": carrier_for(route_leg)["icao"],
        "fltnum": flight_number_digits(fn),
        "date": simbrief_date_str(),
        "civalue": civalue,
        "pax": pax,
        "taxiout": taxi_out,
        "taxiin": TAXI_IN_MINUTES,
        "type": simbrief_type,
    }

    # SimBrief gets the EXPECTED (delay-adjusted) off-block time, not the
    # raw scheduled one - a flight scheduled off-block at 12:05Z with a
    # 15min delay allocation should feed SimBrief 12:20Z. The frontend
    # passes through the exact EOBT string /select already computed and
    # showed the pilot (leg.eobt, e.g. "12:20Z") - that figure also
    # accounts for the minimum-turnaround shift a second rotation leg
    # can get, which this route has no way to recompute on its own (it
    # doesn't know about the previous leg). Only if that's missing
    # (e.g. a pre-CONFIRM preview) does it fall back to recomputing SOBT
    # + delay_minutes here.
    eobt_raw = request.args.get("eobt", default="", type=str).strip()
    eobt_match = re.match(r"^(\d{2}):(\d{2})", eobt_raw)
    if eobt_match:
        params["deph"], params["depm"] = eobt_match.group(1), eobt_match.group(2)
    else:
        schedule = resolve_leg_schedule(route_leg, tz_by_icao, date.today(), taxi_out)
        sobt_dt = schedule["_sobt_dt"]
        if sobt_dt is not None:
            delay_minutes = request.args.get("delay_minutes", default=0, type=int)
            expected_dt = sobt_dt + timedelta(minutes=delay_minutes)
            params["deph"] = expected_dt.strftime("%H")
            params["depm"] = expected_dt.strftime("%M")

    # The confirmed callsign is a one-time random roll from /confirm, not
    # reproducible here - the frontend must pass through the exact one
    # already shown/confirmed for this leg. Left unset, SimBrief falls
    # back to airline+fltnum, which is a fine default too.
    callsign = request.args.get("callsign", default="", type=str).strip()
    if callsign:
        params["callsign"] = callsign

    static_id = request.args.get("static_id", default="", type=str).strip()
    if static_id:
        params["static_id"] = static_id

    # Technical status (see techlog.leg_tech): an MEL flight-level cap goes
    # in as the cruise level, MEL/CDL fuel as extra fuel in minutes (the
    # one unit that doesn't depend on the pilot's kg/lb setting), and the
    # items themselves into the OFP remarks.
    max_fl = request.args.get("max_fl", type=int)
    if max_fl and 100 <= max_fl <= 450:
        params["fl"] = f"FL{max_fl}"
    extra_fuel_min = request.args.get("extra_fuel_min", type=int)
    if extra_fuel_min and 0 < extra_fuel_min <= 120:
        params["addedfuel"] = extra_fuel_min
        params["addedfuel_units"] = "min"
    remarks = re.sub(r"[^A-Za-z0-9 /.+-]", "", request.args.get("remarks", default="", type=str))[:180].strip()
    if remarks:
        params["manualrmk"] = remarks

    return jsonify({"url": "https://www.simbrief.com/system/dispatch.php?" + urlencode(params)})


def ofp_route_fixes(navlog):
    """The OFP's route as a compact list of fixes for drawing it on a map:
    [{"ident", "type", "lat", "lon", "via"}], in flight order. SimBrief's
    navlog.fix is a list (a single fix can arrive as a bare object), with
    every value a string; fixes without usable coordinates are skipped.
    TOC/TOD come through as their own entries (ident "TOC"/"TOD"), and
    SID/STAR fixes only when the pilot's SimBrief settings include
    procedures. The origin airport isn't in the list; the destination
    usually is, as the last entry."""
    # Accept the navlog as {"fix": [...]}, {"fix": {...}} or a bare list;
    # SimBrief's older json=1 format also sends empty values as {}.
    fixes = navlog.get("fix") if isinstance(navlog, dict) else navlog
    if isinstance(fixes, dict):
        fixes = [fixes]
    if not isinstance(fixes, list):
        return []

    def text(value, limit):
        return str(value)[:limit] if isinstance(value, (str, int, float)) else ""

    out = []
    for f in fixes:
        if not isinstance(f, dict):
            continue
        try:
            lat = float(f.get("pos_lat", f.get("lat")))
            lon = float(f.get("pos_long", f.get("lon")))
        except (TypeError, ValueError):
            continue
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            continue
        out.append({
            "ident": text(f.get("ident"), 12),
            "type": text(f.get("type"), 8),
            "lat": round(lat, 4),
            "lon": round(lon, 4),
            "via": text(f.get("via_airway"), 12),
        })
        if len(out) >= 400:   # a sanity cap - real OFPs have far fewer
            break
    return out


@app.route("/simbrief/ofp")
def simbrief_ofp():
    """
    Reads back the pilot's most recently generated OFP from SimBrief's
    public fetch-back endpoint (json=v2, no API key). This always returns
    whichever OFP is most recent for that SimBrief account - matching
    static_id (echoed back inside the OFP's params block) against the one
    this app sent to dispatch.php is how the frontend confirms it got the
    OFP it just asked for, not a stale one from an earlier session.
    """
    username = request.args.get("username", default="", type=str).strip()
    if not username:
        return jsonify({"error": "SimBrief username is required."}), 400

    try:
        resp = requests.get(
            "https://www.simbrief.com/api/xml.fetcher.php",
            params={"username": username, "json": "v2"},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception:
        return jsonify({"error": "Could not reach SimBrief, or no OFP is on file for this username."}), 502

    if not isinstance(data, dict):
        return jsonify({"error": "Unexpected response from SimBrief."}), 502
    if str(data.get("fetch", {}).get("status", "")).lower().startswith("error"):
        return jsonify({"error": "SimBrief reported an error for this username - generate an OFP first."}), 502

    origin = data.get("origin", {})
    destination = data.get("destination", {})
    general = data.get("general", {})
    aircraft = data.get("aircraft", {})
    params_block = data.get("params", {})
    fuel = data.get("fuel", {})
    times = data.get("times", {})
    weights = data.get("weights", {})
    # Unit for every weight/fuel figure below - SimBrief reports these in
    # whichever unit the pilot's own profile is set to (not something
    # this app controls), so it's surfaced rather than assumed.
    weight_unit = general.get("units") or params_block.get("units") or "KG"

    return jsonify({
        "static_id": params_block.get("static_id", ""),
        "origin_icao": origin.get("icao_code", ""),
        "destination_icao": destination.get("icao_code", ""),
        "callsign": f"{general.get('icao_airline', '')}{general.get('flight_number', '')}",
        "route": general.get("route", ""),
        "cost_index": general.get("costindex", ""),
        "initial_altitude_ft": general.get("initial_altitude", ""),
        "registration": aircraft.get("reg", ""),
        "icao_type": aircraft.get("icaocode", ""),
        "weight_unit": weight_unit,
        "block_fuel": fuel.get("plan_ramp", ""),
        "est_time_enroute_sec": times.get("est_time_enroute", ""),
        "est_zfw": weights.get("est_zfw", ""),
        "est_tow": weights.get("est_tow", ""),
        # EFOB = Estimated Fuel On Board at block-off, i.e. block/ramp
        # fuel - the planning-stage figure shown alongside EZFW/ETOW.
        # Not to be confused with AFAD (Actual Fuel At Destination),
        # which the pilot enters themselves at PIREP time.
        "efob": fuel.get("plan_ramp", ""),
        # Planned trip burn and fuel at destination, kept so the Stats page
        # can compare them with the pilot's actual fuel at destination (AFAD).
        "plan_trip_fuel": fuel.get("est_burn", ""),
        "plan_landing_fuel": fuel.get("plan_landing", ""),
        "epax": weights.get("pax_count", ""),
        "navlog": ofp_route_fixes(data.get("navlog")),
    })


if __name__ == "__main__":
    app.run(debug=True, port=5000)
