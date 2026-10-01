"""Runs every browser suite against its own server scenario.

    python tests/e2e/run.py              # all suites
    python tests/e2e/run.py mel ops      # suites whose name contains "mel" or "ops"

Each suite gets a fresh server (see server.py) on VD_E2E_PORT (default
5055) with the test pilot's data reset first, so suites don't depend on
each other's leftovers. Exits non-zero if any suite fails.
"""
import os
import subprocess
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
PORT = int(os.environ.get("VD_E2E_PORT", "5055"))
URL = f"http://localhost:{PORT}"

# (suite file, server scenario)
SUITES = [
    ("test_pages.py", "demo"),
    ("test_search.py", "demo"),
    ("test_settings.py", "demo"),
    ("test_delays.py", "demo"),
    ("test_mel.py", "mel"),
    ("test_mel738.py", "mel738"),
    ("test_ops.py", "ops"),
]


def wait_for_server(proc, timeout=60):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            return False
        try:
            urllib.request.urlopen(URL + "/privacy", timeout=2)
            return True
        except Exception:
            time.sleep(0.5)
    return False


def run_suite(suite, scenario):
    server_py = os.path.join(HERE, "server.py")
    subprocess.run([sys.executable, server_py, "--seed"], check=True, stdout=subprocess.DEVNULL)
    log = open(os.path.join(HERE, f".server-{scenario}.log"), "w")
    server = subprocess.Popen([sys.executable, server_py, "--scenario", scenario, "--port", str(PORT)],
                              stdout=log, stderr=subprocess.STDOUT)
    try:
        if not wait_for_server(server):
            print(f"== {suite}: the server didn't start, see tests/e2e/.server-{scenario}.log")
            return False
        print(f"== {suite} ({scenario})", flush=True)
        env = dict(os.environ, VD_E2E_URL=URL)
        return subprocess.run([sys.executable, os.path.join(HERE, suite)], env=env).returncode == 0
    finally:
        server.terminate()
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server.kill()
        log.close()


def main():
    wanted = sys.argv[1:]
    suites = [(s, sc) for s, sc in SUITES if not wanted or any(w in s for w in wanted)]
    failed = [s for s, sc in suites if not run_suite(s, sc)]
    print(f"\n{len(suites) - len(failed)} of {len(suites)} browser suites passed" + (f"; failed: {', '.join(failed)}" if failed else ""))
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
