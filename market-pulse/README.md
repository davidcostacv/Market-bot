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

## Known limit: no live price feed

This environment's network policy returns 403 on CONNECT to finance hosts, so
the shell cannot reach a quote API. Everything here is news-derived: the
catalyst board shows a percentage **only** where a cited article reported one,
and blank otherwise, rather than inventing a number.

To add real quotes, allow a data host in the environment's network policy, put
the key in an environment variable, and add a fetch step to `build.py` that
writes a `quotes.json` the template can read. The dashboard already reserves the
slot — `observed_move` on each board card.

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
| `/report` | Full scored report from the newest scan |
| `/top` | Only alert-grade items (>= 8/10) |
| `/board` | Likely up / likely down |
| `/calendar` | Next 14 earnings and macro events |
| `/watchlist` | Tracked tickers, with feed hit counts |
| `/help` | Command list |

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
