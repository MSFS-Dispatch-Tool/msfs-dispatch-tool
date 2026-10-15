"""Error monitoring: server crashes and browser errors, reported to Sentry.

Off unless SENTRY_DSN is set, so local runs and tests send nothing. What
reaches Sentry is kept to what's needed to find a bug: the error, its
stack, the URL path and the browser, plus the account's id (never its
email). Request bodies, query strings, cookies, headers other than the
browser's user agent, and the values of variables in the stack are never
sent: bodies and variables can hold Hoppie logon codes or Navigraph
tokens, query strings Navigraph sign-in codes.

Browser errors don't go to Sentry directly (no third-party script on our
pages): the page posts them to /client-errors, and this server forwards
them. Without SENTRY_DSN they're written to the server log instead.

Environment:
    SENTRY_DSN           the project's DSN, from sentry.io
    SENTRY_ENVIRONMENT   optional; "production" on Render, else "development"
"""
import logging
import os

import sentry_sdk

DSN = os.environ.get("SENTRY_DSN") or ""
log = logging.getLogger("vd.monitoring")


def _scrub(event, hint):
    request = event.get("request")
    if isinstance(request, dict):
        for key in ("data", "query_string", "cookies", "env"):
            request.pop(key, None)
        agent = {k: v for k, v in (request.get("headers") or {}).items() if k.lower() == "user-agent"}
        request["headers"] = agent
        if isinstance(request.get("url"), str):
            request["url"] = request["url"].split("?", 1)[0]
    # This server's own calls (SimBrief, Hoppie, Navigraph), noted on the way
    for crumb in (event.get("breadcrumbs") or {}).get("values") or []:
        data = crumb.get("data")
        if isinstance(data, dict):
            data.pop("http.query", None)
            data.pop("http.fragment", None)
            if isinstance(data.get("url"), str):
                data["url"] = data["url"].split("?", 1)[0]
    return event


def init(**options):
    """Turns monitoring on when SENTRY_DSN is set (tests pass dsn and a
    transport directly). Returns whether it's on."""
    dsn = options.pop("dsn", DSN)
    if not dsn:
        return False
    sentry_sdk.init(
        dsn=dsn,
        environment=os.environ.get("SENTRY_ENVIRONMENT") or ("production" if os.environ.get("RENDER") else "development"),
        release=os.environ.get("RENDER_GIT_COMMIT") or None,
        send_default_pii=False,
        max_request_body_size="never",
        include_local_variables=False,
        traces_sample_rate=0,
        before_send=_scrub,
        **options,
    )
    return True


def enabled():
    client = sentry_sdk.get_client()
    return client.is_active() and bool(getattr(client, "dsn", None))


def set_user(user_id):
    if user_id:
        sentry_sdk.set_user({"id": user_id})


def browser_error(error, user_id, user_agent):
    """One error from a page's own scripts (see /client-errors)."""
    message = error["message"]
    if not enabled():
        log.warning("browser error on %s: %s at %s", error["page"], message, error["source"])
        return
    with sentry_sdk.new_scope() as scope:
        scope.set_tag("origin", "browser")
        scope.set_tag("page", error["page"])
        if user_id:
            scope.set_user({"id": user_id})
        scope.set_context("browser_error", {"source": error["source"], "stack": error["stack"], "user_agent": user_agent})
        # Grouped by message and where it happened, not by the time it's sent
        scope.fingerprint = ["browser", message, error["source"].rsplit(":", 1)[0]]
        sentry_sdk.capture_message(f"Browser: {message}", level="error")
