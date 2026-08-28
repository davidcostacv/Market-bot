# WhatsApp Macro Bot

Text the bot what you ate. It replies with the macros for that meal and your
running total for the day. It runs 24/7, resets at your local midnight, and
sends a recap every evening.

```
You   2 eggs, 2 slices of sourdough with butter, flat white
Bot   Logged
      • 2 large scrambled eggs — 180 kcal · 13P 1C 13F
      • 2 slices sourdough — 240 kcal · 8P 46C 2F
      • 1 tbsp butter — 100 kcal · 0P 0C 11F
      • 1 cup flat white — 120 kcal · 7P 10C 6F

      Today so far
      🔥 ▓▓▓░░░░░░░ 640/2400 kcal · 1760 kcal left
      🥩 ▓▓░░░░░░░░ 28/180g · 152g left
      🍚 ▓▓░░░░░░░░ 57/250g · 193g left
      🥑 ▓▓▓▓░░░░░░ 32/70g · 38g left
```

Send a **photo** of the plate and it estimates from the picture. Add a caption
("half of this") to correct the portion.

## How it works

| Piece | What it does |
|---|---|
| WhatsApp Cloud API | Meta's official webhook. Your number, no QR codes to re-scan. |
| `POST /webhook` | Verifies Meta's signature, acks in milliseconds, then works in the background. |
| Claude (`claude-opus-5`) | Turns "chicken burrito bowl" into per-item kcal/protein/carbs/fat via structured outputs. |
| Food memory (SQLite) | Every logged food is remembered. Log the same thing again and it reuses the exact numbers — instantly, and with no API call. |
| Day buckets | Entries are stamped with *your* local date, so the day rolls over at your midnight, not the server's. |
| Nightly recap | A cron tick every 15 min asks "is it 21:00 where this user lives?" and sends the day's summary once. |

### Commands

| Message | Result |
|---|---|
| anything else | logged as food |
| a photo | logged from the image |
| `total` | today's running totals |
| `list` | every item logged today |
| `yesterday` | yesterday's day sheet |
| `week` | last 7 days plus daily averages |
| `undo` | removes the last thing logged |
| `clear` | wipes today |
| `goals 2400 180 250 70` | kcal, protein, carbs, fat (also `goals kcal=2400 p=180`) |
| `tz Europe/Madrid` | set your timezone |
| `forget flat white` | drop a remembered food so it gets re-estimated |
| `help` | the list above |

## Setup

### 1. WhatsApp Cloud API

1. Create a Meta app at <https://developers.facebook.com/apps> → **Business** → add the **WhatsApp** product.
2. **API Setup** gives you a test number and a temporary token. Note the **Phone number ID**.
3. For 24/7 use, swap the 24-hour token for a permanent one: **Business Settings → Users → System Users** → add a system user with the `whatsapp_business_messaging` permission → **Generate token** (no expiry).
4. Copy the **App Secret** from **App Settings → Basic**.
5. Add your own number under **API Setup → To** so the test number can message you.

### 2. Run it

```bash
cp .env.example .env      # fill in the values
npm install
npm start
```

`ALLOWED_NUMBERS` is the allowlist — anyone not on it is ignored. Leave it empty
only if you really want the bot to answer strangers.

### 3. Point Meta at it

The webhook must be reachable over HTTPS. For local development:

```bash
npx localtunnel --port 3000     # or: cloudflared tunnel --url http://localhost:3000
```

Then in the Meta app: **WhatsApp → Configuration → Edit**

- Callback URL: `https://your-host/webhook`
- Verify token: the same string as `WHATSAPP_VERIFY_TOKEN`
- Subscribe to the **messages** field.

Send yourself a `help` to confirm the round trip.

### 4. Deploy for real (24/7)

Any host that runs a container works. Fly.io is included because the free
allowance covers this and a volume keeps the database:

```bash
fly launch --no-deploy
fly volumes create macro_data --size 1
fly secrets set WHATSAPP_TOKEN=... WHATSAPP_PHONE_NUMBER_ID=... \
                WHATSAPP_APP_SECRET=... WHATSAPP_VERIFY_TOKEN=... \
                ANTHROPIC_API_KEY=... ALLOWED_NUMBERS=34600111222
fly deploy
```

`fly.toml` pins `auto_stop_machines = false` and `min_machines_running = 1` — a
suspended machine misses webhooks and the nightly recap.

Then update the Meta callback URL to `https://your-app.fly.dev/webhook`.

Two GitHub Actions back this up, in `.github/workflows/`:

- **Macro bot CI** — runs the tests and boots the server on every push.
- **Macro bot keepalive** — pings `/health` every 10 minutes. Set the
  `BOT_HEALTH_URL` repo secret to `https://your-app.fly.dev/health`; without it
  the job skips. A failing run is your outage alarm.

## Cost

One meal ≈ one short Claude call. On `claude-opus-5` ($5 / $25 per million
input / output tokens) a day of logging is a couple of cents, and repeat foods
come out of the local memory for free. Set `ANTHROPIC_MODEL=claude-haiku-4-5`
in `.env` if you would rather trade some accuracy for a smaller bill.

## Notes

- The estimates are estimates. They are consistent day to day — which is what
  makes a cut or a bulk trackable — but they are not a food scale.
- `data/macros.db` is the whole state. Back it up (`fly ssh sftp get /data/macros.db`)
  and you keep your history.
- Meta redelivers webhooks it does not get a fast `200` for; message ids are
  claimed once in SQLite so a retry never logs a meal twice.

## Layout

```
src/config.js     env loading and validation
src/index.js      express server, webhook verification, message routing
src/handler.js    commands vs. food logs
src/macros.js     Claude structured-output extraction + food memory
src/db.js         SQLite schema and queries
src/scheduler.js  nightly recap in each user's timezone
src/whatsapp.js   Graph API send / media download / signature check
src/format.js     the WhatsApp replies
src/time.js       local-day maths
test/             node:test suite (npm test)
```
