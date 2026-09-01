import { randomUUID } from "node:crypto";
import { config } from "./config.js";
import {
  addEntries,
  clearDay,
  forgetFood,
  getDayEntries,
  getDayTotals,
  getOrCreateUser,
  getRangeTotals,
  rememberFood,
  setGoals,
  setTimezone,
  undoLastBatch,
} from "./db.js";
import {
  HELP_TEXT,
  formatDayDetail,
  formatItem,
  formatLogReply,
  formatTotals,
  formatWeek,
} from "./format.js";
import { estimateMacros, lookupMemory, normalizeKey } from "./macros.js";
import { isValidTimezone, localDate, prettyDate, shiftDate } from "./time.js";

const COMMANDS = [
  { kind: "help", names: ["help", "?", "start", "menu", "commands"] },
  { kind: "total", names: ["total", "totals", "today", "t", "status"] },
  { kind: "list", names: ["list", "log", "items", "detail", "details"] },
  { kind: "yesterday", names: ["yesterday", "y"] },
  { kind: "week", names: ["week", "w", "7d"] },
  { kind: "undo", names: ["undo", "oops"] },
  { kind: "clear", names: ["clear", "reset"], takesArg: true },
  { kind: "export", names: ["export", "csv"] },
  { kind: "goals", names: ["goals", "goal"], takesArg: true },
  { kind: "timezone", names: ["tz", "timezone"], takesArg: true },
  { kind: "forget", names: ["forget"], takesArg: true },
];

/** Multi-word aliases, checked before the single-word table. */
const PHRASES = new Map([
  ["remove last", "undo"],
  ["delete last", "undo"],
  ["start over", "clear"],
]);

export const COMMAND_NAMES = COMMANDS.flatMap((command) => command.names);

/**
 * Commands work with or without a leading slash: `/total` and `total` are the
 * same. The slash matters for what happens when nothing matches — `/totl` is a
 * typo worth reporting, while a bare `totl` is more likely something you ate.
 *
 * @returns {{kind: string, arg: string, slashed: boolean}|null} null means "this is food"
 */
export function parseCommand(text) {
  const trimmed = (text || "").trim();
  if (!trimmed) return null;

  const slashed = trimmed.startsWith("/");
  const body = (slashed ? trimmed.slice(1) : trimmed).trim();
  if (!body) return slashed ? { kind: "help", arg: "", slashed } : null;

  const phrase = PHRASES.get(body.toLowerCase());
  if (phrase) return { kind: phrase, arg: "", slashed };

  const [word, ...rest] = body.split(/\s+/);
  const arg = rest.join(" ").trim();
  const command = COMMANDS.find((entry) => entry.names.includes(word.toLowerCase()));

  if (command) {
    // Without a slash, trailing words mean it was never a command:
    // "total recall burrito" is dinner, "/total recall" is a typo'd command.
    if (arg && !command.takesArg && !slashed) return null;
    return { kind: command.kind, arg, slashed };
  }

  return slashed ? { kind: "unknown", arg: body, slashed } : null;
}

/** `goals 2400 180 250 70` or `goals kcal=2400 p=180 c=250 f=70` */
export function parseGoals(arg, current) {
  const named = {
    kcal: /(?:kcal|cal|calories)\s*[=: ]\s*(\d+)/i,
    protein: /(?:p|protein)\s*[=: ]\s*(\d+)/i,
    carbs: /(?:c|carbs?|carbohydrates?)\s*[=: ]\s*(\d+)/i,
    fat: /(?:f|fats?)\s*[=: ]\s*(\d+)/i,
  };
  const goals = { ...current };
  let matched = false;
  for (const [key, pattern] of Object.entries(named)) {
    const found = pattern.exec(arg);
    if (found) {
      goals[key] = Number(found[1]);
      matched = true;
    }
  }
  if (matched) return goals;

  const numbers = arg.match(/\d+(?:\.\d+)?/g);
  if (!numbers || numbers.length < 4) return null;
  const [kcal, protein, carbs, fat] = numbers.map(Number);
  return { kcal, protein, carbs, fat };
}

function userGoals(user) {
  return {
    kcal: user.kcal_goal,
    protein: user.protein_goal,
    carbs: user.carbs_goal,
    fat: user.fat_goal,
  };
}

async function handleCommand(command, user, today) {
  const phone = user.phone;

  switch (command.kind) {
    case "help":
      return HELP_TEXT;

    case "total":
      return formatTotals(getDayTotals(phone, today), user);

    case "list":
      return formatDayDetail(
        getDayEntries(phone, today),
        getDayTotals(phone, today),
        user,
        "Today",
        "today",
      );

    case "yesterday": {
      const day = shiftDate(today, -1);
      return formatDayDetail(
        getDayEntries(phone, day),
        getDayTotals(phone, day),
        user,
        prettyDate(day, user.timezone),
        prettyDate(day, user.timezone),
      );
    }

    case "week":
      return formatWeek(getRangeTotals(phone, shiftDate(today, -6), today), user);

    case "undo": {
      const removed = undoLastBatch(phone);
      if (!removed?.length) return "Nothing to undo.";
      return [
        "*Removed*",
        ...removed.map(formatItem),
        "",
        formatTotals(getDayTotals(phone, today), user),
      ].join("\n");
    }

    case "clear": {
      const deleted = clearDay(phone, today);
      return deleted
        ? `Cleared ${deleted} item${deleted === 1 ? "" : "s"} from today. Starting fresh.`
        : "Today was already empty.";
    }

    case "goals": {
      if (!command.arg) {
        return [
          "*Your goals*",
          `🔥 ${Math.round(user.kcal_goal)} kcal`,
          `🥩 ${Math.round(user.protein_goal)}g protein`,
          `🍚 ${Math.round(user.carbs_goal)}g carbs`,
          `🥑 ${Math.round(user.fat_goal)}g fat`,
          "",
          "Change them with `/goals 2400 180 250 70`.",
        ].join("\n");
      }
      const goals = parseGoals(command.arg, userGoals(user));
      if (!goals) {
        return "Send goals as `/goals 2400 180 250 70` (kcal, protein, carbs, fat) or `/goals kcal=2400 p=180`.";
      }
      setGoals(phone, goals);
      const updated = getOrCreateUser(phone);
      return `Goals updated.\n\n${formatTotals(getDayTotals(phone, today), updated)}`;
    }

    case "timezone": {
      const tz = command.arg;
      if (!tz || !isValidTimezone(tz)) {
        return "Send a timezone name like `/tz Europe/Madrid` or `/tz America/New_York`.";
      }
      setTimezone(phone, tz);
      return `Timezone set to ${tz}. Your day now rolls over at midnight there.`;
    }

    case "forget": {
      if (!command.arg) return "Send `/forget flat white` to drop a remembered food.";
      const removed = forgetFood(phone, `%${normalizeKey(command.arg)}%`);
      return removed
        ? `Forgot ${removed} remembered food${removed === 1 ? "" : "s"} matching “${command.arg}”.`
        : `Nothing remembered matching “${command.arg}”.`;
    }

    case "export": {
      const rows = getRangeTotals(phone, shiftDate(today, -29), today);
      if (!rows.length) return "Nothing logged in the last 30 days to export.";
      return [
        "*Last 30 days (CSV)*",
        "```",
        "date,kcal,protein,carbs,fat",
        ...rows.map((r) =>
          [r.log_date, r.kcal, r.protein, r.carbs, r.fat]
            .map((value, index) => (index === 0 ? value : Math.round(value)))
            .join(","),
        ),
        "```",
      ].join("\n");
    }

    case "unknown":
      return `Unknown command \`/${command.arg.split(/\s+/)[0]}\`.\n\n${HELP_TEXT}`;

    default:
      return HELP_TEXT;
  }
}

async function handleFoodLog({ user, today, text, image }) {
  const phone = user.phone;

  // Cheap path first: an exact repeat of something already logged.
  if (!image && text) {
    const remembered = lookupMemory(phone, text);
    if (remembered) {
      addEntries(phone, today, randomUUID(), [remembered], "memory");
      return formatLogReply([remembered], getDayTotals(phone, today), user, {
        fromMemory: true,
      });
    }
  }

  const { isFood, items, note } = await estimateMacros({ phone, text, image });

  if (!isFood || !items.length) {
    return "I couldn't find any food in that. Send what you ate (e.g. “2 eggs and toast”) or `/help` for commands.";
  }

  addEntries(phone, today, randomUUID(), items, image ? "photo" : "text");
  const reply = formatLogReply(items, getDayTotals(phone, today), user);
  return note ? `${reply}\n\n_${note}_` : reply;
}

/**
 * @returns {Promise<string>} the reply to send back over WhatsApp
 */
export async function handleMessage({ phone, text, image }) {
  const user = getOrCreateUser(phone);
  const today = localDate(user.timezone);

  if (!image) {
    const command = parseCommand(text);
    if (command) return handleCommand(command, user, today);
  }

  return handleFoodLog({ user, today, text, image });
}

export { config };
