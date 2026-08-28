const round = (n) => Math.round(Number(n) || 0);

export function formatItem(item) {
  const label = item.quantity ? `${item.quantity} ${item.description}` : item.description;
  return `• ${label} — ${round(item.kcal)} kcal · ${round(item.protein)}P ${round(item.carbs)}C ${round(item.fat)}F`;
}

function bar(value, goal, width = 10) {
  if (!goal) return "";
  const filled = Math.max(0, Math.min(width, Math.round((value / goal) * width)));
  return `${"▓".repeat(filled)}${"░".repeat(width - filled)}`;
}

function line(label, value, goal, unit) {
  const left = round(goal - value);
  const suffix = left >= 0 ? `${left}${unit} left` : `${Math.abs(left)}${unit} over`;
  return `${label} ${bar(value, goal)} ${round(value)}/${round(goal)}${unit} · ${suffix}`;
}

export function formatTotals(totals, user, { title = "Today so far" } = {}) {
  return [
    `*${title}*`,
    line("🔥", totals.kcal, user.kcal_goal, " kcal"),
    line("🥩", totals.protein, user.protein_goal, "g"),
    line("🍚", totals.carbs, user.carbs_goal, "g"),
    line("🥑", totals.fat, user.fat_goal, "g"),
  ].join("\n");
}

/** Reply sent right after a meal is logged: what was added, then where you stand. */
export function formatLogReply(items, totals, user, { fromMemory = false } = {}) {
  const header = fromMemory ? "Logged (remembered)" : "Logged";
  return [
    `*${header}*`,
    ...items.map(formatItem),
    "",
    formatTotals(totals, user),
  ].join("\n");
}

export function formatDayDetail(entries, totals, user, title) {
  if (!entries.length) return `Nothing logged for ${title.toLowerCase()} yet.`;
  return [
    `*${title}*`,
    ...entries.map(formatItem),
    "",
    formatTotals(totals, user, { title: "Total" }),
  ].join("\n");
}

export function formatWeek(rows, user) {
  if (!rows.length) return "No meals logged in the last 7 days.";
  const avg = (key) => rows.reduce((sum, r) => sum + Number(r[key] || 0), 0) / rows.length;
  return [
    `*Last ${rows.length} logged day${rows.length === 1 ? "" : "s"}*`,
    ...rows.map(
      (r) =>
        `${r.log_date.slice(5)} — ${round(r.kcal)} kcal · ${round(r.protein)}P ${round(r.carbs)}C ${round(r.fat)}F`,
    ),
    "",
    `*Daily average*`,
    `🔥 ${round(avg("kcal"))} kcal (goal ${round(user.kcal_goal)})`,
    `🥩 ${round(avg("protein"))}g P · 🍚 ${round(avg("carbs"))}g C · 🥑 ${round(avg("fat"))}g F`,
  ].join("\n");
}

export const HELP_TEXT = [
  "*Macro bot* — just tell me what you ate.",
  "",
  "_Examples_",
  "• `2 eggs, 2 toast with butter and a flat white`",
  "• `chicken burrito bowl from chipotle`",
  "• send a *photo* of the plate (add a caption for portion size)",
  "",
  "_Commands_",
  "• `total` — today's running totals",
  "• `list` — everything logged today",
  "• `yesterday` — yesterday's totals",
  "• `week` — last 7 days + averages",
  "• `undo` — remove the last thing logged",
  "• `clear` — wipe today and start over",
  "• `goals 2400 180 250 70` — kcal, protein, carbs, fat",
  "• `tz Europe/Madrid` — set your timezone",
  "• `forget flat white` — drop a remembered food",
  "• `help` — this message",
].join("\n");
