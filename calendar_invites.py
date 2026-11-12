"""Work-calendar invites for trips (optional).

For each trip that should be in the owner's work calendar, the app keeps a set of events: the trip
itself (all-day; for business + private trips one event per run of business days) and one timed
event per flight. Each event is an iCalendar invite (METHOD:REQUEST) emailed to the work address.

The NAS never contacts Cloudflare (its Worker addresses are often unreachable from China). Instead the
Cloudflare Worker that receives the travel emails checks the outbox once a minute through the normal
travel address: GET /api/outbox returns the invites due, the Worker sends them with Cloudflare's email
binding and POSTs the results back. Changes wait until the trip has not changed for a few minutes, so
editing does not send a stream of updates. A changed event is sent again with the same UID and a higher
SEQUENCE (Outlook updates it); a removed one is cancelled (METHOD:CANCEL).
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import format_datetime, make_msgid
import hashlib
import json
import re
import time
from zoneinfo import ZoneInfo

SETTLE_SECONDS = 5 * 60          # a trip must stay unchanged this long before an invite goes out
MAX_PER_POLL = 10
PRODID = "-//Travel Manager//Work calendar//EN"
ADDRESS = re.compile(r"[a-z0-9._%+'-]{1,64}@[a-z0-9-]{1,63}(\.[a-z0-9-]{1,63})+")


class CalendarError(Exception):
    def __init__(self, status, message, code):
        super().__init__(message)
        self.status, self.message, self.code = status, message, code


# ---------------------------------------------------------------- which events a trip should have

def _valid_date(value):
    try:
        return date.fromisoformat(value) if isinstance(value, str) and len(value) == 10 else None
    except ValueError:
        return None


def _utc(day, clock, zone):
    when = datetime.fromisoformat(day + "T" + clock)
    return when.replace(tzinfo=ZoneInfo(zone)).astimezone(timezone.utc)


def trip_enabled(trip):
    mode = str(trip.get("calendar") or "")
    if mode in ("on", "off"):
        return mode == "on"
    return trip.get("type") in ("Business", "Mixed")


def desired_events(state, today):
    """{uid: event} for every trip that should be in the work calendar. Trips that ended are left alone."""
    events = {}
    activities = state.get("activities", [])
    for trip in state.get("trips", []):
        start, end = _valid_date(trip.get("startDate")), _valid_date(trip.get("endDate"))
        if not start or not end or end < start or end < today or not trip_enabled(trip) or trip.get("sample") is True:
            continue
        trip_id = str(trip.get("tripId"))
        private = set(str(trip.get("privateDays") or "").split("|")) if trip.get("type") == "Mixed" else set()
        days = [start + timedelta(days=i) for i in range((end - start).days + 1)]
        runs, current = [], []
        for day in days:
            if day.isoformat() in private:
                if current:
                    runs.append(current)
                current = []
            else:
                current.append(day)
        if current:
            runs.append(current)
        flights = []
        for a in activities:
            if a.get("tripId") != trip_id or a.get("category") != "Flight" or str(a.get("status")) == "Cancelled":
                continue
            if a.get("startDate") in private:
                continue
            try:
                begin = _utc(a["startDate"], a["startTime"], a["startZone"])
                finish = _utc(a["endDate"], a["endTime"], a["endZone"]) if a.get("endDate") and a.get("endTime") and a.get("endZone") else begin + timedelta(hours=1)
            except (KeyError, ValueError, TypeError):
                continue  # no exact time yet; the trip event still lists it
            if finish <= begin:
                finish = begin + timedelta(hours=1)
            flights.append((a, begin, finish))
        flights.sort(key=lambda item: item[1])
        mixed = trip.get("type") == "Mixed"
        for index, run in enumerate(runs):
            title = str(trip.get("name") or "Trip") + (" (business days)" if mixed and private & {d.isoformat() for d in days} else "")
            description = "\n".join(filter(None, [
                str(trip.get("destination") or ""),
                "%s – %s" % (run[0].strftime("%d %b %Y"), run[-1].strftime("%d %b %Y")),
                "Each flight is a separate event." if flights and index == 0 else "",
                "From Travel Manager. Changes to the trip update this event."]))
            events["trip-%s-%d@travel-manager" % (trip_id, index)] = {
                "kind": "trip", "tripId": trip_id, "summary": title, "location": str(trip.get("destination") or ""),
                "description": description, "allDay": True, "start": run[0].isoformat(), "end": (run[-1] + timedelta(days=1)).isoformat()}
        for a, begin, finish in flights:
            details = [
                "%s → %s" % (a.get("from") or "", a.get("to") or "") if a.get("from") or a.get("to") else "",
                "Departs %s %s (%s)" % (a.get("startDate"), a.get("startTime"), a.get("startZone")),
                "Arrives %s %s (%s)" % (a.get("endDate"), a.get("endTime"), a.get("endZone")) if a.get("endTime") else "",
                " · ".join(filter(None, [str(a.get("provider") or ""), ("Booking " + str(a["confirmation"])) if a.get("confirmation") else ""]))]
            events["flight-%s@travel-manager" % a.get("activityId")] = {
                "kind": "flight", "tripId": trip_id, "summary": "✈ " + str(a.get("title") or "Flight"),
                "location": str(a.get("from") or ""), "description": "\n".join(filter(None, details)), "allDay": False,
                "start": begin.strftime("%Y%m%dT%H%M%SZ"), "end": finish.strftime("%Y%m%dT%H%M%SZ")}
    return events


HASH_VERSION = "2"


def event_hash(event):
    """Changes that are worth an update in Outlook: title, place, dates/times, booking reference.
    Wording changes in the description alone do not send an update."""
    key = {k: event.get(k) for k in ("summary", "location", "allDay", "start", "end")}
    reference = re.search(r"Booking (\S+)", event.get("description", "")) if event.get("kind") == "flight" else None
    key["reference"] = reference.group(1) if reference else ""
    return hashlib.sha256(json.dumps(key, sort_keys=True, ensure_ascii=True).encode("ascii")).hexdigest()


# ---------------------------------------------------------------- iCalendar and the email

def _escape(text):
    return str(text).replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\r\n", "\n").replace("\n", "\\n")


def _fold(line):
    """Lines longer than 75 octets continue on the next line after a space (RFC 5545)."""
    data, out = line.encode("utf-8"), []
    while len(data) > 75:
        cut = 75 if not out else 74
        while cut > 0 and (data[cut] & 0xC0) == 0x80:  # never split a UTF-8 character
            cut -= 1
        out.append(data[:cut])
        data = data[cut:]
    out.append(data)
    return "\r\n ".join(part.decode("utf-8") for part in out)


def ics(event, uid, sequence, method, organizer, attendee, now=None):
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%SZ")
    if event["allDay"]:
        when = ["DTSTART;VALUE=DATE:" + event["start"].replace("-", ""), "DTEND;VALUE=DATE:" + event["end"].replace("-", ""),
                "TRANSP:TRANSPARENT", "X-MICROSOFT-CDO-BUSYSTATUS:FREE", "X-MICROSOFT-CDO-ALLDAYEVENT:TRUE"]
    else:
        when = ["DTSTART:" + event["start"], "DTEND:" + event["end"], "TRANSP:OPAQUE", "X-MICROSOFT-CDO-BUSYSTATUS:BUSY"]
    lines = ["BEGIN:VCALENDAR", "PRODID:" + PRODID, "VERSION:2.0", "CALSCALE:GREGORIAN", "METHOD:" + method,
             "BEGIN:VEVENT", "UID:" + uid, "SEQUENCE:%d" % sequence, "DTSTAMP:" + stamp, *when,
             "SUMMARY:" + _escape(event["summary"]), "LOCATION:" + _escape(event["location"]),
             "DESCRIPTION:" + _escape(event["description"]),
             "ORGANIZER;CN=Travel Manager:mailto:" + organizer,
             "ATTENDEE;ROLE=REQ-PARTICIPANT;PARTSTAT=NEEDS-ACTION;RSVP=FALSE:mailto:" + attendee,
             "STATUS:" + ("CANCELLED" if method == "CANCEL" else "CONFIRMED"),
             "END:VEVENT", "END:VCALENDAR"]
    return "\r\n".join(_fold(line) for line in lines) + "\r\n"


def invite_email(event, uid, sequence, method, sender, recipient, now=None):
    """The raw email (str) for one invite: a short text plus the text/calendar part Outlook reads."""
    message = EmailMessage()
    cancelled = method == "CANCEL"
    message["From"] = "Travel Manager <" + sender + ">"
    message["To"] = recipient
    message["Subject"] = ("Cancelled: " if cancelled else "Updated: " if sequence else "") + event["summary"]
    message["Date"] = format_datetime(now or datetime.now(timezone.utc))
    message["Message-ID"] = make_msgid(domain=sender.rsplit("@", 1)[-1])
    message.set_content(("This trip event was removed from Travel Manager.\n\n" if cancelled else "") + event["description"] + "\n")
    message.add_alternative(ics(event, uid, sequence, method, sender, recipient, now), subtype="calendar",
                            params={"method": method})
    return message.as_string()


# ---------------------------------------------------------------- outbox (what the Worker sends)

class Calendar:
    def __init__(self, store):
        self.store = store
        with store.connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS invites (uid TEXT PRIMARY KEY, trip_id TEXT NOT NULL, sequence INTEGER NOT NULL, "
                       "sent_hash TEXT NOT NULL, sent_event TEXT NOT NULL, sent_at REAL NOT NULL, status TEXT NOT NULL, "
                       "pending_hash TEXT NOT NULL, pending_since REAL NOT NULL, error TEXT NOT NULL, pending_event TEXT NOT NULL DEFAULT '')")
            if (db.execute("SELECT value FROM meta WHERE key='calendar_hash_version'").fetchone() or [""])[0] != HASH_VERSION:
                for row in db.execute("SELECT uid, sent_event FROM invites WHERE status='sent' AND sent_event<>''").fetchall():
                    db.execute("UPDATE invites SET sent_hash=? WHERE uid=?", (event_hash(json.loads(row["sent_event"])), row["uid"]))
                db.execute("INSERT INTO meta VALUES ('calendar_hash_version', ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (HASH_VERSION,))

    # settings ------------------------------------------------------
    def settings(self):
        with self.store.connect() as db:
            rows = db.execute("SELECT sent_event, sent_at, status, error, pending_hash FROM invites ORDER BY MAX(sent_at, pending_since) DESC LIMIT 15").fetchall()
            waiting = db.execute("SELECT COUNT(*) FROM invites WHERE pending_hash<>''").fetchone()[0]
        recent = []
        for r in rows:
            event = json.loads(r["sent_event"]) if r["sent_event"] else {}
            recent.append({"summary": event.get("summary", ""), "sentAt": r["sent_at"], "status": r["status"], "error": r["error"],
                           "waiting": bool(r["pending_hash"])})
        last = self.store.meta_get("calendar_last_poll")
        return {"enabled": self.store.meta_get("calendar_enabled") == "1", "to": self.store.meta_get("calendar_to") or "",
                "from": self.store.meta_get("calendar_from") or "", "lastPoll": float(last) if last else None,
                "waiting": waiting, "recent": recent}

    def save_settings(self, payload):
        if set(payload) - {"enabled", "to", "from"} or not isinstance(payload.get("enabled", False), bool):
            raise CalendarError(400, "Supply enabled, to and from.", "invalid_settings")
        to = str(payload.get("to") or "").strip().lower()
        sender = str(payload.get("from") or "").strip().lower()
        for value in (to, sender):
            if value and not ADDRESS.fullmatch(value):
                raise CalendarError(400, "This is not a valid email address: " + value[:80], "invalid_settings")
        if payload.get("enabled") and not (to and sender):
            raise CalendarError(400, "Enter your work address and the travel address first.", "invalid_settings")
        self.store.meta_set({"calendar_enabled": "1" if payload.get("enabled") else "0", "calendar_to": to, "calendar_from": sender})
        return self.settings()

    def resend(self, trip_id, now=None, today=None):
        """Send a trip's invites again (with a higher SEQUENCE) on Cloudflare's next check, about a minute."""
        if self.store.meta_get("calendar_enabled") != "1":
            raise CalendarError(400, "Work calendar invites are switched off (Documents → Work calendar invites).", "calendar_disabled")
        events = {uid: e for uid, e in desired_events(self.store.metadata(), today or date.today()).items() if e["tripId"] == trip_id}
        if not events:
            raise CalendarError(400, "This trip is not sent to your work calendar: it has ended, is private, or is set to Don’t send.", "not_sent")
        with self.store.connect() as db:
            for uid, event in events.items():
                digest = event_hash(event)
                if db.execute("SELECT 1 FROM invites WHERE uid=?", (uid,)).fetchone():
                    db.execute("UPDATE invites SET sent_hash='resend', pending_hash=?, pending_since=0, error='' WHERE uid=?", (digest, uid))
                else:
                    db.execute("INSERT INTO invites VALUES (?, ?, -1, '', '', 0, 'new', ?, 0, '', '')", (uid, trip_id, digest))
        return {"queued": len(events)}

    # outbox --------------------------------------------------------
    def outbox(self, now=None, today=None):
        """Invites due now. An event waits until it has been unchanged for SETTLE_SECONDS."""
        now = now if now is not None else time.time()
        self.store.meta_set({"calendar_last_poll": repr(now)})
        if self.store.meta_get("calendar_enabled") != "1":
            return []
        to, sender = self.store.meta_get("calendar_to") or "", self.store.meta_get("calendar_from") or ""
        if not (to and sender):
            return []
        today = today or date.today()
        desired = desired_events(self.store.metadata(), today)
        due = []
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            rows = {r["uid"]: r for r in db.execute("SELECT * FROM invites")}
            wanted = [(uid, event, event_hash(event)) for uid, event in desired.items()]
            # Sent events that are no longer wanted are cancelled - unless their trip simply ended.
            for uid, row in rows.items():
                if uid not in desired and row["status"] == "sent":
                    event = json.loads(row["sent_event"])
                    finished = (event["end"][:8] if not event["allDay"] else event["end"].replace("-", "")) < today.strftime("%Y%m%d")
                    if not finished:
                        wanted.append((uid, event, "CANCEL"))
            for uid, event, digest in wanted:
                row = rows.get(uid)
                if row is not None and row["sent_hash"] == digest:
                    if row["pending_hash"]:
                        db.execute("UPDATE invites SET pending_hash='', pending_since=0 WHERE uid=?", (uid,))
                    continue
                if row is None:
                    db.execute("INSERT INTO invites VALUES (?, ?, -1, '', '', 0, 'new', ?, ?, '', '')", (uid, event["tripId"], digest, now))
                    continue
                if row["pending_hash"] != digest:
                    db.execute("UPDATE invites SET pending_hash=?, pending_since=? WHERE uid=?", (digest, now, uid))
                    continue
                if now - row["pending_since"] < SETTLE_SECONDS or len(due) >= MAX_PER_POLL:
                    continue
                method = "CANCEL" if digest == "CANCEL" else "REQUEST"
                sequence = row["sequence"] + 1
                # Remember exactly what was sent, so a later cancellation can describe it.
                db.execute("UPDATE invites SET pending_event=? WHERE uid=?", (json.dumps(event, ensure_ascii=True), uid))
                due.append({"id": "%s|%d|%s" % (uid, sequence, digest), "from": sender, "to": to,
                            "raw": invite_email(event, uid, sequence, method, sender, to)})
        return due

    def acknowledge(self, results, now=None):
        now = now if now is not None else time.time()
        if not isinstance(results, list) or len(results) > 50:
            raise CalendarError(400, "Supply results as a list.", "invalid_results")
        with self.store.connect() as db:
            for item in results:
                if not isinstance(item, dict) or not isinstance(item.get("id"), str):
                    continue
                try:
                    uid, sequence, digest = item["id"].rsplit("|", 2)
                    sequence = int(sequence)
                except ValueError:
                    continue
                row = db.execute("SELECT * FROM invites WHERE uid=?", (uid,)).fetchone()
                if row is None:
                    continue
                if item.get("ok") is True:
                    event = row["sent_event"] if digest == "CANCEL" or not row["pending_event"] else row["pending_event"]
                    db.execute("UPDATE invites SET sequence=?, sent_hash=?, sent_event=?, sent_at=?, status=?, error='', "
                               "pending_hash=CASE WHEN pending_hash=? THEN '' ELSE pending_hash END WHERE uid=?",
                               (sequence, digest, event, now, "cancelled" if digest == "CANCEL" else "sent", digest, uid))
                else:
                    # Try again after another settle period.
                    db.execute("UPDATE invites SET error=?, pending_since=? WHERE uid=?",
                               (str(item.get("error") or "Sending failed.")[:300], now, uid))
        return {"ok": True}
