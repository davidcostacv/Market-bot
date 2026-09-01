import fs from "node:fs";

// Minimal .env loader so the bot runs without an extra dependency.
// Real deployments (Fly, Render, Railway) inject env vars directly and this is a no-op.
function loadDotEnv(file = ".env") {
  if (!fs.existsSync(file)) return;
  for (const line of fs.readFileSync(file, "utf8").split("\n")) {
    const match = /^\s*([A-Z0-9_]+)\s*=\s*(.*)\s*$/i.exec(line);
    if (!match) continue;
    const [, key, rawValue] = match;
    if (process.env[key] !== undefined) continue;
    process.env[key] = rawValue.replace(/^["']|["']$/g, "").replace(/\s+#.*$/, "").trim();
  }
}

loadDotEnv();

function num(value, fallback) {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : fallback;
}

export const config = {
  port: num(process.env.PORT, 3000),
  dbPath: process.env.DB_PATH || "./data/macros.db",

  whatsapp: {
    token: process.env.WHATSAPP_TOKEN || "",
    phoneNumberId: process.env.WHATSAPP_PHONE_NUMBER_ID || "",
    appSecret: process.env.WHATSAPP_APP_SECRET || "",
    verifyToken: process.env.WHATSAPP_VERIFY_TOKEN || "",
    graphVersion: process.env.GRAPH_API_VERSION || "v21.0",
  },

  // Digits only, so "+34 600 111 222" and "34600111222" compare equal.
  allowedNumbers: (process.env.ALLOWED_NUMBERS || "")
    .split(",")
    .map((n) => n.replace(/\D/g, ""))
    .filter(Boolean),

  anthropic: {
    apiKey: process.env.ANTHROPIC_API_KEY || "",
    model: process.env.ANTHROPIC_MODEL || "claude-opus-5",
  },

  defaultTimezone: process.env.DEFAULT_TIMEZONE || "Europe/Madrid",
  dailySummaryHour:
    process.env.DAILY_SUMMARY_HOUR === "" ? null : num(process.env.DAILY_SUMMARY_HOUR, 21),

  // Free-form messages are refused more than 24h after the user's last one, so
  // the recap needs an approved template to be delivered reliably. Without one
  // the recap still goes out, but only on days you have already messaged.
  dailySummaryTemplate: process.env.DAILY_SUMMARY_TEMPLATE || "",
  dailySummaryTemplateLang: process.env.DAILY_SUMMARY_TEMPLATE_LANG || "en",

  goals: {
    kcal: num(process.env.DEFAULT_KCAL_GOAL, 2400),
    protein: num(process.env.DEFAULT_PROTEIN_GOAL, 180),
    carbs: num(process.env.DEFAULT_CARBS_GOAL, 250),
    fat: num(process.env.DEFAULT_FAT_GOAL, 70),
  },
};

export function assertConfig() {
  const missing = [];
  if (!config.whatsapp.token) missing.push("WHATSAPP_TOKEN");
  if (!config.whatsapp.phoneNumberId) missing.push("WHATSAPP_PHONE_NUMBER_ID");
  if (!config.whatsapp.verifyToken) missing.push("WHATSAPP_VERIFY_TOKEN");
  if (!config.whatsapp.appSecret) missing.push("WHATSAPP_APP_SECRET");
  if (!config.anthropic.apiKey) missing.push("ANTHROPIC_API_KEY");
  if (missing.length) {
    throw new Error(
      `Missing required environment variables: ${missing.join(", ")}. Copy .env.example to .env and fill it in.`,
    );
  }
}
