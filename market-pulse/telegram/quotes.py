#!/usr/bin/env python3
"""Live quote fetching for the Telegram bot.

Runs on GitHub Actions, which has open outbound internet. (The Claude
container does not — its network policy 403s finance hosts — so this module
cannot be exercised there; it is verified by running the workflow.)

Yahoo's v8 chart endpoint is the source: it needs no API key and no crumb,
unlike the v7 quote endpoint which has required auth since 2024.
"""
import json, re, urllib.request, urllib.error
from concurrent.futures import ThreadPoolExecutor

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
CHART = "https://query1.finance.yahoo.com/v8/finance/chart/{}?range=5d&interval=1d"
TIMEOUT = 12


def _yahoo_symbol(t):
    """BRK.B -> BRK-B; Yahoo uses dashes for share classes."""
    return t.replace(".", "-").upper().strip()


def _one(ticker):
    """Fetch a single quote. Returns a dict, or {'error': ...} — never raises."""
    sym = _yahoo_symbol(ticker)
    req = urllib.request.Request(CHART.format(sym), headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            payload = json.loads(r.read())
    except urllib.error.HTTPError as e:
        return {"ticker": ticker, "error": "http %s" % e.code}
    except Exception as e:                                        # noqa: BLE001
        return {"ticker": ticker, "error": type(e).__name__}

    try:
        res = payload["chart"]["result"][0]
        meta = res["meta"]
    except (KeyError, IndexError, TypeError):
        return {"ticker": ticker, "error": "no data"}

    price = meta.get("regularMarketPrice")
    prev = meta.get("chartPreviousClose") or meta.get("previousClose")

    # Prefer the prior session's close from the series: chartPreviousClose can
    # lag on the first print of a new session.
    try:
        closes = [c for c in res["indicators"]["quote"][0]["close"] if c is not None]
        if len(closes) >= 2:
            prev = closes[-2] if abs(closes[-1] - (price or closes[-1])) < 1e-9 else prev or closes[-2]
    except (KeyError, IndexError, TypeError):
        pass

    if price is None or not prev:
        return {"ticker": ticker, "error": "no price"}

    return {
        "ticker": ticker,
        "price": float(price),
        "prev": float(prev),
        "pct": (float(price) - float(prev)) / float(prev) * 100.0,
        "currency": meta.get("currency", "USD"),
        "state": meta.get("marketState", ""),
        "name": meta.get("shortName") or meta.get("longName") or "",
    }


def fetch(tickers, workers=8):
    """Fetch many quotes concurrently. Returns {ticker: quote_dict}."""
    tickers = list(dict.fromkeys(t for t in tickers if t))
    if not tickers:
        return {}
    with ThreadPoolExecutor(max_workers=min(workers, len(tickers))) as ex:
        return {q["ticker"]: q for q in ex.map(_one, tickers)}


def market_state(quotes):
    """Best-effort session label from whatever came back."""
    for q in quotes.values():
        s = q.get("state")
        if s:
            return {"PRE": "pre-market", "REGULAR": "market open",
                    "POST": "after hours", "POSTPOST": "after hours",
                    "CLOSED": "market closed"}.get(s, s.lower())
    return ""


if __name__ == "__main__":
    import sys
    for t, q in fetch(sys.argv[1:] or ["AAPL", "NVDA"]).items():
        print(t, q)
