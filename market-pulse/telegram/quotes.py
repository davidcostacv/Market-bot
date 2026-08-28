#!/usr/bin/env python3
"""Live quote fetching for the Telegram bot.

Runs on GitHub Actions. The Claude container cannot reach finance hosts (its
network policy 403s them), so this module is verified by running the workflow
with mode=prices, never locally.

Source order matters. Yahoo's v8 chart endpoint returns HTTP 429 for GitHub
Actions runners — it rate-limits datacenter IP ranges — so Stooq is primary and
Yahoo is only a fallback for symbols Stooq does not carry.

Stooq needs two endpoints to produce a day change:
  q/l    last price, intraday, many symbols in one request
  q/d/l  daily history, for the previous session's close
Day change is measured against the previous close, per convention, not against
today's open.
"""
import csv, datetime, io, json, urllib.request, urllib.error
from concurrent.futures import ThreadPoolExecutor

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
TIMEOUT = 15
STOOQ_LAST = "https://stooq.com/q/l/?s={}&f=sd2t2ohlcv&h&e=csv"
STOOQ_HIST = "https://stooq.com/q/d/l/?s={}&i=d&d1={}&d2={}"
YAHOO = "https://query1.finance.yahoo.com/v8/finance/chart/{}?range=5d&interval=1d"


def _get(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return r.read().decode("utf-8", "replace")


def _stooq_symbol(t):
    """AAPL -> aapl.us ; BRK.B -> brk-b.us"""
    return t.replace(".", "-").lower() + ".us"


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _stooq_last(tickers):
    """One request for every symbol's latest price. {ticker: price}."""
    syms = ",".join(_stooq_symbol(t) for t in tickers)
    out = {}
    try:
        text = _get(STOOQ_LAST.format(syms))
    except Exception:                                             # noqa: BLE001
        return out
    back = {_stooq_symbol(t): t for t in tickers}
    for row in csv.DictReader(io.StringIO(text)):
        t = back.get((row.get("Symbol") or "").lower())
        price = _f(row.get("Close"))
        if t and price:
            out[t] = price
    return out


def _stooq_prev_close(ticker):
    """Previous session's close, from daily history."""
    today = datetime.date.today()
    d1 = (today - datetime.timedelta(days=14)).strftime("%Y%m%d")
    d2 = today.strftime("%Y%m%d")
    try:
        text = _get(STOOQ_HIST.format(_stooq_symbol(ticker), d1, d2))
    except Exception:                                             # noqa: BLE001
        return None
    closes = [_f(r.get("Close")) for r in csv.DictReader(io.StringIO(text))]
    closes = [c for c in closes if c]
    # last row is the current/most recent session; the one before it is the base
    return closes[-2] if len(closes) >= 2 else (closes[-1] if closes else None)


def _yahoo(ticker):
    """Fallback for symbols Stooq lacks. Often 429s from a runner."""
    try:
        payload = json.loads(_get(YAHOO.format(ticker.replace(".", "-").upper())))
        meta = payload["chart"]["result"][0]["meta"]
        price = meta.get("regularMarketPrice")
        prev = meta.get("chartPreviousClose") or meta.get("previousClose")
        if price and prev:
            return float(price), float(prev), meta.get("marketState", "")
    except Exception:                                             # noqa: BLE001
        pass
    return None


def fetch(tickers, workers=8):
    """{ticker: {price, prev, pct, ...}} — a failed ticker carries 'error'."""
    tickers = list(dict.fromkeys(t for t in tickers if t))
    if not tickers:
        return {}

    last = _stooq_last(tickers)
    with ThreadPoolExecutor(max_workers=min(workers, len(tickers))) as ex:
        prevs = dict(zip(tickers, ex.map(_stooq_prev_close, tickers)))

    out = {}
    missing = []
    for t in tickers:
        p, pc = last.get(t), prevs.get(t)
        if p and pc:
            out[t] = {"ticker": t, "price": p, "prev": pc,
                      "pct": (p - pc) / pc * 100.0, "source": "stooq"}
        else:
            missing.append(t)

    if missing:                       # try Yahoo only for what Stooq missed
        with ThreadPoolExecutor(max_workers=min(workers, len(missing))) as ex:
            for t, y in zip(missing, ex.map(_yahoo, missing)):
                if y:
                    p, pc, state = y
                    out[t] = {"ticker": t, "price": p, "prev": pc,
                              "pct": (p - pc) / pc * 100.0,
                              "source": "yahoo", "state": state}
                else:
                    out[t] = {"ticker": t, "error": "no quote"}
    return out


def market_state(quotes):
    for q in quotes.values():
        s = q.get("state")
        if s:
            return {"PRE": "pre-market", "REGULAR": "market open",
                    "POST": "after hours", "POSTPOST": "after hours",
                    "CLOSED": "market closed"}.get(s, s.lower())
    return ""


if __name__ == "__main__":
    import sys
    got = fetch(sys.argv[1:] or ["AAPL", "NVDA"])
    for t, q in got.items():
        print(t, q)
    ok = sum(1 for q in got.values() if "error" not in q)
    print("\n%d/%d quoted" % (ok, len(got)))
