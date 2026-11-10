# Bookings by email — setup

Forward a booking confirmation to your travel address (`travel@businessintels.com`). Travel Manager reads the email **and its attachments** (PDF, photos, Word, emails forwarded as attachments) with Claude and adds the bookings the next time you open the app. Uncertain bookings go to review, as with uploads.

Already done: Email Routing is on for `businessintels.com`, and the rule `travel@…` currently forwards to your inbox.

## 1. Travel Manager (after the update is installed)
1. Open the app → **Documents** → **Bookings by email**.
2. Click **Create email key**. Copy the key; it is shown only once. Keep this tab open until step 2.4 is done.
3. Under **Allowed senders**, enter every address you will forward from (e.g. your personal and work addresses), one per line.
4. Tick **Accept emails** → **Save**. The badge shows **On**.

## 2. Cloudflare Worker (once)
1. Cloudflare dashboard → **Workers & Pages** → **Create** → **Create Worker** (start from "Hello World"). Name: `travel-inbound` → **Deploy**.
2. **Edit code** → replace everything with the contents of `tools/cloudflare_inbound_worker.js` → **Deploy**.
3. Worker → **Settings** → **Variables and secrets** → **Add**:
   - Type **Secret**, name `INBOUND_SECRET`, value: the key from step 1.2.
   - Type **Text**, name `FALLBACK_TO`, value: your verified inbox. If Travel Manager cannot take an email (NAS offline), it goes there instead of being lost.
4. **Email** → **Email Routing** → businessintels.com → **Routing rules** → `travel@…` → **Edit** → Action **Send to a Worker** → `travel-inbound` → **Save**.

## 3. Test
Forward one airline confirmation with its PDF to `travel@businessintels.com` from an allowed address. Within a minute, **Recent emails** in the app shows **Added**; open or reload the app and the flights appear in their trip.

## If something goes wrong
- **Recent emails says "Refused · sender not on the allowed list":** add that exact address under Allowed senders.
- **"Refused · sender could not be verified":** the sender's mail server did not pass Cloudflare's checks (SPF/DKIM/DMARC). Forward from another address, or send the Activity log entry to Claude.
- **Nothing in Recent emails:** check Cloudflare's Email Routing **Activity log**. If the email went to your inbox (fallback), the key in the Worker probably does not match: create a new key and update `INBOUND_SECRET`.
- **A new key** in the app replaces the old one immediately; update the Worker secret straight after.
