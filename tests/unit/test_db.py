"""Database layer against a real Postgres. Skipped unless TEST_DATABASE_URL
points at a throwaway database (the tests write rows for made-up users)."""
import os
import uuid

import pytest

import db

URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not URL, reason="TEST_DATABASE_URL is not set")


@pytest.fixture(autouse=True)
def database(monkeypatch):
    monkeypatch.setattr(db, "DATABASE_URL", URL)
    db.init_db()
    user = "test-" + uuid.uuid4().hex
    yield user
    db.delete_user_data(user)


def test_every_app_table_has_row_security_and_the_owner_still_reads_it(database):
    db.enable_row_security()
    with db.get_connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT relname FROM pg_class WHERE relname = ANY(%s) AND relrowsecurity", (list(db.APP_TABLES),))
        assert sorted(r[0] for r in cur.fetchall()) == sorted(db.APP_TABLES)
    db.save_settings({"profile": {"username": "pilot"}, "generation": {}}, database)
    assert db.get_settings(database)["profile"]["username"] == "pilot"
    # A role granted access to the table, like Supabase's public API roles, sees no rows
    with db.get_connection() as conn, conn.cursor() as cur:
        cur.execute("DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'vd_api_probe') "
                    "THEN CREATE ROLE vd_api_probe; END IF; END $$")
        cur.execute("GRANT SELECT ON app_settings TO vd_api_probe")
        cur.execute("SET LOCAL ROLE vd_api_probe")
        cur.execute("SELECT count(*) FROM app_settings")
        assert cur.fetchone()[0] == 0
        conn.rollback()


def test_allowed_pilots_and_the_waitlist(database):
    email = database + "@example.com"
    assert db.join_waitlist(email.upper(), "consent text") and not db.join_waitlist(email, "other text")
    assert next(w for w in db.list_waitlist() if w["email"] == email)["consent"] == "consent text"
    db.add_allowed_pilot(" " + email.upper(), "tester", "boss@example.com")
    assert email in db.allowed_pilot_emails()
    assert all(w["email"] != email for w in db.list_waitlist())            # off the waitlist once allowed
    db.add_allowed_pilot(email, "renamed", "boss@example.com")
    assert next(p for p in db.list_allowed_pilots() if p["email"] == email)["note"] == "renamed"
    assert db.remove_allowed_pilot(email) and not db.remove_allowed_pilot(email)
    db.join_waitlist(email, "consent text")
    assert db.remove_from_waitlist(email) and not db.remove_from_waitlist(email)


def test_settings_merge_with_defaults(database):
    assert db.get_settings(database) == db.DEFAULT_SETTINGS
    saved = db.save_settings({"profile": {"username": "pilot"}, "generation": {"mel": {"probability": 40}}}, database)
    assert saved["profile"]["username"] == "pilot" and saved["profile"]["aircraft_owned"] == []
    assert saved["generation"]["mel"]["probability"] == 40 and saved["generation"]["mel"]["enabled"] is True
    assert saved["generation"]["delay"] == db.DEFAULT_SETTINGS["generation"]["delay"]


def test_acars_logon_is_kept_apart_from_settings(database):
    db.save_acars({"logon": "ABC123xyz"}, database)                         # before any other settings
    assert db.get_acars(database) == {"logon": "ABC123xyz"}
    db.save_settings({"profile": {"username": "pilot"}, "generation": {}}, database)
    assert db.get_acars(database) == {"logon": "ABC123xyz"}                  # a profile save keeps it
    assert "acars" not in db.get_settings(database)                          # and /settings never shows it
    db.save_acars({}, database)
    assert db.get_acars(database) == {} and db.get_settings(database)["profile"]["username"] == "pilot"


def test_plain_logon_codes_are_encrypted_once(database):
    import acars
    db.save_acars({"logon": "PLAIN123abc"}, database)
    assert db.seal_plain_acars_logons(lambda code: "sealed:" + code, acars.LOGON_RE.pattern) >= 1
    assert db.get_acars(database) == {"logon": "sealed:PLAIN123abc"}
    db.seal_plain_acars_logons(lambda code: "sealed:" + code, acars.LOGON_RE.pattern)
    assert db.get_acars(database) == {"logon": "sealed:PLAIN123abc"}      # already sealed: left alone


def test_navigraph_link_is_kept_apart_from_settings(database):
    db.save_navigraph({"name": "jdoe", "refresh": "sealed"}, database)
    db.save_settings({"profile": {"username": "pilot"}, "generation": {}}, database)
    assert db.get_navigraph(database) == {"name": "jdoe", "refresh": "sealed"}   # a profile save keeps it
    assert "navigraph" not in db.get_settings(database)
    with db.locked_navigraph(database) as (link, save):
        assert link["refresh"] == "sealed"
        save({**link, "refresh": "sealed-2"})
    assert db.get_navigraph(database)["refresh"] == "sealed-2"
    db.save_navigraph({}, database)
    assert db.get_navigraph(database) == {}


def test_problem_reports_list_newest_first_and_go_with_the_account(database):
    first = db.create_report(database, "p@example.com", "bug", "The map is blank", {"view": "search"}, None)
    second = db.create_report(database, "p@example.com", "data", "Wrong runway", {"errors": ["x"]}, {"legs": [1]})
    listed = [r for r in db.list_reports() if r["user_id"] == database]
    assert [r["id"] for r in listed] == [second, first] and "flight" not in listed[0]
    assert listed[0]["has_flight"] and not listed[1]["has_flight"] and listed[0]["created_at"].endswith("Z")
    assert db.get_report(second)["flight"] == {"legs": [1]} and db.get_report("missing") is None
    new = db.count_new_reports()
    assert db.set_report_status(first, "fixed") and db.count_new_reports() == new - 1
    assert not db.set_report_status("missing", "seen")
    db.delete_user_data(database)
    assert db.get_report(first) is None and db.get_report(second) is None


def test_active_flight_revisions_reject_stale_writes(database):
    assert db.get_active_flight(database) == {"flight": None, "rev": 0}
    ok, current = db.save_active_flight(database, {"legs": [1]}, 0)
    assert ok and current["rev"] == 1
    ok, current = db.save_active_flight(database, {"legs": [2]}, 0)          # another device, stale copy
    assert not ok and current == {"flight": {"legs": [1]}, "rev": 1}


def test_pireps_are_scoped_to_their_owner(database):
    rec = db.create_pirep({"flight_number": "FR1", "pirep": {"submitted_at": "2026-07-01T10:00:00Z"}}, database)
    assert [r["id"] for r in db.list_pireps(database)] == [rec["id"]]
    assert not db.delete_pirep(rec["id"], "someone-else")
    assert db.delete_pirep(rec["id"], database)


def test_connections_are_reused_and_a_failed_query_does_not_poison_the_pool(database):
    with db.get_connection() as conn:
        first = conn
    with db.get_connection() as conn:
        assert conn is first                                                   # reused, no new handshake
    with pytest.raises(Exception):
        with db.get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM no_such_table")
    assert db.get_settings(database)["profile"]["username"] == ""             # the pool still works
    with db.get_connection() as conn:
        pooled = conn
    pooled.close()                                                             # dropped while idle in the pool...
    with db.get_connection() as conn:
        assert conn is not pooled and not conn.closed                          # ...and never handed out
