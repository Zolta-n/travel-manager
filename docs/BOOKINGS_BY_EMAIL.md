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

# Work calendar invites (optional)

Trips are sent to your work email as Outlook invites. The trip is an all-day event; for business + private trips, only the business days are included. Each flight is a separate event at its local time.
- Business trips are sent automatically. Private trips are sent only when the trip's **Work calendar invite** setting is *Send*. *Don't send* stops any trip.
- A change goes out as an update once the trip has not changed for 5 minutes. A removed trip or flight is cancelled in Outlook.
- Trips that have already ended are never sent. Trip notes and flight notes are never included.

The Worker that receives your emails also sends the invites. It checks Travel Manager once a minute, so your NAS never has to contact Cloudflare.

## Setup (once)
1. **Verify your work address:** Cloudflare → Email Routing → businessintels.com → **Destination addresses** → **Add destination address** → your work email. Click the link in the verification email.
2. **Update the Worker code:** Workers & Pages → `travel-inbound` → **Edit code**. Replace everything with the current `tools/cloudflare_inbound_worker.js`, then click **Deploy**.
3. **Add the email binding:** Worker → **Settings** → **Bindings** → **Add binding** → **Send email**. Variable name: `EMAIL`. Destination: your work address. Then **Deploy**.
4. **Add the minute trigger:** Worker → **Settings** → **Trigger events** → **Add** → **Cron trigger** → every minute (`* * * * *`). Then **Deploy**.
5. **In Travel Manager:** Documents → **Work calendar invites**. Enter your work email and the travel address (`travel@businessintels.com`), tick **Send invites**, then **Save**. The note should say Cloudflare checks every minute.

The first invites arrive about 6 minutes later. **Recent invites** in the panel shows what was sent, and any error.
