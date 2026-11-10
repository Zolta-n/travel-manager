# Next upgrade: bookings by email (inbound mail → automatic reading)

Status: **planned, not built.** Agreed with the owner on 2026-11-09/10.

## Goal
The owner forwards booking confirmations to one email address. Travel Manager receives each email **automatically**, reads the email body **and all attachments**, and adds the bookings to the itinerary. No manual import.

## Decisions already taken
- **Address:** `travel@businessintels.com`, **not** an address at `travel.businessintels.com`. That hostname is a tunnel CNAME, and MX records cannot share a name with a CNAME.
- **Mail service:** Cloudflare Email Routing on the existing `businessintels.com` zone (free). No mailbox, no IMAP.
- **Delivery to the app:** a Cloudflare **Email Worker** POSTs the raw email to the app through the existing tunnel. Polling Gmail/IMAP from the NAS was rejected because the NAS is in China, where Gmail is unreachable without the VPN.
- **The Worker stays dumb:** it forwards the raw RFC 822 message unchanged. All parsing, attachment handling and reading happens in the app, so later changes are a normal PR + Watchtower deploy.
- Outbound mail (`digest@businessintels.com` via Resend, another app) is unaffected. Email Routing only adds inbound MX records.

## Flow
```
owner forwards mail → travel@businessintels.com
  → Cloudflare Email Routing → Email Worker "travel-inbound"
  → POST https://travel.businessintels.com/api/inbound   (header X-Inbound-Secret, body = raw .eml)
  → app: verify secret + sender → store .eml as a document original → split parts
  → AI reading per part (existing ai_reader pipeline) → auto-add clean segments, rest to review queue
```

## Part 1 — Cloudflare Email Worker (outside this repo)
Create in Cloudflare → Workers → Create → name `travel-inbound`. Add the secret `INBOUND_SECRET` (Settings → Variables → Secret) with the same value as in the app. Then Email Routing → Routing rules → `travel@businessintels.com` → action **Send to a Worker** → `travel-inbound`.

```js
export default {
  async email(message, env) {
    const raw = await new Response(message.raw).arrayBuffer();
    const res = await fetch("https://travel.businessintels.com/api/inbound", {
      method: "POST",
      headers: {
        "Content-Type": "message/rfc822",
        "X-Inbound-Secret": env.INBOUND_SECRET,
        "X-Envelope-From": message.from,
      },
      body: raw,
    });
    if (res.status >= 500) {
      // Temporary failure (NAS offline): make the sender's server retry later.
      throw new Error("Travel Manager unavailable: " + res.status);
    }
    if (!res.ok) message.setReject("Not accepted by Travel Manager");
  },
};
```
Optional: keep a copy in the repo as `tools/cloudflare_inbound_worker.js` (not part of the NAS package, so not in the 16-file allowlist).

**If Cloudflare Access protects `travel.businessintels.com`:** add a bypass policy for the path `/api/inbound` (or use an Access service token in the Worker). Otherwise the Worker gets the Access login page.

## Part 2 — App changes (this repo)

### Endpoint `POST /api/inbound`
- Machine-to-machine: **no session cookie, no CSRF**, but Host check stays (`TRAVEL_ALLOWED_HOSTS`).
- Auth: `X-Inbound-Secret` compared with `hmac.compare_digest` against the stored secret. Missing secret configured → 503 `inbound_disabled`. Wrong → 401, counted in the per-client sign-in limits (CF-Connecting-IP).
- Body limit: 25 MB (the Cloudflare Email Routing maximum). Content-Type `message/rfc822`.
- **Sender allowlist:** accept only when the `From` header (and `X-Envelope-From`) matches an owner-configured list. Otherwise 403, logged without addresses or contents. Keep the list in settings, **not in the repo** (the repo may be public).
- Idempotent: dedupe on `Message-ID` plus the content hash, so Worker retries never create duplicates.
- Respond fast (< 10 s): store the email, queue the reading as a background job (`JobBoard`), return `202 {"documentId": …}`. Cloudflare cuts at 100 s.

### Secret management
Prefer a **Settings button in the app**: "Generate inbound email key". It shows the key once, stores only a hash in `store.meta`, and a "rotate" option invalidates the old key. This avoids installer/compose changes (no install task run needed). The alternative is an `install.sh --set-inbound-secret` plus an env var in `compose.yaml`, which needs the DSM install task.

### Reading emails with attachments
Today `reader_worker.read_email()` **skips attachments** (`get_content_disposition() == "attachment"` → `continue`) and only returns body text. Change this:
- Walk the MIME tree in the sandboxed worker (keep the 200-part limit), and return the body text plus a list of attachments (name, MIME type, bytes).
- Handle each attachment:

| Type | Handling |
|---|---|
| PDF | Claude `document` block (existing path, 12 MB limit) |
| JPEG/PNG/WebP/GIF/HEIC | `prepare_ai_image` → Claude `image` block |
| `.ics` | Parse deterministically (VEVENT DTSTART/DTEND/TZID/LOCATION), no AI; good cross-check for times |
| `.pkpass` | ZIP → `pass.json` → flight number, seat, times (deterministic) |
| `.docx` | Existing docx reader |
| Nested `message/rfc822` (forwarded as attachment) | Recurse once |
| Encrypted PDF, unknown types, signatures/logos < 10 KB | Skip; list in review reasons |

- Prefer **one Claude call per email**: body text plus all readable attachments as content blocks in the same request. The model can then merge e.g. the body (booking reference) with the PDF (times) into one segment. Fall back to per-attachment calls if the total exceeds limits.
- The existing deterministic checks (dates, zones, durations, trip match) apply unchanged. Only fully clean segments with a clear trip match are auto-added; everything else goes to the review queue with reasons, as today.

### Storage / data
- Store the raw `.eml` as the document original (content-addressed). Store each attachment as its own document original linked to it, so the e-ticket/voucher can be opened from the trip.
- `documents[].auto` gets `{"source": "email", "messageId", "receivedAt", "attachments": [...]}`. That is free-form metadata, so no state migration is needed. State format stays version 2.
- Never log subjects, addresses, bodies or attachment names. Log only counts and result codes.

### UI
- Settings: inbound email on/off, key generate/rotate, allowed sender addresses, and a line showing the address `travel@businessintels.com`.
- Review queue: show the email source ("from email, 2 attachments") and failed or skipped attachments with their reason.

### Tests
- Fixtures: plain-text mail, HTML-only mail, mail with PDF and `.ics`, forwarded-as-attachment, encrypted PDF, oversized, foreign sender, wrong secret, duplicate Message-ID.
- Use the fake Claude client pattern from `tests/test_ai.py`. No real addresses or itinerary data in fixtures.

## Owner steps after merge (DSM/Cloudflare GUI, no SSH)
1. Cloudflare → businessintels.com → Email → Email Routing → enable (adds MX/TXT records automatically).
2. Create the Worker `travel-inbound` from Part 1 and add the secret.
3. In Travel Manager Settings: generate the inbound key, paste it into the Worker secret, and enter the allowed sender addresses.
4. Routing rule `travel@businessintels.com` → Send to Worker.
5. Test: forward one airline confirmation with a PDF and check it appears within a minute.
