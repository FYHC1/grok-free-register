// Email Routing -> local webhook via Cloudflare Tunnel
// REQUIRED secret: WEBHOOK_URL = https://hook.example.com/webhook
import PostalMime from "postal-mime";

function pickEmail(value) {
  if (value == null) return "";
  if (typeof value === "string") {
    const m = value.match(/[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}/i);
    return (m ? m[0] : value).trim();
  }
  if (Array.isArray(value)) {
    for (const item of value) {
      const got = pickEmail(item);
      if (got) return got;
    }
    return "";
  }
  if (typeof value === "object") {
    if (value.address) return pickEmail(value.address);
    if (value.email) return pickEmail(value.email);
    if (value.value) return pickEmail(value.value);
  }
  return String(value);
}

function trim(s, max = 50000) {
  if (!s) return "";
  s = String(s);
  return s.length > max ? s.slice(0, max) : s;
}

function cleanWebhookUrl(raw) {
  if (raw == null) return "";
  // Remove BOM / zero-width / control chars that break URL parsing.
  let s = String(raw)
    .replace(/^\uFEFF/, "")
    .replace(/[\u0000-\u001F\u007F\u200B-\u200D\uFEFF]/g, "")
    .trim();
  // Strip accidental wrapping quotes.
  if (
    (s.startsWith('"') && s.endsWith('"')) ||
    (s.startsWith("'") && s.endsWith("'"))
  ) {
    s = s.slice(1, -1).trim();
  }
  return s;
}

export default {
  async email(message, env) {
    const webhook = cleanWebhookUrl(env.WEBHOOK_URL);
    console.log(
      "email event rawSize=" +
        (message.rawSize || 0) +
        " from=" +
        message.from +
        " to=" +
        message.to
    );
    console.log(
      "webhook_url_check len=" +
        webhook.length +
        " starts=" +
        webhook.slice(0, 8) +
        " validish=" +
        /^https?:\/\//i.test(webhook)
    );

    if (!webhook || !/^https?:\/\//i.test(webhook)) {
      console.error("WEBHOOK_URL invalid or missing");
      try {
        message.setReject("WEBHOOK_URL invalid");
      } catch (_) {}
      return;
    }

    let subject = "";
    let text = "";
    let html = "";
    try {
      const parsed = await PostalMime.parse(message.raw);
      subject = parsed.subject || "";
      text = trim(parsed.text || "");
      html = trim(parsed.html || "");
    } catch (err) {
      console.error("mime parse failed: " + String(err));
    }
    try {
      if (!subject && message.headers) {
        subject = message.headers.get("subject") || "";
      }
    } catch (_) {}

    let to = pickEmail(message.to);
    let from = pickEmail(message.from);
    try {
      if (!to && message.headers) {
        to = pickEmail(
          message.headers.get("to") ||
            message.headers.get("delivered-to") ||
            message.headers.get("cc") ||
            ""
        );
      }
      if (!from && message.headers) {
        from = pickEmail(message.headers.get("from") || "");
      }
    } catch (_) {}

    const payload = {
      from,
      to,
      subject: trim(subject, 500),
      text,
      html,
    };
    console.log(
      "posting webhook to=" + to + " from=" + from + " subject=" + subject
    );

    try {
      const res = await fetch(webhook, {
        method: "POST",
        headers: {
          "content-type": "application/json",
          ...(env.WEBHOOK_TOKEN
            ? { "x-webhook-token": env.WEBHOOK_TOKEN }
            : {}),
        },
        body: JSON.stringify(payload),
      });
      console.log("webhook status=" + res.status);
      if (!res.ok) {
        const body = await res.text().catch(() => "");
        console.error("webhook non-ok body=" + body.slice(0, 200));
        try {
          message.setReject("webhook " + res.status);
        } catch (_) {}
      }
    } catch (err) {
      console.error("webhook fetch failed: " + String(err));
      try {
        message.setReject("webhook fetch failed");
      } catch (_) {}
    }
  },
};