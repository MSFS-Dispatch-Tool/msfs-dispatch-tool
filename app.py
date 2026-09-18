"""
MSFS immersion tool - bare-bones Flask app (step 3).

At this stage this does ONE thing: loads all five JSON datasets into memory
at startup and serves a single page confirming they loaded, with counts.
No generator logic yet (that's step 4) - this is purely "does the app run
and can it read its own data" before anything else gets built on top.
"""

from flask import Flask, render_template
import json
import os

app = Flask(__name__)

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")


def load_json(filename):
    path = os.path.join(DATA_DIR, filename)
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


# Loaded once at startup, not re-read per request (see step 2 discussion on
# why: the datasets are small and barely change, so there's no benefit to
# hitting disk on every "generate" click).
routes = load_json("routes_enriched.json")
mels = load_json("mel_list.json")
delay_codes = load_json("delay_codes.json")
lmc_events = load_json("lmc_events.json")
dangerous_goods = load_json("dangerous_goods.json")


@app.route("/")
def index():
    counts = {
        "routes": len(routes),
        "mels": len(mels),
        "delay_codes": len(delay_codes),
        "lmc_events": len(lmc_events),
        "dangerous_goods": len(dangerous_goods),
    }
    return render_template("index.html", counts=counts)


if __name__ == "__main__":
    # debug=True is fine for local testing, but should be OFF before this
    # ever gets deployed publicly (even to a free host) - it exposes a
    # debugger console that can execute arbitrary code if left on.
    app.run(debug=True, port=5000)
