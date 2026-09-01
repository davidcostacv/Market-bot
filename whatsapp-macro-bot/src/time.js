/**
 * Everything is bucketed by the *user's* local calendar day, not UTC — a 1am
 * snack still belongs to the day that just ended for them, and the day rolls
 * over at their midnight wherever the server happens to run.
 */

/** "YYYY-MM-DD" in the given IANA timezone. */
export function localDate(timezone, date = new Date()) {
  // en-CA formats as YYYY-MM-DD, which sorts and compares as a plain string.
  return new Intl.DateTimeFormat("en-CA", {
    timeZone: timezone,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).format(date);
}

/** Local hour 0-23 in the given IANA timezone. */
export function localHour(timezone, date = new Date()) {
  return Number(
    new Intl.DateTimeFormat("en-GB", {
      timeZone: timezone,
      hour: "2-digit",
      hour12: false,
    }).format(date),
  );
}

/** Shift a "YYYY-MM-DD" string by whole days. */
export function shiftDate(day, days) {
  const shifted = new Date(`${day}T12:00:00Z`);
  shifted.setUTCDate(shifted.getUTCDate() + days);
  return shifted.toISOString().slice(0, 10);
}

export function isValidTimezone(timezone) {
  try {
    new Intl.DateTimeFormat("en-CA", { timeZone: timezone });
    return true;
  } catch {
    return false;
  }
}

/** "Thu 28 Aug" — used in summaries. */
export function prettyDate(day, timezone) {
  return new Intl.DateTimeFormat("en-GB", {
    timeZone: timezone,
    weekday: "short",
    day: "numeric",
    month: "short",
  }).format(new Date(`${day}T12:00:00Z`));
}
