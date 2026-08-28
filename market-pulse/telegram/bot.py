#!/usr/bin/env python3
"""Telegram bridge for Market Pulse.

Runs on GitHub Actions, not in the Claude container — that container's network
policy blocks api.telegram.org (403 on CONNECT), and it is ephemeral besides.

  python3 bot.py poll     handle incoming commands (/report, /calendar, ...)
  python3 bot.py alerts   push any new item scoring >= state.alert_threshold

Both are idempotent: a command is answered once (update_id offset) and an alert
is sent once (sent ledger). Reads TELEGRAM_BOT_TOKEN from the environment.
"""
import json, os, sys, pathlib, urllib.request, urllib.error, urllib.parse

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


def call(method, **params):
    body = urllib.parse.urlencode(
        {k: v for k, v in params.items() if v is not None}).encode()
    req = urllib.request.Request(API + "/" + method, data=body)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        sys.stderr.write("%s failed: %s %s\n" % (method, e.code, e.read()[:400]))
    except Exception as e:                                    # noqa: BLE001
        sys.stderr.write("%s failed: %r\n" % (method, e))
    return {"ok": False}


def send(chat_id, text):
    """Send text, splitting on paragraph boundaries if it exceeds the cap."""
    while text:
        chunk = text
        if len(chunk) > MAXLEN:
            cut = text.rfind("\n\n", 0, MAXLEN)
            cut = cut if cut > 400 else MAXLEN
            chunk, text = text[:cut], text[cut:].lstrip("\n")
        else:
            text = ""
        call("sendMessage", chat_id=chat_id, text=chunk,
             parse_mode="HTML", disable_web_page_preview="true")


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


def cmd_report():
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


def cmd_top():
    news = load("news.json", {"items": []})
    thr = load("state.json", {"alert_threshold": 8}).get("alert_threshold", 8)
    hits = [i for i in sorted(news["items"], key=lambda x: -x["impact"])
            if i["impact"] >= thr]
    if not hits:
        return "Nothing at or above the %s/10 alert bar right now." % thr
    return ("\U0001F6A8 <b>Alert-grade (≥%s/10)</b>\n\n" % thr) + \
        "\n\n".join(fmt_item(i) for i in hits)


def cmd_board():
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


def cmd_calendar():
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


def cmd_watchlist():
    wl = load("watchlist.json", {"tickers": []})["tickers"]
    news = load("news.json", {"items": []})["items"]
    rows = []
    for w in wl:
        hits = sum(1 for i in news if w["ticker"] in i.get("tickers", []))
        rows.append("<code>%-6s</code> %s%s" % (
            w["ticker"], esc(w.get("name", "")),
            "  ← %d in feed" % hits if hits else ""))
    return "⭐ <b>Watchlist</b> (%d)\n\n" % len(wl) + "\n".join(rows)


HELP = ("\U0001F4C8 <b>Market Pulse bot</b>\n\n"
        "/report — full scored report, newest scan\n"
        "/top — only alert-grade items (≥8/10)\n"
        "/board — likely up / likely down\n"
        "/calendar — upcoming earnings &amp; macro events\n"
        "/watchlist — your tracked tickers\n"
        "/help — this message\n\n"
        "<i>Scans run hourly on weekdays, 08:00–17:00 ET. "
        "High-impact items are pushed here automatically.</i>")

COMMANDS = {"report": cmd_report, "top": cmd_top, "board": cmd_board,
            "calendar": cmd_calendar, "watchlist": cmd_watchlist,
            "help": lambda: HELP, "start": lambda: HELP}


# ---------- modes ----------

def poll():
    tg = load("telegram.json", {"chat_ids": [], "offset": 0, "sent_ids": []})
    res = call("getUpdates", offset=tg.get("offset", 0), timeout=0, limit=40)
    if not res.get("ok"):
        return 1
    updates = res.get("result", [])
    for u in updates:
        tg["offset"] = u["update_id"] + 1
        msg = u.get("message") or u.get("channel_post") or {}
        chat = msg.get("chat", {}).get("id")
        text = (msg.get("text") or "").strip()
        if not chat or not text.startswith("/"):
            continue
        if chat not in tg["chat_ids"]:
            tg["chat_ids"].append(chat)              # register for alerts
        name = text[1:].split()[0].split("@")[0].lower()
        fn = COMMANDS.get(name)
        send(chat, fn() if fn else "Unknown command. Try /help")
        print("answered /%s for chat %s" % (name, chat))
    save_tg(tg)
    return 0


def alerts():
    tg = load("telegram.json", {"chat_ids": [], "offset": 0, "sent_ids": []})
    if not tg["chat_ids"]:
        print("no registered chats yet — send /start to the bot")
        return 0
    thr = load("state.json", {"alert_threshold": 8}).get("alert_threshold", 8)
    news = load("news.json", {"items": []})["items"]
    fresh = [i for i in sorted(news, key=lambda x: -x["impact"])
             if i["impact"] >= thr and i["id"] not in tg["sent_ids"]]
    if not fresh:
        print("no new alert-grade items")
        return 0
    text = "\U0001F6A8 <b>%d new alert-grade item%s</b>\n\n" % (
        len(fresh), "" if len(fresh) == 1 else "s")
    text += "\n\n".join(fmt_item(i) for i in fresh)
    for chat in tg["chat_ids"]:
        send(chat, text)
    tg["sent_ids"] = (tg["sent_ids"] + [i["id"] for i in fresh])[-400:]
    save_tg(tg)
    print("pushed %d alert(s) to %d chat(s)" % (len(fresh), len(tg["chat_ids"])))
    return 0


def register_commands():
    """Populate the blue command menu in the Telegram UI. Run once."""
    cmds = [{"command": c, "description": d} for c, d in [
        ("report", "Full scored market report"),
        ("top", "Only alert-grade items"),
        ("board", "Likely up / likely down"),
        ("calendar", "Upcoming earnings & macro"),
        ("watchlist", "Your tracked tickers"),
        ("help", "Show commands")]]
    print(call("setMyCommands", commands=json.dumps(cmds)))
    return 0


if __name__ == "__main__":
    if not TOKEN:
        sys.exit("TELEGRAM_BOT_TOKEN is not set")
    mode = sys.argv[1] if len(sys.argv) > 1 else "poll"
    sys.exit({"poll": poll, "alerts": alerts,
              "setup": register_commands}.get(mode, poll)())
