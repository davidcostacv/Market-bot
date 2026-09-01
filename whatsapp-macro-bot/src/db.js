import fs from "node:fs";
import path from "node:path";
import Database from "better-sqlite3";
import { config } from "./config.js";

// Imports are evaluated before any caller's setup code runs, so the directory
// has to be made right here — a fresh deploy with an empty volume otherwise
// crashes on the very first boot.
fs.mkdirSync(path.dirname(path.resolve(config.dbPath)), { recursive: true });

const db = new Database(config.dbPath);
db.pragma("journal_mode = WAL");

db.exec(`
  CREATE TABLE IF NOT EXISTS users (
    phone         TEXT PRIMARY KEY,
    timezone      TEXT NOT NULL,
    kcal_goal     REAL NOT NULL,
    protein_goal  REAL NOT NULL,
    carbs_goal    REAL NOT NULL,
    fat_goal      REAL NOT NULL,
    last_summary  TEXT,
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
  );

  CREATE TABLE IF NOT EXISTS entries (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    phone       TEXT NOT NULL,
    log_date    TEXT NOT NULL,
    logged_at   TEXT NOT NULL DEFAULT (datetime('now')),
    batch_id    TEXT NOT NULL,
    description TEXT NOT NULL,
    quantity    TEXT,
    kcal        REAL NOT NULL,
    protein     REAL NOT NULL,
    carbs       REAL NOT NULL,
    fat         REAL NOT NULL,
    source      TEXT NOT NULL
  );
  CREATE INDEX IF NOT EXISTS idx_entries_day ON entries (phone, log_date);

  -- Remembered foods: what "my usual protein shake" resolved to last time.
  CREATE TABLE IF NOT EXISTS food_memory (
    phone       TEXT NOT NULL,
    key         TEXT NOT NULL,
    description TEXT NOT NULL,
    quantity    TEXT,
    kcal        REAL NOT NULL,
    protein     REAL NOT NULL,
    carbs       REAL NOT NULL,
    fat         REAL NOT NULL,
    hits        INTEGER NOT NULL DEFAULT 1,
    last_used   TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (phone, key)
  );

  -- WhatsApp retries webhooks aggressively; this keeps meals from being logged twice.
  CREATE TABLE IF NOT EXISTS processed_messages (
    wa_message_id TEXT PRIMARY KEY,
    seen_at       TEXT NOT NULL DEFAULT (datetime('now'))
  );
`);

const statements = {
  getUser: db.prepare("SELECT * FROM users WHERE phone = ?"),
  insertUser: db.prepare(`
    INSERT INTO users (phone, timezone, kcal_goal, protein_goal, carbs_goal, fat_goal)
    VALUES (@phone, @timezone, @kcal, @protein, @carbs, @fat)
  `),
  updateGoals: db.prepare(`
    UPDATE users SET kcal_goal = @kcal, protein_goal = @protein, carbs_goal = @carbs, fat_goal = @fat
    WHERE phone = @phone
  `),
  updateTimezone: db.prepare("UPDATE users SET timezone = ? WHERE phone = ?"),
  updateLastSummary: db.prepare("UPDATE users SET last_summary = ? WHERE phone = ?"),
  allUsers: db.prepare("SELECT * FROM users"),

  insertEntry: db.prepare(`
    INSERT INTO entries (phone, log_date, batch_id, description, quantity, kcal, protein, carbs, fat, source)
    VALUES (@phone, @logDate, @batchId, @description, @quantity, @kcal, @protein, @carbs, @fat, @source)
  `),
  entriesForDay: db.prepare(
    "SELECT * FROM entries WHERE phone = ? AND log_date = ? ORDER BY id",
  ),
  totalsForDay: db.prepare(`
    SELECT COALESCE(SUM(kcal), 0) AS kcal, COALESCE(SUM(protein), 0) AS protein,
           COALESCE(SUM(carbs), 0) AS carbs, COALESCE(SUM(fat), 0) AS fat,
           COUNT(*) AS items
    FROM entries WHERE phone = ? AND log_date = ?
  `),
  totalsByDayRange: db.prepare(`
    SELECT log_date, SUM(kcal) AS kcal, SUM(protein) AS protein, SUM(carbs) AS carbs, SUM(fat) AS fat
    FROM entries WHERE phone = ? AND log_date BETWEEN ? AND ?
    GROUP BY log_date ORDER BY log_date
  `),
  lastBatch: db.prepare(
    "SELECT batch_id FROM entries WHERE phone = ? ORDER BY id DESC LIMIT 1",
  ),
  entriesInBatch: db.prepare("SELECT * FROM entries WHERE phone = ? AND batch_id = ?"),
  deleteBatch: db.prepare("DELETE FROM entries WHERE phone = ? AND batch_id = ?"),
  deleteDay: db.prepare("DELETE FROM entries WHERE phone = ? AND log_date = ?"),

  getMemory: db.prepare("SELECT * FROM food_memory WHERE phone = ? AND key = ?"),
  topMemory: db.prepare(
    "SELECT * FROM food_memory WHERE phone = ? ORDER BY hits DESC, last_used DESC LIMIT ?",
  ),
  upsertMemory: db.prepare(`
    INSERT INTO food_memory (phone, key, description, quantity, kcal, protein, carbs, fat)
    VALUES (@phone, @key, @description, @quantity, @kcal, @protein, @carbs, @fat)
    ON CONFLICT(phone, key) DO UPDATE SET
      hits = hits + 1,
      last_used = datetime('now'),
      description = excluded.description,
      quantity = excluded.quantity,
      kcal = excluded.kcal,
      protein = excluded.protein,
      carbs = excluded.carbs,
      fat = excluded.fat
  `),
  touchMemory: db.prepare(
    "UPDATE food_memory SET hits = hits + 1, last_used = datetime('now') WHERE phone = ? AND key = ?",
  ),
  deleteMemory: db.prepare("DELETE FROM food_memory WHERE phone = ? AND key LIKE ?"),

  markSeen: db.prepare("INSERT OR IGNORE INTO processed_messages (wa_message_id) VALUES (?)"),
  pruneSeen: db.prepare(
    "DELETE FROM processed_messages WHERE seen_at < datetime('now', '-3 days')",
  ),
};

export function getOrCreateUser(phone) {
  const existing = statements.getUser.get(phone);
  if (existing) return existing;
  statements.insertUser.run({
    phone,
    timezone: config.defaultTimezone,
    kcal: config.goals.kcal,
    protein: config.goals.protein,
    carbs: config.goals.carbs,
    fat: config.goals.fat,
  });
  return statements.getUser.get(phone);
}

export const setGoals = (phone, goals) => statements.updateGoals.run({ phone, ...goals });
export const setTimezone = (phone, tz) => statements.updateTimezone.run(tz, phone);
export const setLastSummary = (phone, day) => statements.updateLastSummary.run(day, phone);
export const listUsers = () => statements.allUsers.all();

export const addEntries = db.transaction((phone, logDate, batchId, items, source) => {
  for (const item of items) {
    statements.insertEntry.run({
      phone,
      logDate,
      batchId,
      description: item.description,
      quantity: item.quantity ?? null,
      kcal: item.kcal,
      protein: item.protein,
      carbs: item.carbs,
      fat: item.fat,
      source,
    });
  }
});

export const getDayEntries = (phone, day) => statements.entriesForDay.all(phone, day);
export const getDayTotals = (phone, day) => statements.totalsForDay.get(phone, day);
export const getRangeTotals = (phone, from, to) => statements.totalsByDayRange.all(phone, from, to);
export const clearDay = (phone, day) => statements.deleteDay.run(phone, day).changes;

export function undoLastBatch(phone) {
  const last = statements.lastBatch.get(phone);
  if (!last) return null;
  const items = statements.entriesInBatch.all(phone, last.batch_id);
  statements.deleteBatch.run(phone, last.batch_id);
  return items;
}

export const rememberedFood = (phone, key) => statements.getMemory.get(phone, key);
export const touchRememberedFood = (phone, key) => statements.touchMemory.run(phone, key);
export const topRememberedFoods = (phone, limit = 40) => statements.topMemory.all(phone, limit);
export const rememberFood = (phone, key, item) =>
  statements.upsertMemory.run({
    phone,
    key,
    description: item.description,
    quantity: item.quantity ?? null,
    kcal: item.kcal,
    protein: item.protein,
    carbs: item.carbs,
    fat: item.fat,
  });
export const forgetFood = (phone, pattern) => statements.deleteMemory.run(phone, pattern).changes;

/** Returns false when this WhatsApp message id was already handled. */
export function claimMessage(waMessageId) {
  return statements.markSeen.run(waMessageId).changes === 1;
}

export const pruneOldMessageIds = () => statements.pruneSeen.run();

export default db;
