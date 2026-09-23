"""
Supabase Auth (GoTrue) integration for VirtualDispatch.

This app already runs its Postgres on Supabase, so account signup/login/
email-verification rides on the same project's built-in Auth service
instead of a hand-rolled password+token system - GoTrue issues and
verifies the emails itself, and its REST API is small enough that a
custom SDK dependency isn't worth adding (the app already depends on
`requests`).

Required environment variables (all set in the Supabase project's
Settings -> API page):
  SUPABASE_URL              e.g. https://xxxx.supabase.co
  SUPABASE_ANON_KEY         the project's public "anon" key
  SUPABASE_SERVICE_ROLE_KEY the project's secret "service_role" key -
                             ONLY used server-side, for admin user
                             management (list/ban/delete). Never sent
                             to the browser.

Also required in the Supabase dashboard (Authentication -> ...):
  - Email provider enabled, "Confirm email" turned ON (Providers ->
    Email) so signup actually requires clicking the emailed link before
    sign-in succeeds.
  - Site URL / Redirect URLs (URL Configuration) include this app's
    origin plus "/auth/callback", so the confirmation link's redirect
    lands back on this app instead of being rejected.
  - A custom SMTP sender (Auth -> Emails -> SMTP Settings) configured
    for anything beyond a handful of beta testers - Supabase's built-in
    mailer is rate-limited to a few emails/hour and is not meant for
    real signup volume.
"""

import os
import requests

SUPABASE_URL = (os.environ.get("SUPABASE_URL") or "").rstrip("/")
SUPABASE_ANON_KEY = os.environ.get("SUPABASE_ANON_KEY")
SUPABASE_SERVICE_ROLE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")

# Emails allowed onto the /admin user-management page. Comma-separated,
# case-insensitive. Empty means nobody can reach /admin - admin access
# has to be turned on deliberately, not default-open.
ADMIN_EMAILS = {
    e.strip().lower()
    for e in (os.environ.get("ADMIN_EMAILS") or "").split(",")
    if e.strip()
}

# Cloudflare Turnstile - shown on login/signup. Unset TURNSTILE_SECRET_KEY
# means verification is skipped entirely (matches this app's long-standing
# "missing config = feature off, not broken" pattern for local dev).
TURNSTILE_SITE_KEY = os.environ.get("TURNSTILE_SITE_KEY")
TURNSTILE_SECRET_KEY = os.environ.get("TURNSTILE_SECRET_KEY")

AVATAR_BUCKET = "avatars"

AUTH_TIMEOUT = 10


def auth_available():
    return bool(SUPABASE_URL and SUPABASE_ANON_KEY)


def admin_available():
    return bool(SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY)


def _anon_headers():
    return {"apikey": SUPABASE_ANON_KEY, "Content-Type": "application/json"}


def _service_headers():
    return {
        "apikey": SUPABASE_SERVICE_ROLE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
        "Content-Type": "application/json",
    }


class AuthError(Exception):
    def __init__(self, message):
        super().__init__(message)
        self.message = message


def _raise_for_gotrue_error(resp):
    try:
        body = resp.json()
    except ValueError:
        body = {}
    message = body.get("error_description") or body.get("msg") or body.get("error") or f"Auth request failed ({resp.status_code})."
    raise AuthError(message)


def sign_up(email, password, redirect_to=None):
    """Creates the account and triggers Supabase's verification email.
    Returns the GoTrue user object. The account can't sign in until the
    emailed link is clicked (assuming "Confirm email" is on)."""
    params = {"redirect_to": redirect_to} if redirect_to else {}
    resp = requests.post(
        f"{SUPABASE_URL}/auth/v1/signup",
        headers=_anon_headers(),
        params=params,
        json={"email": email, "password": password},
        timeout=AUTH_TIMEOUT,
    )
    if resp.status_code >= 400:
        _raise_for_gotrue_error(resp)
    return resp.json()


def sign_in(email, password):
    """Returns {"access_token", "refresh_token", "expires_in", "user"}.
    Raises AuthError (with GoTrue's own message, e.g. "Email not
    confirmed") on bad credentials or an unverified account."""
    resp = requests.post(
        f"{SUPABASE_URL}/auth/v1/token",
        headers=_anon_headers(),
        params={"grant_type": "password"},
        json={"email": email, "password": password},
        timeout=AUTH_TIMEOUT,
    )
    if resp.status_code >= 400:
        _raise_for_gotrue_error(resp)
    return resp.json()


def refresh_session(refresh_token):
    resp = requests.post(
        f"{SUPABASE_URL}/auth/v1/token",
        headers=_anon_headers(),
        params={"grant_type": "refresh_token"},
        json={"refresh_token": refresh_token},
        timeout=AUTH_TIMEOUT,
    )
    if resp.status_code >= 400:
        _raise_for_gotrue_error(resp)
    return resp.json()


def get_user(access_token):
    """Validates an access token against Supabase directly (rather than
    decoding the JWT locally) - one extra network round trip, but no JWT
    secret to manage and it can't go stale relative to what Supabase
    itself considers valid (e.g. a since-banned or deleted account)."""
    resp = requests.get(
        f"{SUPABASE_URL}/auth/v1/user",
        headers={**_anon_headers(), "Authorization": f"Bearer {access_token}"},
        timeout=AUTH_TIMEOUT,
    )
    if resp.status_code >= 400:
        return None
    return resp.json()


def request_password_reset(email, redirect_to=None):
    params = {"redirect_to": redirect_to} if redirect_to else {}
    resp = requests.post(
        f"{SUPABASE_URL}/auth/v1/recover",
        headers=_anon_headers(),
        params=params,
        json={"email": email},
        timeout=AUTH_TIMEOUT,
    )
    if resp.status_code >= 400:
        _raise_for_gotrue_error(resp)


def resend_verification(email, redirect_to=None):
    params = {"redirect_to": redirect_to} if redirect_to else {}
    resp = requests.post(
        f"{SUPABASE_URL}/auth/v1/resend",
        headers=_anon_headers(),
        params=params,
        json={"type": "signup", "email": email},
        timeout=AUTH_TIMEOUT,
    )
    if resp.status_code >= 400:
        _raise_for_gotrue_error(resp)


# ---------------------------------------------------------------------
# Admin user management (service-role key - server-side only, never
# exposed to the browser)
# ---------------------------------------------------------------------

def admin_list_users(page=1, per_page=200):
    resp = requests.get(
        f"{SUPABASE_URL}/auth/v1/admin/users",
        headers=_service_headers(),
        params={"page": page, "per_page": per_page},
        timeout=AUTH_TIMEOUT,
    )
    if resp.status_code >= 400:
        _raise_for_gotrue_error(resp)
    body = resp.json()
    return body.get("users", body if isinstance(body, list) else [])


def admin_set_banned(user_id, banned):
    """banned=True locks the account out indefinitely (a very long ban
    duration - GoTrue has no simple boolean flag); banned=False lifts it
    immediately ("none")."""
    resp = requests.put(
        f"{SUPABASE_URL}/auth/v1/admin/users/{user_id}",
        headers=_service_headers(),
        json={"ban_duration": "876000h" if banned else "none"},
        timeout=AUTH_TIMEOUT,
    )
    if resp.status_code >= 400:
        _raise_for_gotrue_error(resp)
    return resp.json()


def admin_delete_user(user_id):
    resp = requests.delete(
        f"{SUPABASE_URL}/auth/v1/admin/users/{user_id}",
        headers=_service_headers(),
        timeout=AUTH_TIMEOUT,
    )
    if resp.status_code >= 400 and resp.status_code != 404:
        _raise_for_gotrue_error(resp)


# ---------------------------------------------------------------------
# Cloudflare Turnstile (captcha) - server-side verification only; the
# widget itself is rendered client-side against TURNSTILE_SITE_KEY.
# ---------------------------------------------------------------------

def turnstile_available():
    return bool(TURNSTILE_SECRET_KEY)


def verify_turnstile(token, remote_ip=None):
    """True if the token is valid, or if Turnstile isn't configured
    (local dev without the secret set - matches every other optional
    integration in this file)."""
    if not TURNSTILE_SECRET_KEY:
        return True
    if not token:
        return False
    payload = {"secret": TURNSTILE_SECRET_KEY, "response": token}
    if remote_ip:
        payload["remoteip"] = remote_ip
    try:
        resp = requests.post(
            "https://challenges.cloudflare.com/turnstile/v0/siteverify",
            data=payload,
            timeout=AUTH_TIMEOUT,
        )
        return bool(resp.json().get("success"))
    except requests.RequestException:
        return False


# ---------------------------------------------------------------------
# Supabase Storage (profile photos) - service-role key, server-side
# uploads only. The bucket is created automatically at app startup
# (see ensure_avatar_bucket) rather than requiring a manual dashboard
# step, the same way init_db() creates its own tables.
# ---------------------------------------------------------------------

def ensure_avatar_bucket():
    """Idempotent: creates the public avatar-photos bucket if it
    doesn't already exist. Safe to call on every app startup."""
    resp = requests.post(
        f"{SUPABASE_URL}/storage/v1/bucket",
        headers=_service_headers(),
        json={"id": AVATAR_BUCKET, "name": AVATAR_BUCKET, "public": True,
              "file_size_limit": 2 * 1024 * 1024,
              "allowed_mime_types": ["image/jpeg", "image/png", "image/webp"]},
        timeout=AUTH_TIMEOUT,
    )
    # 400/409 here just means the bucket already exists - not an error.
    if resp.status_code >= 400 and "already exists" not in resp.text.lower():
        raise AuthError(f"Could not create avatar storage bucket: {resp.text}")


def upload_avatar(path, data, content_type):
    """Uploads (overwriting any existing file at the same path) and
    returns the public URL. path is typically the user's id plus an
    extension, e.g. '<uuid>.jpg'."""
    resp = requests.post(
        f"{SUPABASE_URL}/storage/v1/object/{AVATAR_BUCKET}/{path}",
        headers={
            "apikey": SUPABASE_SERVICE_ROLE_KEY,
            "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
            "Content-Type": content_type,
            "x-upsert": "true",
        },
        data=data,
        timeout=AUTH_TIMEOUT,
    )
    if resp.status_code >= 400:
        raise AuthError(f"Photo upload failed: {resp.text}")
    return f"{SUPABASE_URL}/storage/v1/object/public/{AVATAR_BUCKET}/{path}"
