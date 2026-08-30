import express from "express";
import { assertConfig, config } from "./config.js";
import { claimMessage } from "./db.js";
import { handleMessage } from "./handler.js";
import { startScheduler } from "./scheduler.js";
import { downloadMedia, markRead, sendText, verifySignature } from "./whatsapp.js";

assertConfig();

const app = express();
// The signature is computed over the exact bytes Meta sent, so keep the raw body.
app.use(express.json({ verify: (req, _res, buf) => (req.rawBody = buf) }));

app.get("/health", (_req, res) => res.json({ ok: true, uptime: process.uptime() }));

// Meta calls this once when you register the webhook URL.
app.get("/webhook", (req, res) => {
  const mode = req.query["hub.mode"];
  const token = req.query["hub.verify_token"];
  if (mode === "subscribe" && token === config.whatsapp.verifyToken) {
    return res.status(200).send(req.query["hub.challenge"]);
  }
  return res.sendStatus(403);
});

app.post("/webhook", (req, res) => {
  if (!verifySignature(req.rawBody, req.get("x-hub-signature-256"))) {
    console.warn("[webhook] rejected a request with a bad signature");
    return res.sendStatus(401);
  }
  // Ack immediately — Meta retries anything it does not hear back about in
  // ~20 seconds, and asking Claude for macros takes longer than that.
  res.sendStatus(200);
  processWebhook(req.body).catch((error) =>
    console.error("[webhook] processing failed:", error),
  );
});

const isAllowed = (phone) =>
  config.allowedNumbers.length === 0 || config.allowedNumbers.includes(phone);

async function processWebhook(payload) {
  for (const entry of payload?.entry ?? []) {
    for (const change of entry.changes ?? []) {
      for (const message of change.value?.messages ?? []) {
        await handleIncoming(message);
      }
    }
  }
}

async function handleIncoming(message) {
  const phone = String(message.from || "").replace(/\D/g, "");
  if (!phone) return;

  if (!isAllowed(phone)) {
    console.warn(`[webhook] ignoring message from unlisted number ${phone}`);
    return;
  }
  // Meta redelivers on any hiccup; without this a retry logs the meal twice.
  if (!claimMessage(message.id)) {
    console.log(`[webhook] duplicate delivery of ${message.id}, skipped`);
    return;
  }

  await markRead(message.id);

  try {
    let reply;
    if (message.type === "text") {
      reply = await handleMessage({ phone, text: message.text?.body ?? "" });
    } else if (message.type === "image") {
      const image = await downloadMedia(message.image.id);
      reply = await handleMessage({ phone, text: message.image.caption ?? "", image });
    } else {
      reply =
        "I can read text and photos of food. Send me what you ate, or `/help` for the commands.";
    }
    await sendText(phone, reply);
  } catch (error) {
    console.error(`[bot] failed handling ${message.id}:`, error);
    await sendText(
      phone,
      "Something went wrong working that out. Try again in a moment — nothing was logged.",
    ).catch(() => {});
  }
}

const server = app.listen(config.port, () => {
  console.log(`[bot] listening on :${config.port}`);
  startScheduler();
});

for (const signal of ["SIGTERM", "SIGINT"]) {
  process.on(signal, () => server.close(() => process.exit(0)));
}
