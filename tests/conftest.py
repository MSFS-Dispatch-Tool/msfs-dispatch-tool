"""Unit tests run the app in-process with no external services: Supabase
and the database are switched off before anything is imported (database
tests opt back in with TEST_DATABASE_URL, see unit/test_db.py), and
weather lookups return nothing unless a test supplies reports."""
import os
import sys

for var in ("SUPABASE_URL", "SUPABASE_ANON_KEY", "SUPABASE_SERVICE_ROLE_KEY", "DATABASE_URL", "TURNSTILE_SECRET_KEY"):
    os.environ.pop(var, None)
os.environ.setdefault("FLASK_SECRET_KEY", "unit-tests")

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import pytest  # noqa: E402


@pytest.fixture(scope="session")
def app_module():
    import app
    return app


@pytest.fixture
def client(app_module, monkeypatch):
    monkeypatch.setattr(app_module, "fetch_weather_batch",
                        lambda icaos: {i: {"metar": None, "taf": None} for i in set(icaos)})
    return app_module.app.test_client()


# The browser suites are standalone scripts run by tests/e2e/run.py
collect_ignore_glob = ["e2e/*"]
