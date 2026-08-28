import crypto from "node:crypto";
import { config } from "./config.js";

const graph = (path) =>
  `https://graph.facebook.com/${config.whatsapp.graphVersion}/${path}`;

const authHeaders = () => ({ Authorization: `Bearer ${config.whatsapp.token}` });

async function graphFetch(url, options = {}, { retries = 2 } = {}) {
  let lastError;
  for (let attempt = 0; attempt <= retries; attempt++) {
    try {
      const response = await fetch(url, options);
      if (response.ok) return response;
      const body = await response.text();
      // 4xx other than rate limiting will not get better by retrying.
      if (response.status < 500 && response.status !== 429) {
        throw new Error(`Graph API ${response.status}: ${body}`);
      }
      lastError = new Error(`Graph API ${response.status}: ${body}`);
    } catch (error) {
      lastError = error;
      if (String(error.message).startsWith("Graph API 4")) throw error;
    }
    await new Promise((resolve) => setTimeout(resolve, 500 * 2 ** attempt));
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
