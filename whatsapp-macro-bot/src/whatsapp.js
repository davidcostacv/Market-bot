import crypto from "node:crypto";
import { config } from "./config.js";

const graph = (path) =>
  `https://graph.facebook.com/${config.whatsapp.graphVersion}/${path}`;

const authHeaders = () => ({ Authorization: `Bearer ${config.whatsapp.token}` });

/** Meta's error codes are the only way to tell "expired token" from "outside
 *  the 24-hour window", and each needs a different response. */
export class GraphError extends Error {
  constructor(status, payload, raw) {
    const detail = payload?.error?.message || raw || "no response body";
    super(`Graph API ${status}: ${detail}`);
    this.name = "GraphError";
    this.status = status;
    this.code = payload?.error?.code ?? null;
    this.subcode = payload?.error?.error_subcode ?? null;
  }
}

/** 131047: the user has not messaged in 24h, so free-form text is refused. */
export const OUTSIDE_24H_WINDOW = 131047;

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function graphFetch(url, options = {}, { retries = 2 } = {}) {
  let lastError;
  for (let attempt = 0; attempt <= retries; attempt++) {
    let response;
    try {
      response = await fetch(url, options);
    } catch (cause) {
      // Network-level failure: worth retrying.
      lastError = new Error(`Could not reach the Graph API: ${cause.message}`, { cause });
      await sleep(500 * 2 ** attempt);
      continue;
    }

    if (response.ok) return response;

    const raw = await response.text();
    let payload;
    try {
      payload = JSON.parse(raw);
    } catch {
      payload = null;
    }
    const error = new GraphError(response.status, payload, raw);

    // A bad token or a rejected message will not fix itself; only server
    // errors and rate limits are worth another attempt.
    if (response.status < 500 && response.status !== 429) throw error;
    lastError = error;
    await sleep(500 * 2 ** attempt);
  }
  throw lastError;
}

/** WhatsApp caps a text body at 4096 characters. */
function chunk(text, size = 3900) {
  const parts = [];
  let rest = text;
  while (rest.length > size) {
    const cut = rest.lastIndexOf("\n", size);
    const at = cut > size / 2 ? cut : size;
    parts.push(rest.slice(0, at));
    rest = rest.slice(at).replace(/^\n/, "");
  }
  parts.push(rest);
  return parts;
}

export async function sendText(to, body) {
  for (const part of chunk(body)) {
    await graphFetch(graph(`${config.whatsapp.phoneNumberId}/messages`), {
      method: "POST",
      headers: { ...authHeaders(), "Content-Type": "application/json" },
      body: JSON.stringify({
        messaging_product: "whatsapp",
        recipient_type: "individual",
        to,
        type: "text",
        text: { preview_url: false, body: part },
      }),
    });
  }
}

/**
 * WhatsApp only allows free-form text within 24 hours of the user's last
 * message. The nightly recap is usually outside that window, so it goes out
 * as a pre-approved template with the day's numbers as parameters.
 */
export async function sendTemplate(to, name, languageCode, parameters = []) {
  await graphFetch(graph(`${config.whatsapp.phoneNumberId}/messages`), {
    method: "POST",
    headers: { ...authHeaders(), "Content-Type": "application/json" },
    body: JSON.stringify({
      messaging_product: "whatsapp",
      recipient_type: "individual",
      to,
      type: "template",
      template: {
        name,
        language: { code: languageCode },
        components: parameters.length
          ? [
              {
                type: "body",
                parameters: parameters.map((text) => ({ type: "text", text: String(text) })),
              },
            ]
          : [],
      },
    }),
  });
}

/** Used by `npm run doctor` to prove the token and phone number id line up. */
export async function getPhoneNumberInfo() {
  const response = await graphFetch(
    graph(`${config.whatsapp.phoneNumberId}?fields=display_phone_number,verified_name,quality_rating`),
    { headers: authHeaders() },
    { retries: 0 },
  );
  return response.json();
}

export async function markRead(messageId) {
  try {
    await graphFetch(graph(`${config.whatsapp.phoneNumberId}/messages`), {
      method: "POST",
      headers: { ...authHeaders(), "Content-Type": "application/json" },
      body: JSON.stringify({
        messaging_product: "whatsapp",
        status: "read",
        message_id: messageId,
      }),
    });
  } catch (error) {
    console.warn("[whatsapp] could not mark message read:", error.message);
  }
}

/** Two hops: media id -> temporary CDN url -> bytes. */
export async function downloadMedia(mediaId) {
  const metaResponse = await graphFetch(graph(mediaId), { headers: authHeaders() });
  const meta = await metaResponse.json();
  const binary = await graphFetch(meta.url, { headers: authHeaders() });
  const buffer = Buffer.from(await binary.arrayBuffer());
  return {
    data: buffer.toString("base64"),
    mediaType: (meta.mime_type || "image/jpeg").split(";")[0],
    bytes: buffer.length,
  };
}

/**
 * Meta signs every webhook with the app secret. Without this check anyone who
 * learns the URL can post fake meals — or fake anything — into the bot.
 */
export function verifySignature(rawBody, signatureHeader) {
  if (!signatureHeader?.startsWith("sha256=")) return false;
  const expected = crypto
    .createHmac("sha256", config.whatsapp.appSecret)
    .update(rawBody)
    .digest("hex");
  const received = signatureHeader.slice("sha256=".length);
  const a = Buffer.from(expected, "utf8");
  const b = Buffer.from(received, "utf8");
  return a.length === b.length && crypto.timingSafeEqual(a, b);
}
