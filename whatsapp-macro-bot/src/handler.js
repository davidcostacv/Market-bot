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
  { kind: "help", match: /^(help|\?|start|hi|hello)$/i },
  { kind: "total", match: /^(total|totals|today|t|status)$/i },
  { kind: "list", match: /^(list|log|detail|details|items)$/i },
  { kind: "yesterday", match: /^(yesterday|y)$/i },
  { kind: "week", match: /^(week|w|7d)$/i },
  { kind: "undo", match: /^(undo|oops|remove last|delete last)$/i },
  { kind: "clear", match: /^(clear|reset)( today)?$/i },
  { kind: "goals", match: /^goals?\b(.*)$/i },
  { kind: "timezone", match: /^(?:tz|timezone)\b(.*)$/i },
  { kind: "forget", match: /^forget\b(.*)$/i },
];

/** Returns {kind, arg} for a bot command, or null when the text is a meal. */
export function parseCommand(text) {
  const trimmed = (text || "").trim();
  if (!trimmed) return null;
  for (const { kind, match } of COMMANDS) {
    const found = match.exec(trimmed);
    if (found) return { kind, arg: (found[found.length - 1] || "").trim() };
  }
  return null;
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
      );

    case "yesterday": {
      const day = shiftDate(today, -1);
      return formatDayDetail(
        getDayEntries(phone, day),
        getDayTotals(phone, day),
        user,
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
      const goals = parseGoals(command.arg, userGoals(user));
      if (!goals) {
        return "Send goals as `goals 2400 180 250 70` (kcal, protein, carbs, fat) or `goals kcal=2400 p=180`.";
      }
      setGoals(phone, goals);
      const updated = getOrCreateUser(phone);
      return `Goals updated.\n\n${formatTotals(getDayTotals(phone, today), updated)}`;
    }

    case "timezone": {
      const tz = command.arg;
      if (!tz || !isValidTimezone(tz)) {
        return "Send a timezone name like `tz Europe/Madrid` or `tz America/New_York`.";
      }
      setTimezone(phone, tz);
      return `Timezone set to ${tz}. Your day now rolls over at midnight there.`;
    }

    case "forget": {
      if (!command.arg) return "Send `forget flat white` to drop a remembered food.";
      const removed = forgetFood(phone, `%${normalizeKey(command.arg)}%`);
      return removed
        ? `Forgot ${removed} remembered food${removed === 1 ? "" : "s"} matching “${command.arg}”.`
        : `Nothing remembered matching “${command.arg}”.`;
    }

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
    return `I couldn't find any food in that. Send what you ate (e.g. “2 eggs and toast”) or \`help\` for commands.`;
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
