# Market Pulse

A scheduled scan that turns market news into a scored, deduplicated, directional
feed, republishes a live dashboard, and pushes a notification only when
something genuinely matters.

**Dashboard:** https://claude.ai/code/artifact/481f3a3c-3571-46f0-9da8-2e83c9116871

## Why a workflow, not an open-ended agent

The task splits cleanly into two halves, and they want opposite things.

Gathering, deduplicating, rendering, and notifying are **mechanical** — they must
happen the same way every run, on a schedule, whether or not anything
interesting happened. Deciding *"does this headline change what the company
earns, or what investors will pay for it?"* is **judgment** — no rule set gets it
right, because the same headline means different things in different regimes.

So this is a workflow with exactly one agentic step: fixed cadence, fixed source
list, fixed scoring rubric, fixed output schema, and one place where a model
reads the news and scores it. That keeps the run auditable (every item carries
its score, its reasoning, and its source URL) and cheap, while still catching
the read-through that pure keyword matching always misses — the reason the
Nvidia guide shows up on AVGO, TSM, and ANET rather than only on NVDA.

A fully autonomous agent would drift: different sources each run, no dedupe
ledger, no guarantee it runs at all on a quiet morning. A pure rules workflow
would score by keyword and drown you in noise.

## Layout

```
market-pulse/
├── data/
│   ├── universe.json    97 monitored tickers (S&P 500 mega-caps + your groups)
│   ├── news.json        scored headlines, newest 40
│   ├── movers.json      catalyst board, rebuilt each run from items >= 6
│   ├── calendar.json    earnings + macro events, with a confirmed flag
│   ├── watchlist.json   seed for the Watchlist tab
│   └── state.json       last run, alert threshold, dedupe ledger
├── template.html        the dashboard, with a __PAYLOAD__ slot
├── build.py             data/*.json + template.html -> dashboard.html
└── dashboard.html       generated; do not hand-edit
```

The agent's run procedure lives in `.claude/skills/market-pulse/SKILL.md`.

## Running it

```bash
# by hand, any time
/market-pulse

# render only, after editing data by hand
python3 market-pulse/build.py
```

The scheduled Routine fires hourly on weekdays, 08:00–17:00 ET.

## Impact scoring

| Score | Meaning |
|---|---|
| 9-10 | Repricing event — guidance change, M&A, FDA, CEO exit, Fed surprise |
| 7-8  | Material — earnings with a directional guide, major-bank rating change |
| 5-6  | Notable — in-line earnings, product launch, sector sympathy |
| 1-4  | Noise — dropped |

Notifications fire at **>= 8** only, once per item ever (`state.json.alerted_ids`).
Change the bar by editing `alert_threshold` in `state.json`.

## Where prices come from, and where they don't

**The Telegram bot can have live prices, with one free API key.** `/stocks` and
`/holdings` fetch at the moment you ask.

Free keyless sources do not work from a GitHub Actions runner. Probed directly
rather than assumed:

| Source | Result from a runner |
|---|---|
| Yahoo v8 chart | HTTP 429, rate-limits datacenter IP ranges |
| Stooq `q/l` | HTTP 404, endpoint retired |
| Stooq `q/d/l` | HTTP 200 but a JavaScript bot-verification challenge |

Neither is fixable with headers or retries. Finnhub's free tier does work from a
runner, returns the previous close directly, and allows 60 calls/minute — ample
for a 19-ticker watchlist. Get a key at finnhub.io/register and store it as the
`FINNHUB_API_KEY` repository secret. Without it, `/stocks` says exactly that
instead of failing silently; every news command works regardless.

**The dashboard and the scan do not.** They run in the Claude container, whose
network policy returns 403 on CONNECT to finance hosts. So the catalyst board
shows a percentage only where a cited article reported one, and blank
otherwise, rather than inventing a number.

That asymmetry is deliberate rather than an oversight: ask the bot for prices,
read the dashboard for what the news means. If the container's network policy
ever allows a data host, `market-pulse/telegram/quotes.py` is importable as-is
and `observed_move` on each board card is the slot to fill.

Quote failures degrade rather than break: a ticker that will not resolve shows
`—` with the reason, and the rest of the table still renders.

## Watchlist

Edits in the Watchlist tab save to that browser's `localStorage` and survive
dashboard republishes. **Copy list for the agent** puts the symbols on your
clipboard; paste them back here and they get written into `watchlist.json` as
the new seed.

## Telegram bot

`t.me/Marketnews_catalystbot` — free, no paid tier needed.

The bot **cannot run in the Claude container**: that environment's network
policy returns 403 on CONNECT to `api.telegram.org`, and the container is
reclaimed when the session ends. It runs on GitHub Actions instead
(`.github/workflows/market-pulse-bot.yml`), which reaches Telegram fine and is
free for public repositories.

```
Claude scan (hourly)  ->  commits scored JSON to this branch
GitHub Actions (5 min) ->  reads that JSON, answers commands, pushes alerts
```

### Commands

| Command | Does |
|---|---|
| `/stocks` | Day % change across your watchlist. `/stocks AAPL NVDA` for ad-hoc tickers |
| `/holdings` | Your positions: live price, day move, value, P/L |
| `/hold TICKER SHARES [COST]` | Add or update a position, e.g. `/hold NVDA 25 178.40` |
| `/unhold TICKER` | Remove a position |
| `/report` | Full scored report from the newest scan |
| `/top` | Only alert-grade items (>= 8/10) |
| `/board` | Likely up / likely down |
| `/calendar` | Next 14 earnings and macro events |
| `/watchlist` | Tracked tickers, with feed hit counts |
| `/help` | Command list |

### Secrets

| Secret | Needed for | Notes |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` or `TELEGRAM_TOKEN` | The bot | Either name works |
| `GEMINI_API_KEY` or `GOOGLE_API_KEY` | The scan | Free tier at aistudio.google.com/apikey, no card. Uses Google Search grounding |
| `ANTHROPIC_API_KEY` / `CLAUDE_API_KEY` / `ANTHROPIC_KEY` / `CLAUDE_KEY` / `ANTHROPIC_TOKEN` | The scan | Any of the five; the gate resolves whichever is set and logs which names are populated by length |
| `ANTHROPIC_WORKSPACE_ID` or `CLAUDE_WORKSPACE_ID` | The scan, **only for identity-linked keys** | From the Anthropic Console under Settings > Workspaces, `wrkspc_01...`. Without it such a key returns `400 anthropic-workspace-id is required`. A plain workspace key needs no header. |
| `FINNHUB_API_KEY` | `/stocks` and `/holdings` | Optional; news commands work without it |

**Either provider works.** Anthropic is used when it is configured and funded;
the scan falls back to Gemini automatically when the Anthropic account is out of
credits or its key is identity-linked without a workspace id. With only a Gemini
key it goes straight there. The scoring rubric, merge, alert threshold and
Telegram formatting are provider-agnostic, so switching changes one function.

Note that Gemini's free tier permits Google to use prompts and responses to
improve their products. That is acceptable for scanning public financial news;
it would not be for anything private.

Both workflows skip quietly when their key is missing, so a half-configured repo
does not mail a failure on every scheduled run.

### Setup

1. **Rotate the token.** BotFather -> `/revoke` -> pick the bot. The old token
   was shared in plain text and must be considered burned.
2. Repo **Settings -> Secrets and variables -> Actions -> New secret**,
   named `TELEGRAM_BOT_TOKEN`, value = the new token.
3. Merge this branch to `main`. GitHub only fires `schedule` from the default
   branch, so the workflow will not run on a feature branch. It reads market
   data from `DATA_BRANCH`, so the Claude scan can keep pushing here.
4. Message the bot `/start` — that registers your chat id for alerts.
5. Optional: Actions tab -> Market Pulse Telegram bot -> Run workflow ->
   mode `setup`, to populate the blue command menu.

The token is never committed; `bot.py` reads it from the environment. Until the
secret exists the scheduled run skips quietly with a notice rather than failing,
so you do not get a failure email every five minutes while setup is pending.

### Running it from your own machine

`.env` is for local runs only. **GitHub Actions cannot read it** — the runner
receives only committed files, and this repository is public so a committed
token would be world-readable. Actions reads the `TELEGRAM_BOT_TOKEN`
repository secret; the two are separate and you need the secret regardless.

```bash
cp .env.example .env          # then paste your token into .env
python3 market-pulse/telegram/bot.py poll     # answer pending commands
python3 market-pulse/telegram/bot.py alerts   # push new alert-grade items
python3 market-pulse/telegram/bot.py setup    # register the command menu
```

A real environment variable always beats `.env`, so CI is never overridden by a
stray file. `.env` is gitignored and must stay that way — if you ever commit
one, revoke the token in BotFather immediately, because rewriting history does
not un-publish it.

This is also the quickest way to prove a token is valid, independently of
whether the Actions secret is wired: if `setup` works locally, the token is
good and the problem is the secret.

### Cost

Free. Telegram's Bot API has no fee, and Actions minutes are unlimited on
public repositories. On a private repo the 5-minute cadence would exceed the
2,000 free minutes/month — drop to `*/15` during market hours if you ever make
this repo private.
