#!/usr/bin/env python3
"""Live quote fetching for the Telegram bot.

Runs on GitHub Actions. Verified with `mode=prices`, never locally — the Claude
container 403s finance hosts.

Source history, established by probing rather than assuming:
  Yahoo v8 chart   HTTP 429, rate-limits datacenter IP ranges
  Stooq q/l        HTTP 404, endpoint retired
  Stooq q/d/l      HTTP 200 but a JavaScript bot-verification challenge

Both free no-key sources block cloud runners, and neither is fixable with
headers or retries. Finnhub's free tier does work from a runner and returns the
previous close directly, so it is the source. It needs a free API key in the
FINNHUB_API_KEY secret; without one, quotes are unavailable and say so plainly
rather than failing mysteriously.
"""
import json, os, urllib.request, urllib.error
from concurrent.futures import ThreadPoolExecutor

UA = "market-pulse-bot/1.0"
TIMEOUT = 12
KEY = os.environ.get("FINNHUB_API_KEY", "").strip()
FINNHUB = "https://finnhub.io/api/v1/quote?symbol={}&token={}"

NO_KEY = "no API key"


def _symbol(t):
    """BRK.B -> BRK.B ; Finnhub uses dots for share classes, unlike Yahoo."""
    return t.upper().strip()


def _one(ticker):
    if not KEY:
        return {"ticker": ticker, "error": NO_KEY}
    url = FINNHUB.format(urllib.parse.quote(_symbol(ticker)), KEY)
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            d = json.loads(r.read())
    except urllib.error.HTTPError as e:
        return {"ticker": ticker, "error": "http %s" % e.code}
    except Exception as e:                                        # noqa: BLE001
        return {"ticker": ticker, "error": type(e).__name__}

    price, prev = d.get("c"), d.get("pc")
    # Finnhub returns c=0 for an unknown symbol rather than an error
    if not price or not prev:
        return {"ticker": ticker, "error": "unknown symbol"}
    pct = d.get("dp")
    if pct is None:
        pct = (price - prev) / prev * 100.0
    return {"ticker": ticker, "price": float(price), "prev": float(prev),
            "pct": float(pct), "source": "finnhub"}


def fetch(tickers, workers=8):
    """{ticker: {price, prev, pct, ...}} — a failed ticker carries 'error'."""
    tickers = list(dict.fromkeys(t for t in tickers if t))
    if not tickers:
        return {}
    if not KEY:                       # one shared reason, no pointless requests
        return {t: {"ticker": t, "error": NO_KEY} for t in tickers}
    with ThreadPoolExecutor(max_workers=min(workers, len(tickers))) as ex:
        return {q["ticker"]: q for q in ex.map(_one, tickers)}


def configured():
    return bool(KEY)


def market_state(quotes):
    return ""


import urllib.parse  # noqa: E402  (used by _one)


if __name__ == "__main__":
    import sys
    args = [a for a in sys.argv[1:] if a != "--probe"]
    if not KEY:
        print("FINNHUB_API_KEY is not set — quotes are unavailable.")
        print("Add a free key at https://finnhub.io/register, then store it as")
        print("the FINNHUB_API_KEY repository secret.")
        sys.exit(0)
    got = fetch(args or ["AAPL", "NVDA", "MSFT", "SPY"])
    for t, q in got.items():
        print(t, q)
    ok = sum(1 for q in got.values() if "error" not in q)
    print("\n%d/%d quoted" % (ok, len(got)))
