#!/usr/bin/env node
/**
 * Local chat simulator — talk to the bot from a terminal, no WhatsApp needed.
 *
 *   npm run chat                     # interactive
 *   npm run chat -- "/total" "/week" # run a script of messages and exit
 *
 * It drives the exact same handler the webhook uses, against the same
 * database, so anything that works here works in WhatsApp. Food logging
 * needs ANTHROPIC_API_KEY; the commands and remembered foods do not.
 */
import readline from "node:readline/promises";
import { stdin, stdout } from "node:process";
import { handleMessage } from "../src/handler.js";

const phone = process.env.CHAT_PHONE || "34600111222";

async function say(text) {
  const reply = await handleMessage({ phone, text });
  console.log(`\n${reply}\n`);
}

const scripted = process.argv.slice(2);
if (scripted.length) {
  for (const message of scripted) {
    console.log(`> ${message}`);
    try {
      await say(message);
    } catch (error) {
      console.error(`\nerror: ${error.message}\n`);
    }
  }
  process.exit(0);
}

console.log(`Chatting as ${phone}. Type /help, or ctrl-c to quit.\n`);
const rl = readline.createInterface({ input: stdin, output: stdout });
for (;;) {
  const line = await rl.question("> ");
  if (!line.trim()) continue;
  try {
    await say(line);
  } catch (error) {
    console.error(`error: ${error.message}\n`);
  }
}
