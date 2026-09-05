#!/usr/bin/env node
/**
 * Checks every moving part and says exactly what to fix.
 *
 *   npm run doctor              # config, Claude, WhatsApp
 *   npm run doctor -- --send    # also sends you a real WhatsApp message
 *
 * Run it after setup, after a deploy, or any time the bot goes quiet.
 */
import fs from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";
import Anthropic from "@anthropic-ai/sdk";
import { config } from "../src/config.js";

const results = [];
const pass = (name, detail) => results.push({ ok: true, name, detail });
const fail = (name, detail, fix) => results.push({ ok: false, name, detail, fix });
const warn = (name, detail, fix) => results.push({ ok: null, name, detail, fix });

function checkConfig() {
  const required = {
    WHATSAPP_TOKEN: config.whatsapp.token,
    WHATSAPP_PHONE_NUMBER_ID: config.whatsapp.phoneNumberId,
    WHATSAPP_APP_SECRET: config.whatsapp.appSecret,
    WHATSAPP_VERIFY_TOKEN: config.whatsapp.verifyToken,
    ANTHROPIC_API_KEY: config.anthropic.apiKey,
  };
  const missing = Object.entries(required)
    .filter(([, value]) => !value)
    .map(([key]) => key);

  if (missing.length) {
    fail("Configuration", `missing ${missing.join(", ")}`, "Run `npm run setup`, or set them in your host's secrets.");
    return false;
  }
  pass("Configuration", "all required variables are set");

  if (!config.allowedNumbers.length) {
    warn(
      "Allowlist",
      "ALLOWED_NUMBERS is empty — the bot will answer anyone who finds the number",
      "Set ALLOWED_NUMBERS to your own number.",
    );
  } else {
    pass("Allowlist", `${config.allowedNumbers.length} number(s) allowed`);
  }
  return true;
}

function checkDatabase() {
  const dir = path.dirname(path.resolve(config.dbPath));
  try {
    fs.mkdirSync(dir, { recursive: true });
    fs.accessSync(dir, fs.constants.W_OK);
    pass("Database", `${config.dbPath} is writable`);
  } catch (error) {
    fail("Database", error.message, `Make ${dir} writable, or point DB_PATH somewhere you can write.`);
  }
}

async function checkClaude() {
  const client = new Anthropic({ apiKey: config.anthropic.apiKey });
  try {
    // models.retrieve costs nothing and still proves the key is live.
    const model = await client.models.retrieve(config.anthropic.model);
    pass("Claude API", `key works, ${model.id} is available`);
  } catch (error) {
    if (error instanceof Anthropic.AuthenticationError) {
      fail("Claude API", "the API key was rejected", "Check ANTHROPIC_API_KEY at console.anthropic.com.");
    } else if (error instanceof Anthropic.NotFoundError) {
      fail(
        "Claude API",
        `the key works but ${config.anthropic.model} is not available to it`,
        "Set ANTHROPIC_MODEL to a model your account can use.",
      );
    } else if (error instanceof Anthropic.APIConnectionError) {
      fail("Claude API", "could not reach api.anthropic.com", "Check outbound network access from this host.");
    } else {
      fail("Claude API", error.message, "");
    }
  }
}

async function checkWhatsApp({ send }) {
  const { getPhoneNumberInfo, sendText, OUTSIDE_24H_WINDOW } = await import("../src/whatsapp.js");

  let info;
  try {
    info = await getPhoneNumberInfo();
    pass(
      "WhatsApp number",
      `${info.display_phone_number}${info.verified_name ? ` (${info.verified_name})` : ""}` +
        `${info.quality_rating ? ` · quality ${info.quality_rating}` : ""}`,
    );
  } catch (error) {
    if (error.code === 190) {
      fail(
        "WhatsApp token",
        "the access token is invalid or expired",
        "Generate a permanent System User token: Business Settings → Users → System Users → Generate token, with whatsapp_business_messaging.",
      );
    } else if (error.status === 404) {
      fail(
        "WhatsApp number",
        "no phone number with that ID",
        "WHATSAPP_PHONE_NUMBER_ID is the long ID under 'From' on the API Setup page, not the phone number.",
      );
    } else {
      fail("WhatsApp API", error.message, "");
    }
    return;
  }

  if (!send) {
    warn("Test message", "skipped", "Run `npm run doctor -- --send` to have the bot message you.");
    return;
  }

  const to = config.allowedNumbers[0];
  if (!to) {
    fail("Test message", "no number in ALLOWED_NUMBERS to send to", "Set ALLOWED_NUMBERS first.");
    return;
  }

  try {
    await sendText(to, "✅ Macro bot is connected. Send `/help` to get started.");
    pass("Test message", `delivered to ${to}`);
  } catch (error) {
    if (error.code === OUTSIDE_24H_WINDOW) {
      warn(
        "Test message",
        "refused — you have not messaged the bot in the last 24 hours",
        `This is normal and not a bug. Message the bot from ${to} first, then re-run.`,
      );
    } else if (error.code === 131030) {
      fail(
        "Test message",
        `${to} is not on the app's recipient allowlist`,
        "While the app is in development mode, add the number under WhatsApp → API Setup → 'To'.",
      );
    } else {
      fail("Test message", error.message, "");
    }
  }
}

function report() {
  const icon = { true: "✅", false: "❌", null: "⚠️ " };
  for (const result of results) {
    console.log(`${icon[String(result.ok)]} ${result.name}: ${result.detail}`);
    if (!result.ok && result.fix) console.log(`   → ${result.fix}`);
  }
  const failures = results.filter((r) => r.ok === false).length;
  console.log(
    failures
      ? `\n${failures} problem${failures === 1 ? "" : "s"} to fix before the bot will work.`
      : "\nAll good. Start it with `npm start`.",
  );
  return failures === 0;
}

export async function runDoctor({ send = false } = {}) {
  results.length = 0;
  if (checkConfig()) {
    checkDatabase();
    await checkClaude();
    await checkWhatsApp({ send });
  }
  return report();
}

// Only run when invoked directly, not when setup.js imports runDoctor.
// pathToFileURL is what makes this work on Windows, where argv[1] is a
// backslash path like C:\app\scripts\doctor.js and never equals a file:// URL.
if (import.meta.url === pathToFileURL(process.argv[1]).href) {
  const send = process.argv.includes("--send");
  process.exit((await runDoctor({ send })) ? 0 : 1);
}
