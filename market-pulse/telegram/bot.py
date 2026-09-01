#!/usr/bin/env python3
"""Telegram bridge for Market Pulse.

Runs on GitHub Actions, not in the Claude container — that container's network
policy blocks api.telegram.org (403 on CONNECT), and it is ephemeral besides.

  python3 bot.py poll     handle incoming commands (/report, /calendar, ...)
  python3 bot.py alerts   push any new item scoring >= state.alert_threshold

Both are idempotent: a command is answered once (update_id offset) and an alert
is sent once (sent ledger). Reads TELEGRAM_BOT_TOKEN from the environment.
"""
import json, os, re, sys, pathlib, urllib.request, urllib.error, urllib.parse

import quotes as quotes_mod

ROOT = pathlib.Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
REPO = ROOT.parent


def _load_dotenv():
    """Read KEY=VALUE lines from a .env at the repo root, for local runs.

    A real environment variable always wins, so CI (which sets the variable
    from the Actions secret) is never overridden by a stray file. No
    dependency — python-dotenv is not worth one for six lines.
    """
    f = REPO / ".env"
    if not f.exists():
        return
    for raw in f.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = val


_load_dotenv()
TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
API = "https://api.telegram.org/bot" + TOKEN

# Both names have been used for this secret. Trying them in a fixed order is
# what hid a dead token for two days: the stale TELEGRAM_BOT_TOKEN won over a
# fresh TELEGRAM_TOKEN, and the workflow reported the secret as "configured".
# Pick by what Telegram actually accepts, not by name precedence.
TOKEN_NAMES = ("TELEGRAM_BOT_TOKEN", "TELEGRAM_TOKEN")

# A well-formed token is <bot id>:<35-char secret>, about 46 characters.
TOKEN_SHAPE = re.compile(r"^\d{5,}:[A-Za-z0-9_-]{30,}$")


def clean_token(raw):
    """Recover the token from the ways a secret gets pasted wrong.

    A secret box wants the bare value, but the surrounding line usually comes
    with it: `TELEGRAM_TOKEN=123:abc`, a quoted value, or the api.telegram.org
    path prefix. Telegram answers 404 for all of them, which reads as a revoked
    token and sends you back to BotFather for a replacement that will not help.
    """
    tok = (raw or "").strip().strip('"').strip("'").strip()
    m = re.match(r"^[A-Za-z_][A-Za-z0-9_]*\s*=\s*(.+)$", tok)   # NAME=value
    if m:
        tok = m.group(1).strip().strip('"').strip("'").strip()
    tok = re.sub(r"^(?:https?://)?(?:api\.telegram\.org/)?bot(?=\d)", "", tok)
    return tok.strip()
MAXLEN = 3900          # Telegram hard-caps a message at 4096 chars
ARROW = {"bullish": "\U0001F7E2", "bearish": "\U0001F534", "uncertain": "\U0001F7E1"}


# ---------- io ----------

def load(name, default=None):
    p = DATA / name
    if not p.exists():
        return default
    return json.loads(p.read_text())


def save_tg(state):
    (DATA / "telegram.json").write_text(json.dumps(state, indent=2) + "\n")


def set_token(tok):
    global TOKEN, API
    TOKEN = tok
    API = "https://api.telegram.org/bot" + tok


def resolve_token():
    """Adopt whichever configured token Telegram actually accepts.

    Returns the winning variable name, or None if none work. Logs the name and
    length only — never the value, which would land in a public Actions log.
    """
    seen = []
    for name in TOKEN_NAMES:
        raw = os.environ.get(name, "")
        tok = clean_token(raw)
        if not tok or tok in seen:
            continue
        seen.append(tok)
        fixed = "" if tok == raw.strip() else \
            " (recovered from %d chars of surrounding text)" % (len(raw.strip()) - len(tok))
        set_token(tok)
        me = call("getMe", _quiet=True)
        if me.get("ok"):
            print("token: %s (%d chars)%s accepted as @%s"
                  % (name, len(tok), fixed, me["result"].get("username", "?")))
            return name
        shape = "" if TOKEN_SHAPE.match(tok) else \
            " — this does not look like a bot token (expected <id>:<secret>)"
        print("token: %s (%d chars)%s rejected by Telegram%s"
              % (name, len(tok), fixed, shape))
    set_token("")
    return None


def call(method, _quiet=False, _timeout=30, **params):
    body = urllib.parse.urlencode(
        {k: v for k, v in params.items() if v is not None}).encode()
    req = urllib.request.Request(API + "/" + method, data=body)
    try:
        with urllib.request.urlopen(req, timeout=_timeout) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        if _quiet:
            return {"ok": False, "error_code": e.code}
        sys.stderr.write("%s failed: %s %s\n" % (method, e.code, e.read()[:400]))
        if e.code in (401, 404):
            # Telegram answers 404 for an unknown /bot<token> path and 401 for a
            # rejected one: both mean the token is wrong, revoked, or replaced,
            # not that the method or the chat is missing. Say so, because a bare
            # 404 reads like a bug in the request.
            sys.stderr.write(
                "  the bot token is invalid or has been revoked. Open BotFather,"
                " send /mybots -> your bot -> API Token, and update the"
                " TELEGRAM_BOT_TOKEN repository secret.\n")
    except Exception as e:                                    # noqa: BLE001
        if _quiet:
            return {"ok": False}
        sys.stderr.write("%s failed: %r\n" % (method, e))
    return {"ok": False}


def send(chat_id, text):
    """Send text, splitting on paragraph boundaries if it exceeds the cap.

    Returns True only if every chunk was accepted. Callers that record a
    message as delivered must check it — a swallowed failure once marked an
    alert sent that Telegram had rejected, and the ledger then suppressed it
    for good.
    """
    ok = True
    while text:
        chunk = text
        if len(chunk) > MAXLEN:
            cut = text.rfind("\n\n", 0, MAXLEN)
            cut = cut if cut > 400 else MAXLEN
            chunk, text = text[:cut], text[cut:].lstrip("\n")
        else:
            text = ""
        if not call("sendMessage", chat_id=chat_id, text=chunk,
                    parse_mode="HTML", disable_web_page_preview="true").get("ok"):
            ok = False
    return ok


def esc(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


# ---------- report builders ----------

def fmt_item(i, brief=False):
    line = "%s <b>%s/10</b>  %s" % (
        ARROW.get(i["direction"], "⚪"), i["impact"], esc(i["headline"]))
    if brief:
        return line
    out = [line]
    if i.get("tickers"):
        out.append("<code>%s</code>" % esc("  ".join(i["tickers"][:9])))
    if i.get("why"):
        out.append("<i>%s</i>" % esc(i["why"]))
    if i.get("url"):
        out.append('<a href="%s">%s</a>' % (esc(i["url"]), esc(i.get("source", "source"))))
    return "\n".join(out)


def cmd_report(args=None):
    news = load("news.json", {"items": [], "session_date": "?"})
    state = load("state.json", {"alert_threshold": 8})
    items = sorted(news["items"], key=lambda x: (-x["impact"], x["time"]))
    head = ("\U0001F4C8 <b>Market Pulse</b> — session %s\n"
            "<i>Scanned %s · %d headlines · alert bar %s/10</i>" % (
                esc(news.get("session_date", "?")),
                esc(news.get("generated_at", "?")[:16].replace("T", " ") + " UTC"),
                len(items), state.get("alert_threshold", 8)))
    body = "\n\n".join(fmt_item(i) for i in items[:8])
    return head + "\n\n" + body


def cmd_top(args=None):
    news = load("news.json", {"items": []})
    thr = load("state.json", {"alert_threshold": 8}).get("alert_threshold", 8)
    hits = [i for i in sorted(news["items"], key=lambda x: -x["impact"])
            if i["impact"] >= thr]
    if not hits:
        return "Nothing at or above the %s/10 alert bar right now." % thr
    return ("\U0001F6A8 <b>Alert-grade (≥%s/10)</b>\n\n" % thr) + \
        "\n\n".join(fmt_item(i) for i in hits)


def cmd_board(args=None):
    m = load("movers.json", {"board": []})
    up = [b for b in m["board"] if b["signal"] == "bullish"]
    dn = [b for b in m["board"] if b["signal"] == "bearish"]
    rank = {"high": 0, "medium": 1, "low": 2}
    def block(rows, title):
        rows = sorted(rows, key=lambda r: rank.get(r["conviction"], 3))
        if not rows:
            return "<b>%s</b>\n  —" % title
        return "<b>%s</b>\n" % title + "\n".join(
            "  <code>%-5s</code> %s%s · <i>%s</i>" % (
                r["ticker"], esc(r["catalyst"]),
                "  " + r["observed_move"] if r.get("observed_move") else "",
                r["conviction"]) for r in rows)
    return "\U0001F4CA <b>Catalyst board</b>\n\n%s\n\n%s\n\n<i>%s</i>" % (
        block(up, "Likely up"), block(dn, "Likely down"),
        esc("News-derived, not a price feed. Moves shown only where a source reported one."))


def cmd_calendar(args=None):
    cal = load("calendar.json", {"events": []})
    today = load("news.json", {"session_date": "0000-00-00"})["session_date"]
    ev = sorted([e for e in cal["events"] if e["date"] >= today],
                key=lambda e: e["date"])[:14]
    if not ev:
        return "No upcoming events on the calendar."
    rows = []
    for e in ev:
        mark = "\U0001F534" if e["importance"] == "high" else "⚪"
        star = "" if e.get("confirmed") else " <i>(date unconfirmed)</i>"
        rows.append("%s <code>%s</code>  %s%s" % (mark, e["date"][5:], esc(e["title"]), star))
    return "\U0001F5D3 <b>Upcoming catalysts</b>\n\n" + "\n".join(rows)


def cmd_watchlist(args=None):
    wl = load("watchlist.json", {"tickers": []})["tickers"]
    news = load("news.json", {"items": []})["items"]
    rows = []
    for w in wl:
        hits = sum(1 for i in news if w["ticker"] in i.get("tickers", []))
        rows.append("<code>%-6s</code> %s%s" % (
            w["ticker"], esc(w.get("name", "")),
            "  ← %d in feed" % hits if hits else ""))
    return "⭐ <b>Watchlist</b> (%d)\n\n" % len(wl) + "\n".join(rows)


# ---------- live prices ----------

def _pct(v):
    """Signed percent with a colour marker, padded so columns line up."""
    arrow = "\U0001F53A" if v > 0 else ("\U0001F53B" if v < 0 else "\u25AB")
    return "%s %+6.2f%%" % (arrow, v)


def _price_table(rows):
    """rows: list of (ticker, quote). Returns a monospace aligned block."""
    out = []
    for t, q in rows:
        if "error" in q:
            out.append("%-6s      —  (%s)" % (t, q["error"]))
        else:
            out.append("%-6s %9.2f  %s" % (t, q["price"], _pct(q["pct"])))
    return "<pre>" + esc("\n".join(out)) + "</pre>"


def cmd_stocks(args=None):
    wl = [w["ticker"] for w in load("watchlist.json", {"tickers": []})["tickers"]]
    if args:
        wl = [a.upper() for a in args]
    if not wl:
        return "Watchlist is empty. Add tickers in the dashboard, or /stocks AAPL NVDA."
    q = quotes_mod.fetch(wl)
    rows = [(t, q.get(t, {"error": "missing"})) for t in wl]
    ok = [r for r in rows if "error" not in r[1]]
    rows.sort(key=lambda r: r[1].get("pct", -1e9), reverse=True)
    state = quotes_mod.market_state(q)
    head = "\U0001F4C8 <b>Watchlist — day change</b>"
    if state:
        head += "\n<i>%s</i>" % esc(state)
    if not ok:
        if not quotes_mod.configured():
            return (head + "\n\nLive prices need a free API key.\n\n"
                    "Free sources that need no key (Yahoo, Stooq) block GitHub's "
                    "servers outright, so this uses Finnhub instead.\n\n"
                    "1. Get a free key at finnhub.io/register\n"
                    "2. Add it as the <code>FINNHUB_API_KEY</code> repository secret\n\n"
                    "News commands work without it: /report /top /board /calendar")
        return head + "\n\nNo quotes came back. Try again shortly."
    body = _price_table(rows)
    up = sum(1 for _, x in ok if x["pct"] > 0)
    foot = "<i>%d up · %d down · %d of %d quoted</i>" % (
        up, len(ok) - up, len(ok), len(rows))
    return head + "\n\n" + body + "\n" + foot


def cmd_holdings(args=None):
    pos = load("holdings.json", {"positions": []})["positions"]
    if not pos:
        return ("You have no positions yet.\n\n"
                "Add one:  <code>/hold AAPL 10 195.50</code>\n"
                "(ticker, shares, average cost — cost is optional)\n"
                "Remove:  <code>/unhold AAPL</code>")
    q = quotes_mod.fetch([p["ticker"] for p in pos])
    lines, tot_val, tot_cost, tot_day = [], 0.0, 0.0, 0.0
    for p in pos:
        t = p["ticker"]
        quote = q.get(t, {"error": "missing"})
        if "error" in quote:
            lines.append("%-6s  —  (%s)" % (t, quote["error"]))
            continue
        sh = float(p.get("shares") or 0)
        val = quote["price"] * sh
        day = (quote["price"] - quote["prev"]) * sh
        tot_val += val
        tot_day += day
        line = "%-6s %8.2f  %s" % (t, quote["price"], _pct(quote["pct"]))
        if sh:
            line += "\n       %.4g sh  =  %s%.2f" % (sh, "$", val)
        cost = p.get("avg_cost")
        if cost and sh:
            tot_cost += cost * sh
            pl = (quote["price"] - cost) * sh
            plp = (quote["price"] - cost) / cost * 100.0
            line += "\n       P/L %s%+.2f (%+.2f%%)" % ("$", pl, plp)
        lines.append(line)
    head = "\U0001F4BC <b>Holdings</b>"
    state = quotes_mod.market_state(q)
    if state:
        head += "\n<i>%s</i>" % esc(state)
    foot = "\n<b>Value</b> $%.2f · <b>Today</b> %s%+.2f" % (tot_val, "$", tot_day)
    if tot_cost:
        foot += "\n<b>Total P/L</b> $%+.2f (%+.2f%%)" % (
            tot_val - tot_cost, (tot_val - tot_cost) / tot_cost * 100.0)
    return head + "\n\n<pre>" + esc("\n".join(lines)) + "</pre>" + foot


def cmd_hold(args=None):
    """/hold TICKER SHARES [AVG_COST]"""
    if not args:
        return ("Usage: <code>/hold TICKER SHARES [AVG_COST]</code>\n"
                "e.g. <code>/hold NVDA 25 178.40</code>")
    d = load("holdings.json", {"positions": []})
    t = args[0].upper().lstrip("$")
    try:
        sh = float(args[1]) if len(args) > 1 else 0.0
        cost = float(args[2]) if len(args) > 2 else None
    except ValueError:
        return "Shares and cost must be numbers. e.g. <code>/hold NVDA 25 178.40</code>"
    d["positions"] = [p for p in d["positions"] if p["ticker"] != t]
    d["positions"].append({"ticker": t, "shares": sh, "avg_cost": cost})
    d["positions"].sort(key=lambda p: p["ticker"])
    (DATA / "holdings.json").write_text(json.dumps(d, indent=2) + "\n")
    msg = "Saved <b>%s</b>: %g shares" % (esc(t), sh)
    if cost:
        msg += " @ $%.2f" % cost
    return msg + "\n\nSee /holdings"


def cmd_unhold(args=None):
    if not args:
        return "Usage: <code>/unhold TICKER</code>"
    d = load("holdings.json", {"positions": []})
    t = args[0].upper().lstrip("$")
    before = len(d["positions"])
    d["positions"] = [p for p in d["positions"] if p["ticker"] != t]
    if len(d["positions"]) == before:
        return "%s is not in your holdings." % esc(t)
    (DATA / "holdings.json").write_text(json.dumps(d, indent=2) + "\n")
    return "Removed <b>%s</b>." % esc(t)


HELP = ("\U0001F4C8 <b>Market Pulse bot</b>\n\n"
        "<b>Prices</b>\n"
        "/stocks — day % change across your watchlist\n"
        "/holdings — your positions, value and P/L\n"
        "/hold TICKER SHARES [COST] — add or update a position\n"
        "/unhold TICKER — remove a position\n\n"
        "<b>News</b>\n"
        "/report — full scored report, newest scan\n"
        "/top — only alert-grade items (≥8/10)\n"
        "/board — likely up / likely down\n"
        "/calendar — upcoming earnings &amp; macro events\n"
        "/watchlist — your tracked tickers\n"
        "/help — this message\n\n"
        "<i>Scans run every 2h on weekdays. High-impact items are pushed "
        "here automatically. Prices are fetched live when you ask.</i>")

COMMANDS = {"report": cmd_report, "top": cmd_top, "board": cmd_board,
            "calendar": cmd_calendar, "watchlist": cmd_watchlist,
            "stocks": cmd_stocks, "holdings": cmd_holdings,
            "hold": cmd_hold, "unhold": cmd_unhold,
            "help": lambda a=None: HELP, "start": lambda a=None: HELP}


# ---------- modes ----------

def poll(long_poll=0):
    """One getUpdates pass. long_poll>0 holds the connection open that many
    seconds, so a command is answered as it arrives rather than on the next
    scheduled run."""
    tg = load("telegram.json", {"chat_ids": [], "offset": 0, "sent_ids": []})
    res = call("getUpdates", offset=tg.get("offset", 0), timeout=long_poll,
               limit=40, _timeout=long_poll + 15)
    if not res.get("ok"):
        return 1
    updates = res.get("result", [])
    failed = 0
    for u in updates:
        tg["offset"] = u["update_id"] + 1
        msg = u.get("message") or u.get("channel_post") or {}
        chat = msg.get("chat", {}).get("id")
        text = (msg.get("text") or "").strip()
        if not chat or not text.startswith("/"):
            continue
        if chat not in tg["chat_ids"]:
            tg["chat_ids"].append(chat)              # register for alerts
        parts = text[1:].split()
        name = parts[0].split("@")[0].lower()
        fn = COMMANDS.get(name)
        if send(chat, fn(parts[1:]) if fn else "Unknown command. Try /help"):
            print("answered /%s for chat %s" % (name, chat))
        else:
            # The offset still advances: a reply that Telegram refuses outright
            # would otherwise be retried on every run forever. Failing the run
            # is what surfaces it.
            failed += 1
            sys.stderr.write("could not answer /%s for chat %s\n" % (name, chat))
    save_tg(tg)
    return 1 if failed else 0


def alerts(quiet=False):
    """quiet suppresses the two routine no-ops, which the worker hits on every
    pass — a few thousand times a shift."""
    tg = load("telegram.json", {"chat_ids": [], "offset": 0, "sent_ids": []})
    if not tg["chat_ids"]:
        if not quiet:
            print("no registered chats yet — send /start to the bot")
        return 0
    thr = load("state.json", {"alert_threshold": 8}).get("alert_threshold", 8)
    news = load("news.json", {"items": []})["items"]
    fresh = [i for i in sorted(news, key=lambda x: -x["impact"])
             if i["impact"] >= thr and i["id"] not in tg["sent_ids"]]
    if not fresh:
        if not quiet:
            print("no new alert-grade items")
        return 0
    text = "\U0001F6A8 <b>%d new alert-grade item%s</b>\n\n" % (
        len(fresh), "" if len(fresh) == 1 else "s")
    text += "\n\n".join(fmt_item(i) for i in fresh)
    delivered = sum(1 for chat in tg["chat_ids"] if send(chat, text))
    if not delivered:
        # Leave the ledger untouched so the next run retries. Recording these
        # ids now would bury the news permanently, which is the opposite of
        # what an alert bar is for.
        sys.stderr.write(
            "delivered to 0 of %d chat(s) — %d alert(s) held for retry\n"
            % (len(tg["chat_ids"]), len(fresh)))
        return 1
    tg["sent_ids"] = (tg["sent_ids"] + [i["id"] for i in fresh])[-400:]
    save_tg(tg)
    print("pushed %d alert(s) to %d of %d chat(s)"
          % (len(fresh), delivered, len(tg["chat_ids"])))
    return 0


def register_commands():
    """Populate the blue command menu in the Telegram UI. Run once."""
    cmds = [{"command": c, "description": d} for c, d in [
        ("stocks", "Watchlist day % change"),
        ("holdings", "Your positions, value and P/L"),
        ("hold", "Add a position: /hold NVDA 25 178.40"),
        ("unhold", "Remove a position: /unhold NVDA"),
        ("report", "Full scored market report"),
        ("top", "Only alert-grade items"),
        ("board", "Likely up / likely down"),
        ("calendar", "Upcoming earnings & macro"),
        ("watchlist", "Your tracked tickers"),
        ("help", "Show commands")]]
    print(call("setMyCommands", commands=json.dumps(cmds)))
    return 0




# ---------- long-running worker ----------

def _git(*args):
    import subprocess
    return subprocess.run(("git",) + args, cwd=str(REPO),
                          capture_output=True, text=True)


def persist(what):
    """Commit and push the data files, if any changed. Rebases on a race with
    the scan workflow, which pushes to the same branch."""
    branch = os.environ.get("DATA_BRANCH", "").strip()
    if not branch:
        # Only the workflow sets DATA_BRANCH. Without it we are running from
        # someone's checkout, where committing and pushing on their behalf is
        # never what they meant — a bare `bot.py serve` here once committed a
        # test ledger onto the working branch.
        print("  DATA_BRANCH unset — not committing (%s)" % what)
        return
    files = ["market-pulse/data/telegram.json", "market-pulse/data/holdings.json",
             "market-pulse/data/news.json", "market-pulse/data/state.json"]
    if not _git("diff", "--quiet", "--", *files).returncode:
        return
    _git("add", *files)
    _git("commit", "-m", "chore(bot): %s [skip ci]" % what)
    for attempt in range(4):
        if not _git("push", "origin", "HEAD:" + branch).returncode:
            print("  pushed (%s)" % what)
            return
        _git("fetch", "origin", branch)
        if _git("rebase", "origin/" + branch).returncode:
            _git("rebase", "--abort")
            sys.stderr.write("  could not rebase onto %s\n" % branch)
            return
    sys.stderr.write("  push failed after 4 attempts\n")


def run_scan():
    import subprocess
    scan = ROOT / "scan.py"
    if not scan.exists() or not os.environ.get("GEMINI_API_KEY"):
        return
    print("scan: starting")
    r = subprocess.run([sys.executable, str(scan)], cwd=str(REPO),
                       capture_output=True, text=True, timeout=900)
    for line in (r.stdout or "").strip().splitlines()[-6:]:
        print("  " + line)
    if r.returncode:
        sys.stderr.write("  scan exited %d: %s\n"
                         % (r.returncode, (r.stderr or "")[-400:]))


def serve():
    """Stay resident and poll continuously, instead of waking on cron.

    GitHub fires this repo's */5 schedule roughly once every four hours — it
    drops the rest — so a cron-driven bot answers a command hours late and an
    alert lands long after the move. One job that lives for its whole timeout
    and long-polls covers the gap: the schedule only has to land often enough
    to start the next worker, which it comfortably does.
    """
    import time
    minutes = float(os.environ.get("SERVE_MINUTES", "330"))
    scan_every = float(os.environ.get("SCAN_EVERY_MIN", "60")) * 60
    deadline = time.time() + minutes * 60
    print("worker: serving for %.0f min, scanning every %.0f min"
          % (minutes, scan_every / 60))

    next_scan = 0.0          # scan immediately on start
    misses = 0
    while time.time() < deadline:
        if time.time() >= next_scan:
            run_scan()
            persist("scan")
            next_scan = time.time() + scan_every

        if alerts(quiet=True) == 0:
            persist("alerts")

        rc = poll(long_poll=25)
        if rc == 0:
            misses = 0
            persist("commands")
        else:
            # A dead token fails every call; do not spend hours hammering it.
            misses += 1
            if not resolve_token():
                sys.stderr.write("worker: no usable token, exiting for restart\n")
                return 1
            if misses >= 20:
                sys.stderr.write("worker: 20 consecutive poll failures\n")
                return 1
            time.sleep(min(60, 2 ** misses))
    print("worker: shift complete")
    return 0


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "poll"
    modes = {"poll": poll, "alerts": alerts, "serve": serve,
             "setup": register_commands}
    if mode not in modes:
        sys.exit("unknown mode %r: expected one of %s"
                 % (mode, ", ".join(sorted(modes))))
    if not any(os.environ.get(n, "").strip() for n in TOKEN_NAMES):
        sys.exit("no bot token: set %s" % " or ".join(TOKEN_NAMES))
    if not resolve_token():
        sys.exit("no configured token was accepted by Telegram. Open BotFather,"
                 " send /mybots -> your bot -> API Token, and update the secret.")
    sys.exit(modes[mode]())
