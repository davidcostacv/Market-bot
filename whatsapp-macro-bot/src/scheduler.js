import cron from "node-cron";
import { config } from "./config.js";
import { getDayTotals, listUsers, pruneOldMessageIds, setLastSummary } from "./db.js";
import { formatTotals } from "./format.js";
import { localDate, localHour, prettyDate } from "./time.js";
import { sendText } from "./whatsapp.js";

function summaryFor(user, day) {
  const totals = getDayTotals(user.phone, day);
  if (!totals.items) {
    return `*${prettyDate(day, user.timezone)}* — nothing logged today. Send me what you ate and I'll catch up.`;
  }
  const left = Math.round(user.kcal_goal - totals.kcal);
  const tail =
    left > 0
      ? `${left} kcal still available if you're eating again tonight.`
      : `${Math.abs(left)} kcal over — no drama, tomorrow is a new day.`;
  return `${formatTotals(totals, user, { title: `Daily recap — ${prettyDate(day, user.timezone)}` })}\n\n${tail}`;
}

/**
 * Runs every 15 minutes and asks "is it recap o'clock where this user lives?".
 * Checking per user (instead of one fixed cron hour) is what makes the bot
 * correct for any timezone, and the last_summary stamp keeps it to once a day.
 */
export function startScheduler() {
  if (config.dailySummaryHour === null) {
    console.log("[scheduler] daily summary disabled (DAILY_SUMMARY_HOUR is empty)");
    return;
  }

  cron.schedule("*/15 * * * *", async () => {
    for (const user of listUsers()) {
      try {
        const today = localDate(user.timezone);
        if (user.last_summary === today) continue;
        if (localHour(user.timezone) !== config.dailySummaryHour) continue;

        await sendText(user.phone, summaryFor(user, today));
        setLastSummary(user.phone, today);
      } catch (error) {
        console.error(`[scheduler] summary failed for ${user.phone}:`, error.message);
      }
    }
  });

  cron.schedule("0 4 * * *", () => pruneOldMessageIds());

  console.log(
    `[scheduler] daily recap armed for ${String(config.dailySummaryHour).padStart(2, "0")}:00 local time`,
  );
}
