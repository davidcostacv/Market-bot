#!/usr/bin/env python3
"""Market Pulse scan — runs on GitHub Actions against the Anthropic API.

Replaces the scheduled Claude session, which could read the repo but never had
git push access, so its findings were silently discarded every run.

One request: Claude searches the wires with the server-side web_search tool,
scores each headline, and returns JSON. This process then writes the data files
and exits; the workflow commits them and lets bot.py send the alerts.

Only items scoring >= ALERT_THRESHOLD reach Telegram.
"""
import datetime
import json
import os
import pathlib
import re
import sys

import urllib.error
import urllib.request

import anthropic

ROOT = pathlib.Path(__file__).resolve().parent
DATA = ROOT / "data"
MODEL = "claude-opus-5"
ALERT_THRESHOLD = int(os.environ.get("ALERT_THRESHOLD", "8"))


def load(name, default=None):
    p = DATA / name
    return json.loads(p.read_text()) if p.exists() else default


def build_prompt(universe, watchlist, recent_ids, today):
    focus = ", ".join(universe["groups"]["focus"])
    broad = ", ".join(universe["groups"]["sp500_megacap"][:40])
    wl = ", ".join(w["ticker"] for w in watchlist["tickers"])
    return f"""Today is {today}. Search the financial news wires for market-moving news from the last 24 hours.

PRIMARY FOCUS — these matter most:
{focus}

ALSO IN SCOPE — largest S&P 500 and Nasdaq names:
{broad}

The user's watchlist: {wl}

Any other company is in scope only if the news is genuinely large enough to move
the broad market or a whole sector.

Search for: earnings and guidance, M&A, regulatory and antitrust actions, export
controls and chip restrictions, supply deals, CEO changes, analyst actions from
major banks, and macro releases (CPI, PCE, jobs, FOMC) that reprice the market.

For each item, score IMPACT 1-10 — how much it changes what the company earns or
what investors will pay for it, NOT how dramatic the headline sounds:

  9-10  Repricing event. Guidance cut or raise, M&A, export ban, FDA decision,
        surprise CEO exit, Fed surprise. Changes the multi-quarter earnings path.
  7-8   Material. Earnings with a directional guide, major-bank rating change,
        large contract, regulatory probe opened.
  5-6   Notable. In-line earnings, mid-tier analyst action, product launch.
  1-4   Noise. Price recaps, opinion, restated news. DROP these entirely.

Rules:
- direction is for the STOCK, not the tone of the news. Layoffs are often bullish.
- tickers must list every affected name, including read-through. If Nvidia guides
  up, that matters to AVGO, TSM, MU, AMD and ANET too. This is the most valuable
  part of the job — do not list only the subject of the headline.
- why: 2-3 sentences on the MECHANISM. Why does this change earnings or the
  multiple? Never restate the headline.
- Never invent a price, percentage or date. Omit the field if no source states it.
- If sources conflict on a number, say they conflict in `why`.
- Cite a real URL per item.

Already reported, do not repeat these ids: {", ".join(recent_ids[:40]) or "(none)"}

Return ONLY a JSON object, no prose before or after, in a ```json fenced block:

{{"items": [{{"id": "YYYYMMDD-ticker-slug", "time": "ISO8601 UTC",
"headline": "...", "impact": 1-10, "direction": "bullish|bearish|uncertain",
"scope": "company|sector|macro", "tickers": ["..."], "why": "...",
"source": "...", "url": "..."}}]}}

Drop anything scoring below 5. If nothing material happened, return {{"items": []}}."""


def extract_json(text):
    """Pull the JSON object out of the reply, fenced or bare."""
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    if m:
        return json.loads(m.group(1))
    start = text.find("{")
    if start < 0:
        raise ValueError("no JSON object in reply")
    depth, in_str, esc = 0, False, False
    for i, ch in enumerate(text[start:], start):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return json.loads(text[start:i + 1])
    raise ValueError("unbalanced JSON in reply")


GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta"


def _gemini_request(model, key, prompt):
    """One grounded generateContent call. Returns the reply text."""
    url = "%s/models/%s:generateContent?key=%s" % (GEMINI_BASE, model, key)
    body = json.dumps({
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        # Google Search grounding is Gemini's equivalent of the web_search tool
        "tools": [{"google_search": {}}],
        "generationConfig": {"temperature": 0.2, "maxOutputTokens": 32000},
    }).encode()
    req = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=180) as r:
        payload = json.loads(r.read())
    cands = payload.get("candidates") or []
    if not cands:
        raise RuntimeError("gemini returned no candidates: %s"
                           % json.dumps(payload)[:300])
    parts = cands[0].get("content", {}).get("parts", [])
    return "".join(p.get("text", "") for p in parts)


def _gemini_pick_model(key):
    """Ask the API which models exist rather than trusting a hardcoded name."""
    url = "%s/models?key=%s&pageSize=200" % (GEMINI_BASE, key)
    try:
        with urllib.request.urlopen(url, timeout=30) as r:
            models = json.loads(r.read()).get("models", [])
    except Exception as e:                                        # noqa: BLE001
        print("could not list gemini models (%s); using the default" % e)
        return None
    usable = [m["name"].split("/")[-1] for m in models
              if "generateContent" in m.get("supportedGenerationMethods", [])]
    # newest-looking pro first, then flash; both handle this task
    for want in ("pro", "flash"):
        hits = sorted((m for m in usable if want in m and "vision" not in m),
                      reverse=True)
        if hits:
            print("gemini models available: %d; chose %s" % (len(usable), hits[0]))
            return hits[0]
    return usable[0] if usable else None


def run_scan_gemini(prompt):
    key = os.environ["GEMINI_API_KEY"]
    model = os.environ.get("GEMINI_MODEL", "").strip() or _gemini_pick_model(key)
    if not model:
        raise RuntimeError("no usable Gemini model found")
    try:
        return _gemini_request(model, key, prompt)
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:300]
        if e.code in (400, 404):
            # the chosen name may be retired; re-resolve once and retry
            alt = _gemini_pick_model(key)
            if alt and alt != model:
                print("retrying with %s after HTTP %s" % (alt, e.code))
                return _gemini_request(alt, key, prompt)
        raise RuntimeError("gemini HTTP %s: %s" % (e.code, detail))


def run_scan(client, prompt):
    """One streamed request with server-side web search. Handles pause_turn."""
    messages = [{"role": "user", "content": prompt}]
    tools = [{
        "type": "web_search_20260209",
        "name": "web_search",
        "max_uses": 12,
    }]
    text = ""
    for attempt in range(4):                    # cap pause_turn restarts
        with client.messages.stream(
            model=MODEL,
            max_tokens=32000,
            thinking={"type": "adaptive"},
            output_config={"effort": "high"},
            tools=tools,
            messages=messages,
        ) as stream:
            msg = stream.get_final_message()
        text = "".join(b.text for b in msg.content if b.type == "text")
        if msg.stop_reason == "refusal":
            raise RuntimeError("refused: %s" % getattr(msg, "stop_details", None))
        if msg.stop_reason != "pause_turn":
            return text
        messages.append({"role": "assistant", "content": msg.content})
    return text


def scan_with_available_provider(prompt):
    """Anthropic when it is funded, Gemini otherwise. None means skip this run.

    A missing key, an unfunded account and a missing workspace id are all
    configuration states rather than faults, so they return None and the caller
    exits cleanly instead of mailing a failure on every scheduled run.
    """
    has_anthropic = bool(os.environ.get("ANTHROPIC_API_KEY"))
    has_gemini = bool(os.environ.get("GEMINI_API_KEY"))

    if not has_anthropic and not has_gemini:
        print("::notice::No model API key configured. Add GEMINI_API_KEY (free "
              "tier at aistudio.google.com/apikey) or ANTHROPIC_API_KEY as a "
              "repository secret. Skipping.")
        return None

    if has_anthropic:
        workspace = (os.environ.get("ANTHROPIC_WORKSPACE_ID")
                     or os.environ.get("CLAUDE_WORKSPACE_ID") or "").strip()
        headers = {"anthropic-workspace-id": workspace} if workspace else None
        client = anthropic.Anthropic(default_headers=headers)
        try:
            print("scanning with Anthropic (%s)" % MODEL)
            return run_scan(client, prompt)
        except anthropic.BadRequestError as e:
            msg = str(e).lower()
            recoverable = "credit balance" in msg or "workspace-id" in msg
            if not recoverable:
                raise
            reason = ("credit balance too low" if "credit balance" in msg
                      else "identity-linked key without a workspace id")
            if has_gemini:
                print("Anthropic unavailable (%s) — falling back to Gemini"
                      % reason)
            else:
                print("::notice::Anthropic unavailable (%s) and no "
                      "GEMINI_API_KEY is set, so this scan was skipped. Add "
                      "credits, or add a free Gemini key from "
                      "aistudio.google.com/apikey." % reason)
                return None

    print("scanning with Gemini")
    return run_scan_gemini(prompt)


def main():
    if not os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit("ANTHROPIC_API_KEY is not set")

    universe = load("universe.json")
    watchlist = load("watchlist.json", {"tickers": []})
    news = load("news.json", {"items": []})
    state = load("state.json", {"alert_threshold": ALERT_THRESHOLD,
                                "alerted_ids": [], "runs": 0})
    now = datetime.datetime.now(datetime.timezone.utc)
    today = now.strftime("%Y-%m-%d")

    existing = {i["id"]: i for i in news.get("items", [])}
    prompt = build_prompt(universe, watchlist, list(existing)[:40], today)

    text = scan_with_available_provider(prompt)
    if text is None:
        return

    found = extract_json(text).get("items", [])
    print("model returned %d item(s)" % len(found))

    fresh = 0
    for item in found:
        if not isinstance(item, dict) or "id" not in item:
            continue
        if int(item.get("impact", 0)) < 5:
            continue
        item.setdefault("time", now.strftime("%Y-%m-%dT%H:%M:%SZ"))
        item.setdefault("alerted", False)
        if item["id"] not in existing:
            fresh += 1
        existing[item["id"]] = item

    merged = sorted(existing.values(), key=lambda x: x["time"], reverse=True)[:40]
    news.update({"generated_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                 "session_date": today, "items": merged})
    (DATA / "news.json").write_text(json.dumps(news, indent=2) + "\n")

    state["last_run"] = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    state["runs"] = state.get("runs", 0) + 1
    state.setdefault("alert_threshold", ALERT_THRESHOLD)
    (DATA / "state.json").write_text(json.dumps(state, indent=2) + "\n")

    alerting = [i for i in merged
                if i["impact"] >= state["alert_threshold"]
                and i["id"] not in state["alerted_ids"]]
    print("%d new item(s) | %d at or above the %s/10 alert bar"
          % (fresh, len(alerting), state["alert_threshold"]))
    for i in alerting:
        print("  ALERT %s/10  %s" % (i["impact"], i["headline"][:80]))


if __name__ == "__main__":
    main()
