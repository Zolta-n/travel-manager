// Cloudflare Email Worker "travel-inbound" (not part of the NAS package).
// Paste this into Cloudflare → Workers & Pages → travel-inbound → Edit code.
// Settings → Variables and secrets:
//   INBOUND_SECRET  (Secret)  the email key from Travel Manager → Documents → Bookings by email
//   FALLBACK_TO     (Text)    optional: a verified inbox; used when Travel Manager cannot take the email
// Email Routing → Routing rules → travel@… → Action: Send to a Worker → travel-inbound.

const APP = "https://travel.businessintels.com/api/inbound";

export default {
  async email(message, env) {
    const raw = await new Response(message.raw).arrayBuffer();
    let response = null;
    try {
      response = await fetch(APP, {
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
};
