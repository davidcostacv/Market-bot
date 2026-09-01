import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { after, describe, it } from "node:test";

// Point the modules at a throwaway database and a dummy key before they load.
const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), "macro-bot-test-"));
process.env.DB_PATH = path.join(tmpDir, "test.db");
process.env.ANTHROPIC_API_KEY = "sk-ant-test";
process.env.DEFAULT_TIMEZONE = "Europe/Madrid";
process.env.WHATSAPP_APP_SECRET = "test-app-secret";

const { localDate, localHour, shiftDate, isValidTimezone } = await import("../src/time.js");
const { formatTotals, formatItem } = await import("../src/format.js");
const { normalizeKey } = await import("../src/macros.js");
const { parseCommand, parseGoals, handleMessage, COMMAND_NAMES } = await import(
  "../src/handler.js"
);
const db = await import("../src/db.js");
const { GraphError, OUTSIDE_24H_WINDOW, verifySignature } = await import(
  "../src/whatsapp.js"
);
const { summaryTemplateParams } = await import("../src/scheduler.js");

after(() => fs.rmSync(tmpDir, { recursive: true, force: true }));

describe("time helpers", () => {
  it("buckets a day by the user's timezone, not UTC", () => {
    // 23:30 in Madrid (UTC+2 in August) is already the next day in Tokyo.
    const instant = new Date("2026-08-28T21:30:00Z");
    assert.equal(localDate("Europe/Madrid", instant), "2026-08-28");
    assert.equal(localDate("Asia/Tokyo", instant), "2026-08-29");
  });

  it("reads the local hour", () => {
    assert.equal(localHour("UTC", new Date("2026-08-28T21:30:00Z")), 21);
  });

  it("shifts days across month boundaries", () => {
    assert.equal(shiftDate("2026-03-01", -1), "2026-02-28");
    assert.equal(shiftDate("2026-12-31", 1), "2027-01-01");
  });

  it("rejects nonsense timezones", () => {
    assert.equal(isValidTimezone("Europe/Madrid"), true);
    assert.equal(isValidTimezone("Middle/Earth"), false);
  });
});

describe("command parsing", () => {
  it("recognises commands", () => {
    assert.equal(parseCommand("total").kind, "total");
    assert.equal(parseCommand("  UNDO ").kind, "undo");
    assert.equal(parseCommand("tz Europe/Lisbon").kind, "timezone");
    assert.equal(parseCommand("forget flat white").arg, "flat white");
  });

  it("accepts the slash form of every command", () => {
    for (const name of COMMAND_NAMES) {
      const bare = parseCommand(name);
      const slashed = parseCommand(`/${name}`);
      assert.ok(bare, `${name} should parse`);
      assert.equal(slashed.kind, bare.kind, `/${name} should match ${name}`);
      assert.equal(slashed.slashed, true);
    }
  });

  it("keeps arguments in the slash form", () => {
    assert.deepEqual(parseCommand("/tz Europe/Lisbon"), {
      kind: "timezone",
      arg: "Europe/Lisbon",
      slashed: true,
    });
    assert.equal(parseCommand("/goals 2400 180 250 70").arg, "2400 180 250 70");
  });

  it("understands multi-word aliases", () => {
    assert.equal(parseCommand("remove last").kind, "undo");
    assert.equal(parseCommand("/start over").kind, "clear");
  });

  it("reports a typo'd slash command instead of eating it", () => {
    assert.equal(parseCommand("/totl").kind, "unknown");
    assert.equal(parseCommand("/total recall").kind, "total");
  });

  it("treats anything else as food", () => {
    assert.equal(parseCommand("2 eggs and toast"), null);
    assert.equal(parseCommand("total recall burrito"), null);
    assert.equal(parseCommand("week old pizza"), null);
  });

  it("parses positional and named goals", () => {
    assert.deepEqual(parseGoals("2400 180 250 70", {}), {
      kcal: 2400,
      protein: 180,
      carbs: 250,
      fat: 70,
    });
    assert.deepEqual(parseGoals("kcal=2000 p=190", { kcal: 1, protein: 2, carbs: 3, fat: 4 }), {
      kcal: 2000,
      protein: 190,
      carbs: 3,
      fat: 4,
    });
    assert.equal(parseGoals("2400 180", {}), null);
  });
});

describe("food memory", () => {
  it("normalises phrases to a stable key", () => {
    assert.equal(normalizeKey("  Flat  White!! "), "flat white");
    assert.equal(normalizeKey("Café con leche"), "cafe con leche");
  });

  it("logs a remembered food without calling the API", async () => {
    const phone = "34600111222";
    db.getOrCreateUser(phone);
    db.rememberFood(phone, normalizeKey("flat white"), {
      description: "flat white",
      quantity: "1 cup",
      kcal: 120,
      protein: 7,
      carbs: 10,
      fat: 6,
    });

    // No ANTHROPIC key is valid here, so this only passes via the memory path.
    const reply = await handleMessage({ phone, text: "flat white" });
    assert.match(reply, /remembered/i);

    const totals = db.getDayTotals(phone, localDate("Europe/Madrid"));
    assert.equal(totals.kcal, 120);
    assert.equal(totals.protein, 7);
  });

  it("undo removes the last logged batch", async () => {
    const phone = "34600111222";
    const removed = db.undoLastBatch(phone);
    assert.equal(removed.length, 1);
    assert.equal(db.getDayTotals(phone, localDate("Europe/Madrid")).kcal, 0);
  });
});

describe("webhook idempotency", () => {
  it("claims a message id exactly once", () => {
    assert.equal(db.claimMessage("wamid.abc"), true);
    assert.equal(db.claimMessage("wamid.abc"), false);
  });
});

describe("commands end to end", () => {
  const phone = "34600999888";

  it("shows and updates goals", async () => {
    db.getOrCreateUser(phone);
    assert.match(await handleMessage({ phone, text: "/goals" }), /Your goals/);

    const updated = await handleMessage({ phone, text: "/goals 2600 190 260 75" });
    assert.match(updated, /0\/2600 kcal/);
    assert.match(await handleMessage({ phone, text: "/goals" }), /190g protein/);
  });

  it("rejects a bad timezone and accepts a good one", async () => {
    assert.match(await handleMessage({ phone, text: "/tz Mars/Olympus" }), /timezone name/);
    assert.match(await handleMessage({ phone, text: "/tz Europe/Lisbon" }), /Europe\/Lisbon/);
  });

  it("answers /help, /list, /week and /export on an empty day", async () => {
    assert.match(await handleMessage({ phone, text: "/help" }), /Macro bot/);
    assert.match(await handleMessage({ phone, text: "/list" }), /Nothing logged for today/);
    assert.match(await handleMessage({ phone, text: "/week" }), /No meals logged/);
    assert.match(await handleMessage({ phone, text: "/export" }), /Nothing logged/);
  });

  it("exports logged days as CSV", async () => {
    db.rememberFood(phone, normalizeKey("banana"), {
      description: "banana",
      quantity: "1 medium",
      kcal: 105,
      protein: 1,
      carbs: 27,
      fat: 0,
    });
    await handleMessage({ phone, text: "banana" });

    const csv = await handleMessage({ phone, text: "/csv" });
    assert.match(csv, /date,kcal,protein,carbs,fat/);
    assert.match(csv, /,105,1,27,0/);
  });

  it("clears the day", async () => {
    assert.match(await handleMessage({ phone, text: "/clear" }), /Cleared 1 item/);
    assert.match(await handleMessage({ phone, text: "/total" }), /0\/2600 kcal/);
    assert.match(await handleMessage({ phone, text: "/clear" }), /already empty/);
  });

  it("explains an unknown slash command", async () => {
    const reply = await handleMessage({ phone, text: "/totl" });
    assert.match(reply, /Unknown command/);
    assert.match(reply, /\/total/);
  });
});

describe("webhook signatures", () => {
  it("accepts a correctly signed body and rejects everything else", async () => {
    const crypto = await import("node:crypto");
    const body = Buffer.from('{"entry":[]}');
    const signature = crypto
      .createHmac("sha256", process.env.WHATSAPP_APP_SECRET)
      .update(body)
      .digest("hex");

    assert.equal(verifySignature(body, `sha256=${signature}`), true);
    assert.equal(verifySignature(body, `sha256=${"0".repeat(64)}`), false);
    assert.equal(verifySignature(body, "not-a-signature"), false);
    assert.equal(verifySignature(body, undefined), false);
    // A truncated signature must not throw on the length mismatch.
    assert.equal(verifySignature(body, "sha256=abc"), false);
  });
});

describe("graph errors", () => {
  it("pulls Meta's error code out of the response so callers can branch", () => {
    const error = new GraphError(400, {
      error: { message: "Message failed to send", code: OUTSIDE_24H_WINDOW, error_subcode: 12 },
    });
    assert.equal(error.code, OUTSIDE_24H_WINDOW);
    assert.equal(error.status, 400);
    assert.match(error.message, /Message failed to send/);
  });

  it("survives a non-JSON body", () => {
    const error = new GraphError(502, null, "<html>bad gateway</html>");
    assert.equal(error.code, null);
    assert.match(error.message, /bad gateway/);
  });
});

describe("daily recap template", () => {
  it("renders the numbers in the order the approved template expects", () => {
    const params = summaryTemplateParams(
      { kcal_goal: 2400 },
      { kcal: 1999.6, protein: 180.4, carbs: 210, fat: 65 },
    );
    assert.deepEqual(params, ["2000", "180", "210", "65", "2400"]);
    assert.ok(params.every((p) => typeof p === "string"));
  });
});

describe("formatting", () => {
  const user = { kcal_goal: 2400, protein_goal: 180, carbs_goal: 250, fat_goal: 70 };

  it("renders totals with progress and remaining", () => {
    const out = formatTotals({ kcal: 1200, protein: 90, carbs: 120, fat: 40 }, user);
    assert.match(out, /1200\/2400 kcal/);
    assert.match(out, /1200 kcal left/);
  });

  it("renders an over-goal day without a negative sign", () => {
    const out = formatTotals({ kcal: 2600, protein: 200, carbs: 300, fat: 90 }, user);
    assert.match(out, /200 kcal over/);
    assert.doesNotMatch(out, /-\d+ kcal/);
  });

  it("renders a single item line", () => {
    assert.equal(
      formatItem({ description: "flat white", quantity: "1 cup", kcal: 120, protein: 7, carbs: 10, fat: 6 }),
      "• 1 cup flat white — 120 kcal · 7P 10C 6F",
    );
  });
});
