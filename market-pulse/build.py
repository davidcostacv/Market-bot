#!/usr/bin/env python3
"""Render the Market Pulse dashboard from data/*.json into dashboard.html.

Deterministic: no judgment here. The agent writes the JSON; this renders it.
"""
import json, pathlib, datetime

ROOT = pathlib.Path(__file__).parent
D = ROOT / "data"

def load(name):
    return json.loads((D / name).read_text())

payload = {
    "news": load("news.json"),
    "movers": load("movers.json"),
    "calendar": load("calendar.json"),
    "watchlist": load("watchlist.json"),
    "state": load("state.json"),
    "universe": load("universe.json"),
    "built_at": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
}

tpl = (ROOT / "template.html").read_text()
out = tpl.replace("__PAYLOAD__", json.dumps(payload, separators=(",", ":")))
(ROOT / "dashboard.html").write_text(out)
print(f"dashboard.html written ({len(out):,} bytes) — "
      f"{len(payload['news']['items'])} news, "
      f"{len(payload['movers']['board'])} catalysts, "
      f"{len(payload['calendar']['events'])} events, "
      f"{len(payload['watchlist']['tickers'])} watchlist")
