// Cloudflare Email Worker "travel-inbound" (not part of the NAS package).
// Paste this into Cloudflare → Workers & Pages → travel-inbound → Edit code.
// Settings → Variables and secrets:
//   INBOUND_SECRET  (Secret)  the email key from Travel Manager → Documents → Bookings by email
//   FALLBACK_TO     (Text)    optional: a verified inbox; used when Travel Manager cannot take the email
// Email Routing → Routing rules → travel@… → Action: Send to a Worker → travel-inbound.
//
// Work-calendar invites (optional):
//   Settings → Bindings → Add binding → "Send email", variable name EMAIL.
//   Settings → Trigger events → Add → Cron trigger, every minute: * * * * *
//   The work address must be verified under Email Routing → Destination addresses.

import { EmailMessage } from "cloudflare:email";

const BASE = "https://travel.businessintels.com";

export default {
  // Incoming booking emails → Travel Manager.
  async email(message, env) {
    const raw = await new Response(message.raw).arrayBuffer();
    let response = null;
    try {
      response = await fetch(BASE + "/api/inbound", {
        method: "POST",
        headers: { "Content-Type": "message/rfc822", "X-Inbound-Secret": env.INBOUND_SECRET || "" },
        body: raw,
      });
    } catch (error) {
      response = null; // NAS or tunnel offline
    }
    if (response && response.ok) return;
    let code = "";
    try { code = (await response.json()).code || ""; } catch (error) {}
    // Refused on purpose (unknown sender, sender not verified, too large): tell the sender.
    if (["sender_not_allowed", "sender_unverified", "too_large"].includes(code)) {
      message.setReject("Not accepted by Travel Manager");
      return;
    }
    // Anything else (NAS offline, wrong key, switched off): keep the email in the inbox instead.
    if (env.FALLBACK_TO) {
      await message.forward(env.FALLBACK_TO);
      return;
    }
    throw new Error("Travel Manager could not take the email (" + (response ? response.status : "offline") + ")");
  },

  // Every minute: collect calendar invites waiting in Travel Manager and send them.
  async scheduled(event, env, ctx) {
    if (!env.EMAIL) return; // invites not set up
    const headers = { "X-Inbound-Secret": env.INBOUND_SECRET || "" };
    let response;
    try {
      response = await fetch(BASE + "/api/outbox", { headers });
    } catch (error) {
      return; // NAS offline: try again next minute
    }
    if (!response.ok) return;
    const { messages = [] } = await response.json();
    if (!messages.length) return;
    const results = [];
    for (const m of messages) {
      try {
        await env.EMAIL.send(new EmailMessage(m.from, m.to, m.raw));
        results.push({ id: m.id, ok: true });
      } catch (error) {
        results.push({ id: m.id, ok: false, error: String(error).slice(0, 200) });
      }
    }
    await fetch(BASE + "/api/outbox", {
      method: "POST",
      headers: { ...headers, "Content-Type": "application/json" },
      body: JSON.stringify({ results }),
    });
  },
};
