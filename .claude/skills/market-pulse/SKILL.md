---
name: market-pulse
description: Scan for market-moving news across the monitored ticker universe, score each item for impact, update the live dashboard, and push a notification only for high-impact items. Use when running a scheduled market pulse check, when the user asks what is moving the market right now, or when invoked as /market-pulse.
---

# Market Pulse

A scheduled scan that turns raw news into a scored, deduplicated, directional feed.
Deterministic scaffolding; exactly one step requires judgment (scoring).

Repo root for this skill: `market-pulse/`

## Run procedure

Execute these in order. Do not skip steps, do not add steps.

### 1. Load state

```
cd market-pulse/data
cat state.json universe.json watchlist.json
```

`state.json.alerted_ids` is the dedupe ledger — never alert twice on the same `id`.

### 2. Gather

Run these WebSearch queries (the shell cannot reach market data hosts — the
network policy returns 403 on CONNECT, so **WebSearch/WebFetch is the only
data path**; do not attempt curl against finance APIs):

1. `stock market news today <today's date> movers`
2. `premarket movers <today's date> earnings guidance upgrade downgrade`
3. `<any watchlist ticker with an event in the next 3 days> news`
4. On Fed/CPI/PCE/jobs days: `<release name> <date> reaction`

Prefer reporting from the last 24 hours. Use WebFetch on a promising URL when a
search snippet is too thin to score.

### 3. Score

For each candidate headline assign **impact 1-10**. This is the judgment step.

| Score | Meaning |
|---|---|
| 9-10 | Repricing event. Guidance cut/raise, M&A, FDA decision, surprise CEO exit, Fed surprise, war/supply shock. Changes the multi-quarter earnings path. |
| 7-8  | Material. Earnings beat/miss with a directional guide, major-bank rating change, large contract, regulatory probe opened. |
| 5-6  | Notable. In-line earnings, mid-tier analyst action, product launch, sector sympathy. |
| 1-4  | Noise. Price-action recaps, opinion, restated news, "stock hits 52-week high". |

Also assign:
- `direction`: `bullish` | `bearish` | `uncertain` — **for the stock**, not for the news' tone. A layoff round is often bullish for the stock.
- `scope`: `company` | `sector` | `macro`
- `tickers`: every monitored ticker with a real read-through, not just the subject. This is where most of the value is — the NVDA guide matters to AVGO/TSM/ANET.
- `why`: 2-3 sentences on the *mechanism* — why this changes what the company earns or what investors will pay for it. Never restate the headline.

**Rules.** Never invent a price, percentage, or date; leave the field blank if
it is not in a source. Never score on how dramatic the headline sounds. Cite a
real URL per item. Drop anything scoring below 4.

### 4. Write

Update in `market-pulse/data/`:
- `news.json` — merge new items, keep the last 40, newest first
- `movers.json` — rebuild the catalyst board from items scoring >= 6
- `calendar.json` — only when a date is confirmed or a new event appears; set `confirmed` honestly
- `state.json` — bump `last_run`, `runs`, append new `alerted_ids`

### 5. Notify

Send **one** `PushNotification` only if an item is new (id not in `alerted_ids`)
**and** `impact >= state.json.alert_threshold` (currently 8).

Format: `<TICKER> <direction arrow> <headline clause> — <impact>/10`
Example: `NVDA ^ revenue doubles, guides to 2028 — 10/10`

If several fire at once, send one combined line. If none qualify, send nothing —
silence is the correct output for a quiet session.

### 6. Commit and push — do this BEFORE publishing

**Git is the source of truth, not the artifact.** The next scan and the Telegram
bot both read `market-pulse/data/*.json` from the branch. An artifact republish
alone is a dead end: your findings vanish on the next run, which re-derives from
whatever the branch still says.

```
git add market-pulse/data
git commit -m "Scan <UTC time>: <n> new items, <n> alerts"
git push origin HEAD:claude/realtime-market-news-agent-w50nuu
```

If the push is rejected, another writer got there first — do not discard your
work and do not force:

```
git pull --rebase origin claude/realtime-market-news-agent-w50nuu
# resolve by UNION: keep every item from both sides, dedupe by event not by id
git push origin HEAD:claude/realtime-market-news-agent-w50nuu
```

Then verify it actually landed. A silent push failure is the exact bug this
step exists to prevent:

```
git fetch -q origin claude/realtime-market-news-agent-w50nuu
git log --oneline -1 origin/claude/realtime-market-news-agent-w50nuu
```

If your commit is not the tip, the run has failed. Say so in your final message
rather than reporting success.

### 7. Publish

```
cd market-pulse && python3 build.py
```

Then republish to the **existing** artifact URL in `market-pulse/ARTIFACT_URL`:
read the artifact first (`action: "read"`), carry over any watchlist the viewer
edited in the page, then publish with `url` set to that URL. Never publish
without `url` — that creates a duplicate dashboard at a new link.

If the publish is refused because a newer version is live, that is another
writer, not an error. Read the live version, take the **union** of its items and
yours, rebuild, and publish again. Never resend your own content unchanged over
someone else's newer scan.

## Judgment guardrails

- A stalled disinflation print outranks any single company's earnings.
- Two sources agreeing is worth more than one source being emphatic.
- If a claim appears only in aggregator content with no primary attribution, drop it.
- Contradicting your own earlier item is fine — say so in `why` and rescore.
- Dedupe by **event**, never by id. Two scans will give the same story different
  ids. When you find a duplicate, keep the better-sourced version (the one with
  a figure, a named party, or a direct quote) and fold the other's read-through
  tickers into it.
- When sources conflict on a number, say they conflict in `why` rather than
  silently picking the one you found first.
