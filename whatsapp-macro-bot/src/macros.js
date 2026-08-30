import Anthropic from "@anthropic-ai/sdk";
import { zodOutputFormat } from "@anthropic-ai/sdk/helpers/zod";
import { z } from "zod";
import { config } from "./config.js";
import { rememberedFood, rememberFood, topRememberedFoods, touchRememberedFood } from "./db.js";

const client = new Anthropic({ apiKey: config.anthropic.apiKey });

const MacroItem = z.object({
  description: z.string().describe("The food, normalised and singular, e.g. 'scrambled eggs'"),
  quantity: z.string().describe("Portion as the user gave it, e.g. '2 large' or '150 g'. Empty string if unknown."),
  kcal: z.number(),
  protein: z.number().describe("grams"),
  carbs: z.number().describe("grams"),
  fat: z.number().describe("grams"),
  confidence: z.enum(["high", "medium", "low"]),
});

const MacroExtraction = z.object({
  is_food: z.boolean().describe("False if the message is not someone logging food they ate"),
  items: z.array(MacroItem),
  note: z.string().describe("At most one short sentence, only if a portion had to be assumed. Empty string otherwise."),
});

const SYSTEM_PROMPT = `You are a nutrition estimator for a personal macro-tracking bot. The user sends what they ate in casual language, or a photo of the plate.

Break the meal into individual food items and estimate calories, protein, carbohydrates and fat for the portion actually eaten.

Rules:
- Estimate for the whole portion described, not per 100 g.
- When no portion is given, assume a normal adult serving and say so in "note".
- Restaurant and branded items: use the chain's published values when you know them.
- Round to whole numbers. Macros should roughly reconcile with the calories (4/4/9 kcal per gram of protein/carb/fat).
- Drinks, sauces, oil used for cooking and snacks all count.
- If the message is not food (a question, a greeting, a command), set is_food to false and return no items.
- Never refuse to estimate. An approximate number is the point of the tool.`;

/** Collapse a food phrase into a stable lookup key. */
export function normalizeKey(text) {
  return String(text)
    .toLowerCase()
    .normalize("NFD")
    .replace(/[\u0300-\u036f]/g, "")
    .replace(/[^a-z0-9\s]/g, " ")
    .replace(/\s+/g, " ")
    .trim();
}

function memoryContext(phone) {
  const foods = topRememberedFoods(phone, 40);
  if (!foods.length) return "";
  const lines = foods.map(
    (f) =>
      `- ${f.quantity ? `${f.quantity} ` : ""}${f.description}: ${Math.round(f.kcal)} kcal, ${Math.round(f.protein)}g P, ${Math.round(f.carbs)}g C, ${Math.round(f.fat)}g F`,
  );
  return `\n\nThis user's previously logged foods. If the current meal contains one of these, reuse the same numbers so their day-to-day tracking stays consistent — scale them if the portion clearly differs:\n${lines.join("\n")}`;
}

function coerceItem(item) {
  return {
    description: item.description.trim(),
    quantity: item.quantity?.trim() || null,
    kcal: Math.max(0, Math.round(item.kcal)),
    protein: Math.max(0, Math.round(item.protein)),
    carbs: Math.max(0, Math.round(item.carbs)),
    fat: Math.max(0, Math.round(item.fat)),
    confidence: item.confidence,
  };
}

/**
 * Exact repeat of something already logged — answered from the local food
 * memory with no API call at all, which is both instant and free.
 */
export function lookupMemory(phone, text) {
  const key = normalizeKey(text);
  if (!key) return null;
  const hit = rememberedFood(phone, key);
  if (!hit) return null;
  touchRememberedFood(phone, key);
  return {
    description: hit.description,
    quantity: hit.quantity,
    kcal: hit.kcal,
    protein: hit.protein,
    carbs: hit.carbs,
    fat: hit.fat,
    confidence: "high",
  };
}

/**
 * @param {object} input
 * @param {string} input.phone
 * @param {string} [input.text]
 * @param {{ data: string, mediaType: string }} [input.image] base64 image
 * @returns {Promise<{ isFood: boolean, items: object[], note: string }>}
 */
export async function estimateMacros({ phone, text, image }) {
  const content = [];
  if (image) {
    content.push({
      type: "image",
      source: { type: "base64", media_type: image.mediaType, data: image.data },
    });
    content.push({
      type: "text",
      text: text
        ? `Photo of what I ate. My note: ${text}`
        : "Photo of what I ate. Estimate the portions from the picture.",
    });
  } else {
    content.push({ type: "text", text });
  }

  let response;
  try {
    response = await client.messages.parse({
      model: config.anthropic.model,
      max_tokens: 4000,
      system: SYSTEM_PROMPT + memoryContext(phone),
      messages: [{ role: "user", content }],
      output_config: {
        effort: "low",
        format: zodOutputFormat(MacroExtraction),
      },
    });
  } catch (error) {
    // Most specific first — each of these needs a different fix from whoever
    // runs the bot, so say which one it is instead of one flat "API error".
    if (error instanceof Anthropic.AuthenticationError) {
      throw new Error("ANTHROPIC_API_KEY is missing or invalid.");
    }
    if (error instanceof Anthropic.RateLimitError) {
      throw new Error("Rate limited by the Claude API — try again in a moment.");
    }
    if (error instanceof Anthropic.APIConnectionError) {
      throw new Error("Could not reach the Claude API.");
    }
    throw error;
  }

  if (response.stop_reason === "refusal") {
    throw new Error("The model declined to answer that one — try rewording it.");
  }

  const parsed = response.parsed_output;
  if (!parsed) throw new Error("Could not read the macros back from the model.");

  const items = parsed.is_food ? parsed.items.map(coerceItem) : [];

  // Everything logged goes into the food memory, so tomorrow's identical meal
  // gets the identical numbers (and no API call).
  for (const item of items) {
    const key = normalizeKey(`${item.quantity ?? ""} ${item.description}`);
    if (key) rememberFood(phone, key, item);
  }
  if (!image && text && items.length) {
    const rawKey = normalizeKey(text);
    if (rawKey && items.length === 1) rememberFood(phone, rawKey, items[0]);
  }

  return { isFood: parsed.is_food, items, note: parsed.note?.trim() || "" };
}

export const sumItems = (items) => ({
  kcal: items.reduce((s, i) => s + i.kcal, 0),
  protein: items.reduce((s, i) => s + i.protein, 0),
  carbs: items.reduce((s, i) => s + i.carbs, 0),
  fat: items.reduce((s, i) => s + i.fat, 0),
});
