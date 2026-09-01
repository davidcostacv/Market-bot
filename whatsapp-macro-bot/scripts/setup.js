#!/usr/bin/env node
/**
 * Interactive first-run setup: asks for the five credentials, writes .env,
 * then hands over to the doctor to prove they actually work.
 *
 *   npm run setup
 */
import crypto from "node:crypto";
import fs from "node:fs";
import readline from "node:readline/promises";
import { stdin, stdout } from "node:process";

const ENV_FILE = ".env";

const QUESTIONS = [
  {
    key: "WHATSAPP_TOKEN",
    label: "WhatsApp access token",
    help: "Meta app → WhatsApp → API Setup. Use a permanent System User token; the temporary one dies in 24h.",
    required: true,
  },
  {
    key: "WHATSAPP_PHONE_NUMBER_ID",
    label: "Phone number ID",
    help: "Same API Setup page — the long number under 'From'. Not the phone number itself.",
    required: true,
  },
  {
    key: "WHATSAPP_APP_SECRET",
    label: "App secret",
    help: "Meta app → Settings → Basic → App Secret. Used to verify Meta's webhook signatures.",
    required: true,
  },
  {
    key: "WHATSAPP_VERIFY_TOKEN",
    label: "Webhook verify token",
    help: "Any random string. You paste the same one into Meta's webhook config.",
    required: true,
    generate: () => crypto.randomBytes(16).toString("hex"),
  },
  {
    key: "ALLOWED_NUMBERS",
    label: "Your WhatsApp number",
    help: "International format, digits only (e.g. 34600111222). Only these numbers get answered.",
    required: true,
    clean: (value) => value.replace(/[^\d,]/g, ""),
  },
  {
    key: "ANTHROPIC_API_KEY",
    label: "Anthropic API key",
    help: "console.anthropic.com → API keys. This is what estimates the macros.",
    required: true,
  },
  {
    key: "DEFAULT_TIMEZONE",
    label: "Your timezone",
    help: "Decides when your day rolls over.",
    fallback: Intl.DateTimeFormat().resolvedOptions().timeZone || "Europe/Madrid",
  },
  { key: "DEFAULT_KCAL_GOAL", label: "Daily calories", fallback: "2400" },
  { key: "DEFAULT_PROTEIN_GOAL", label: "Daily protein (g)", fallback: "180" },
  { key: "DEFAULT_CARBS_GOAL", label: "Daily carbs (g)", fallback: "250" },
  { key: "DEFAULT_FAT_GOAL", label: "Daily fat (g)", fallback: "70" },
  {
    key: "DAILY_SUMMARY_HOUR",
    label: "Hour for the nightly recap (0-23, blank for none)",
    fallback: "21",
    allowEmpty: true,
  },
];

/** Parse an existing .env so re-running setup keeps what you already entered. */
function readExisting() {
  if (!fs.existsSync(ENV_FILE)) return {};
  const values = {};
  for (const line of fs.readFileSync(ENV_FILE, "utf8").split("\n")) {
    const match = /^\s*([A-Z0-9_]+)\s*=\s*(.*)\s*$/i.exec(line);
    if (match) values[match[1]] = match[2].trim();
  }
  return values;
}

const mask = (value) => (value.length > 12 ? `${value.slice(0, 6)}…${value.slice(-4)}` : "set");

const rl = readline.createInterface({ input: stdin, output: stdout });

console.log("\nMacro bot setup — ctrl-c to bail out, nothing is written until the end.\n");

const existing = readExisting();
const answers = { ...existing };

for (const question of QUESTIONS) {
  const current = existing[question.key];
  const suggested = current || question.generate?.() || question.fallback || "";

  if (question.help) console.log(`\n  ${question.help}`);
  const shown = current && question.key.includes("TOKEN") ? mask(current) : suggested;
  const prompt = shown ? `${question.label} [${shown}]: ` : `${question.label}: `;

  let value = (await rl.question(prompt)).trim();
  if (!value) value = suggested;
  if (question.clean) value = question.clean(value);

  while (question.required && !value) {
    value = (await rl.question(`  ${question.label} is required: `)).trim();
    if (question.clean) value = question.clean(value);
  }
  if (!value && !question.allowEmpty && !question.required) continue;

  answers[question.key] = value;
}

rl.close();

answers.PORT ||= "3000";
answers.DB_PATH ||= "./data/macros.db";
answers.ANTHROPIC_MODEL ||= "claude-opus-5";
answers.GRAPH_API_VERSION ||= "v21.0";

const body = Object.entries(answers)
  .map(([key, value]) => `${key}=${value}`)
  .join("\n");
fs.writeFileSync(ENV_FILE, `${body}\n`, { mode: 0o600 });

console.log(`\nWrote ${ENV_FILE} (chmod 600 — it holds your secrets, keep it out of git).`);
console.log("\nChecking that everything actually works…\n");

const { runDoctor } = await import("./doctor.js");
const ok = await runDoctor();

console.log(
  ok
    ? "\nNext: expose the server over HTTPS and paste the callback URL into Meta.\n" +
        `Your verify token is: ${answers.WHATSAPP_VERIFY_TOKEN}\n` +
        "See README.md → 'Point Meta at it'.\n"
    : "\nFix the ❌ lines above, then run `npm run doctor` again.\n",
);
process.exit(ok ? 0 : 1);
