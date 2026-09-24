"""
VirtualDispatch - Flask app.
"""

from flask import Flask, render_template, request, jsonify, session, redirect, url_for
import json
import os
import re
import secrets
import requests
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urlencode
import db
import auth
import blog
from generator import (
    resolve_airport, find_round_trip_pairs, find_itineraries, flight_number_digits,
    generate_leg_conditions, roll_delay, roll_mel, generate_callsign, generate_loadsheet_extras,
    assign_aircraft_type, pax_and_cargo
)
from timeutils import (
    resolve_leg_times, resolve_leg_schedule, format_zulu, turnaround_minutes,
    turnaround_shift, simbrief_date_str, TAXI_IN_MINUTES
)

app = Flask(__name__)

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
PUBLIC_ENDPOINTS = {"login", "signup", "auth_callback", "auth_session",
                    "resend_verification_route", "request_password_reset_route", "static",
                    "index", "blog_index", "blog_post", "legal_privacy", "legal_terms", "legal_cookies"}


@app.context_processor
def inject_template_globals():
    today = date.today()
    return {
        "current_year": today.year,
        "legal_updated": today.strftime("%B %-d, %Y") if os.name != "nt" else today.strftime("%B %d, %Y"),
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


@app.route("/signup", methods=["GET", "POST"])
def signup():
    error = None
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
    return jsonify({"ok": True})


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
    carrier_mels = []
    seen_mel_ids = set()
    for mel_group in mel_groups:
        mel_path = os.path.join("aircraft", mel_group, "mel_list.json")
        if not os.path.exists(os.path.join(DATA_DIR, mel_path)):
            continue
        for m in load_json(mel_path):
            namespaced_id = m["id"] if mel_group == "738" else f"{mel_group}/{m['id']}"
            if namespaced_id not in seen_mel_ids:
                seen_mel_ids.add(namespaced_id)
                m["id"] = namespaced_id
                m["fleet"] = mel_group
                carrier_mels.append(m)

    return {
        "icao": config["icao"], "iata": config["iata"], "name": config["name"],
        "callsign_prefix": config["callsign_prefix"],
        "fleet_by_type": fleet_by_type,
        "routes": carrier_routes,
        "mels": carrier_mels,
    }


# See load_carrier's docstring for how a new carrier plugs in. Route/MEL
# data downstream is keyed per-route/per-carrier (via each route's own
# "carrier" field) rather than assuming a single global carrier, so
# adding one here is a data change, not a rewrite of /search, /select,
# /confirm etc.
ACTIVE_CARRIER_CODES = ("RYR", "EZY")
CARRIERS = {code: load_carrier(code) for code in ACTIVE_CARRIER_CODES}

routes = [r for code in ACTIVE_CARRIER_CODES for r in CARRIERS[code]["routes"]]
routes_by_flight_number = {r["flight_number"]: r for r in routes}
rt_pairing = find_round_trip_pairs(routes)

delay_codes = load_json("delay_codes.json")
lmc_events = load_json("lmc_events.json")
dangerous_goods = load_json("dangerous_goods.json")
airports = load_json("airports.json")
countries = load_json("countries.json")
# Worldwide airport reference (OurAirports, public domain), separate from
# the route-network `airports` above - used only for the preferred base
# picker and destinations map, never for route search, since a pilot's
# home base can be any real airport, not just one a loaded carrier serves.
airports_world = load_json("airports_world.json")

country_by_icao = {a["icao"]: a["country"] for a in airports}
tz_by_icao = {a["icao"]: a["tz"] for a in airports}
airports_by_icao = {a["icao"]: a for a in airports}
airports_world_by_icao = {a["icao"]: a for a in airports_world}

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


def fetch_weather_batch(icao_list):
    unique = sorted(set(icao_list))
    ids_param = ",".join(unique)
    result = {icao: {"metar": None, "taf": None} for icao in unique}
    try:
        resp = requests.get("https://aviationweather.gov/api/data/metar",
                             params={"ids": ids_param, "format": "json"},
                             headers={"User-Agent": WEATHER_USER_AGENT}, timeout=8)
        resp.raise_for_status()
        for item in resp.json():
            icao = item.get("icaoId")
            if icao in result:
                result[icao]["metar"] = item.get("rawOb")
    except Exception:
        pass
    try:
        resp = requests.get("https://aviationweather.gov/api/data/taf",
                             params={"ids": ids_param, "format": "json"},
                             headers={"User-Agent": WEATHER_USER_AGENT}, timeout=8)
        resp.raise_for_status()
        for item in resp.json():
            icao = item.get("icaoId")
            if icao in result:
                result[icao]["taf"] = item.get("rawTAF")
    except Exception:
        pass
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
                            current_user_email=(user["email"] if user else ""), countries=countries)


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
            "fleet": [{"type": t, "seats": ac["seats"]} for t, ac in CARRIERS[code]["fleet_by_type"].items()],
        }
        for code in ACTIVE_CARRIER_CODES
    ])


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
    itinerary_mels = carrier_for(itinerary[0])["mels"]
    itinerary_mel = roll_mel(itinerary_mels, settings["generation"]["mel"])
    owned_types = owned_aircraft_for(settings, itinerary[0]["carrier"])

    legs_out = []
    leg_times = []
    for route_leg in itinerary:
        fleet_entry = fleet_entry_for(route_leg, owned_types)
        conditions = generate_leg_conditions(
            duration_minutes=route_leg["duration_minutes"],
            seat_capacity=fleet_entry["seats"],
        )
        conditions["aircraft_type"] = fleet_entry["type"]
        conditions["mel"] = itinerary_mel
        conditions["flight_number"] = route_leg["flight_number"]
        conditions["carrier"] = route_leg["carrier"]
        conditions["departure_info"] = airport_info(route_leg["departure_icao"])
        conditions["arrival_info"] = airport_info(route_leg["arrival_icao"])
        dep_weather = weather.get(route_leg["departure_icao"], {"metar": None, "taf": None})
        arr_weather = weather.get(route_leg["arrival_icao"], {"metar": None, "taf": None})
        conditions["weather"] = {"departure": dep_weather, "arrival": arr_weather}
        # Weather-flagged delay codes (departure/destination weather,
        # de-icing) only enter the pool when the leg's own METAR actually
        # supports them - see generator.roll_delay - so a summer 20C
        # departure never rolls "de-icing of aircraft".
        conditions["delay"] = roll_delay(
            delay_codes, settings["generation"]["delay"],
            dep_metar=dep_weather.get("metar"), arr_metar=arr_weather.get("metar"),
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

        # Expected (E-) times = scheduled (S-) times shifted by the leg's
        # own delay, if any - the higher end of the range if it's a
        # range. No delay means expected == scheduled. This is also what
        # /simbrief/redirect-url sends as deph/depm, not the raw SOBT.
        # Same day-suffix anchoring as above: EOBT vs today (it's the
        # same kind of figure as SOBT), ETOT/ELDT/EIBT vs this leg's own
        # SOBT date.
        delay_minutes = max(conditions["delay"]["duration_range_minutes"]) if conditions["delay"] else 0
        conditions["expected_delay_minutes"] = delay_minutes
        if sobt_dt is not None:
            delay_delta = timedelta(minutes=delay_minutes)
            conditions["eobt"] = format_zulu(sobt_dt + delay_delta, today)
            conditions["etot"] = format_zulu(stot_dt + delay_delta, sobt_date)
            conditions["eldt"] = format_zulu(sldt_dt + delay_delta, sobt_date)
            conditions["eibt"] = format_zulu(sibt_dt + delay_delta, sobt_date)
        else:
            conditions["eobt"] = conditions["etot"] = conditions["eldt"] = conditions["eibt"] = None

        leg_times.append((sobt_dt, sibt_dt))
        legs_out.append(conditions)

    for i in range(1, len(legs_out)):
        legs_out[i]["turnaround_minutes"] = turnaround_minutes(leg_times[i - 1][1], leg_times[i][0])

    return jsonify({"itinerary": itinerary, "legs": legs_out})


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
    dg, lmc = generate_loadsheet_extras(dangerous_goods, lmc_events, settings["generation"]["lmc"])

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
    all_mels = [m for code in ACTIVE_CARRIER_CODES for m in CARRIERS[code]["mels"]]
    return jsonify({
        "delay": [{"code": d["iata_code"], "description": d["description"]} for d in delay_codes],
        "lmc": [{"id": l["id"], "description": l["description"]} for l in lmc_events],
        "mel": [{"id": m["id"], "system": m["system"], "description": m["description"], "fleet": m["fleet"]}
                for m in all_mels],
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


@app.route("/stats")
def stats_route():
    empty = {"total_flights": 0, "total_flight_minutes": 0, "airports": [], "monthly": []}
    if not db.db_available():
        return jsonify(empty)
    try:
        result = db.get_stats(current_user_id())
    except Exception:
        return jsonify(empty)
    for entry in result["airports"]:
        info = airports_by_icao.get(entry["icao"]) or airports_world_by_icao.get(entry["icao"], {})
        entry["name"] = info.get("name")
        entry["lat"] = info.get("lat")
        entry["lon"] = info.get("lon")

    base = get_settings_safe()["profile"].get("preferred_base")
    if base and not any(entry["icao"] == base for entry in result["airports"]):
        info = airports_by_icao.get(base) or airports_world_by_icao.get(base)
        if info:
            result["airports"].append({
                "icao": base, "count": 0,
                "name": info.get("name"), "lat": info.get("lat"), "lon": info.get("lon"),
            })
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

    return jsonify({"url": "https://www.simbrief.com/system/dispatch.php?" + urlencode(params)})


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
        "epax": weights.get("pax_count", ""),
    })


if __name__ == "__main__":
    app.run(debug=True, port=5000)
