# Tests

One command runs everything:

```
tests/run_all.sh            # static checks, unit tests, browser suites
tests/run_all.sh --quick    # static checks and unit tests only (seconds)
```

## Set-up

```
pip install -r tests/requirements.txt
python -m playwright install chromium     # for the browser suites
```

Node.js is used only for `node --check` on `static/js/app.js`.

## What runs

| Step | What it checks | Needs |
|---|---|---|
| Python static check | pyflakes on every tracked `.py` file | — |
| JavaScript syntax | `node --check static/js/app.js` | Node.js |
| UI copy style | `tools/check_ui_copy.py`: no full stop at the end of app UI sentences | — |
| Unit tests | `tests/unit/`: generation, MEL/CDL effects, weather parsing, crew duty and curfews, times, logbook rows, HTTP endpoints | — |
| Database unit tests | `tests/unit/test_db.py`: settings, PIREPs, active-flight revisions, connection reuse | `TEST_DATABASE_URL` |
| Browser suites | `tests/e2e/`: search filters, settings, delays, MEL/CDL, slots/curfews/duty, pages | `DATABASE_URL`, Chromium |

The unit tests never touch the network or Supabase. The database ones are
skipped unless `TEST_DATABASE_URL` points at a throwaway Postgres database;
they create and delete rows for made-up users.

## Browser suites

`tests/e2e/run.py` starts the real app once per suite (`tests/e2e/server.py`)
with fixed weather samples instead of aviationweather.gov, signed in as a
single local test pilot, and resets that pilot's data first. Some suites use
a scenario that pins a random roll (a given MEL item, an ATFM delay) so the
same situation comes up every time.

```
export DATABASE_URL=postgresql://postgres:postgres@localhost:5432/vd_test   # throwaway: the test pilot's rows are reset
python tests/e2e/run.py            # all suites
python tests/e2e/run.py mel ops    # only suites whose file name contains "mel" or "ops"
```

Optional settings:

- `VD_E2E_PORT`: server port (default 5055)
- `VD_E2E_CHROMIUM`: a Chromium binary to use instead of Playwright's own
- `VD_E2E_ASSETS`: a folder holding Leaflet (`package/dist/...`) and the Inter
  font (`inter/package/files/...`) from npm, for machines that can't reach
  cdnjs and Google Fonts
- `VD_E2E_OUT`: where screenshots go (default: a temp folder)

Some browser checks name real flights from the schedule data (e.g. U2765
Gatwick-Belfast City for slots and curfews). After a schedule refresh, a
suite failing on a missing flight needs its flight number updated, not a
code fix.
