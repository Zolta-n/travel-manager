"""Bookings by email.

Cloudflare Email Routing hands each email for the travel address to a small Email Worker, which
POSTs the raw message to /api/inbound with a secret key. The app checks the key and the sender,
keeps the email in a private spool folder and answers at once. A background thread then splits
the email (in the sandboxed reader), stores the email text and each attachment as documents and
lets Claude read the email and its attachments together. Devices apply the finished reading the
next time the app is open, exactly like a reading started on another device.

Never logged: addresses, subjects, bodies or attachment names. Only counts and result codes.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
from pathlib import Path
import re
import secrets
import threading
import time

from email import policy
from email.parser import BytesHeaderParser
from email.utils import getaddresses

LOGGER = logging.getLogger("travel_manager")
MAX_EMAIL_BYTES = 25 * 1024 * 1024        # the Cloudflare Email Routing maximum
MAX_SENDERS = 20
FAIL_WINDOW, FAIL_LIMIT = 600, 5          # wrong keys per client before a pause
KEEP_DAYS = 400                           # remembered Message-IDs (duplicates are ignored)
STORAGE_MARGIN = 1024 * 1024
CALENDAR_REPLY = re.compile(rb'text/calendar[^:]{0,300}?method="?reply', re.I | re.S)
ADDRESS = re.compile(r"[a-z0-9._%+'-]{1,64}@[a-z0-9-]{1,63}(\.[a-z0-9-]{1,63})+")


class InboundError(Exception):
    def __init__(self, status, message, code):
        super().__init__(message)
        self.status, self.message, self.code = status, message, code


def _address(value):
    pairs = [a.strip().lower() for _, a in getaddresses([value or ""]) if a]
    return pairs[0] if len(pairs) == 1 and ADDRESS.fullmatch(pairs[0]) else ""


def _aligned(domain, other):
    other = other.strip().strip('";').lower()
    return bool(other) and (other == domain or domain.endswith("." + other) or other.endswith("." + domain))


def sender_verified(headers, sender):
    """True when the receiving server's own authentication result (the topmost Authentication-Results
    or ARC-Authentication-Results header; Cloudflare adds its result above any copied from the sender)
    shows DMARC, or DKIM/SPF aligned with the sender's domain, passed."""
    domain = sender.rsplit("@", 1)[-1]
    top = next((str(v) for k, v in headers.items() if k.lower() in ("authentication-results", "arc-authentication-results")), "")
    top = top.lower()
    if not top:
        return False
    dmarc = re.search(r"\bdmarc=pass\b[^;]*", top)
    if dmarc:
        source = re.search(r"header\.from=([a-z0-9.-]+)", dmarc.group(0))
        if source is None or _aligned(domain, source.group(1)):
            return True
    for result in re.finditer(r"\bdkim=pass\b[^;]*?header\.(?:d|i)=@?([a-z0-9.-]+)", top):
        if _aligned(domain, result.group(1)):
            return True
    for result in re.finditer(r"\bspf=pass\b[^;]*?smtp\.mailfrom=(?:[^@\s;]*@)?([a-z0-9.-]+)", top):
        if _aligned(domain, result.group(1)):
            return True
    return False


def _data_uri(mime, binary):
    return "data:" + mime + ";base64," + base64.b64encode(binary).decode("ascii")


class Inbound:
    def __init__(self, app):
        self.app = app
        self.store = app.store
        self.spool = self.store.root / "inbound"
        self.spool.mkdir(mode=0o700, exist_ok=True)
        with self.store.connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS inbound_mail (key TEXT PRIMARY KEY, received REAL NOT NULL, "
                       "status TEXT NOT NULL, detail TEXT NOT NULL, document_id TEXT NOT NULL)")
        self.wake = threading.Event()
        self.thread = None
        self.thread_lock = threading.Lock()
        with self.store.connect() as db:
            if db.execute("SELECT 1 FROM inbound_mail WHERE status='received' LIMIT 1").fetchone():
                self._start()  # emails received before a restart

    def _start(self):
        with self.thread_lock:
            if self.thread is None:
                self.thread = threading.Thread(target=self._loop, daemon=True, name="inbound-mail")
                self.thread.start()
        self.wake.set()

    # ------------------------------------------------------------ settings (owner, signed in)

    def settings(self):
        with self.store.connect() as db:
            events = [{"received": r["received"], "status": r["status"], "detail": r["detail"]}
                      for r in db.execute("SELECT received, status, detail FROM inbound_mail ORDER BY received DESC LIMIT 15")]
        return {"enabled": self.store.meta_get("inbound_enabled") == "1", "keySet": bool(self.store.meta_get("inbound_key")),
                "senders": json.loads(self.store.meta_get("inbound_senders") or "[]"), "events": events,
                "aiEnabled": self.app.ai.enabled}

    def save_settings(self, payload):
        if set(payload) - {"enabled", "senders"} or not isinstance(payload.get("enabled", False), bool):
            raise InboundError(400, "Supply enabled (true/false) and senders (a list of email addresses).", "invalid_settings")
        senders = payload.get("senders", [])
        if not isinstance(senders, list) or len(senders) > MAX_SENDERS:
            raise InboundError(400, "Enter at most %d sender addresses." % MAX_SENDERS, "invalid_settings")
        clean = []
        for value in senders:
            address = value.strip().lower() if isinstance(value, str) else ""
            if not ADDRESS.fullmatch(address):
                raise InboundError(400, "This is not a valid email address: " + str(value)[:80], "invalid_settings")
            if address not in clean:
                clean.append(address)
        enabled = payload.get("enabled", False)
        if enabled and not clean:
            raise InboundError(400, "Add at least one sender address before turning this on.", "invalid_settings")
        if enabled and not self.store.meta_get("inbound_key"):
            raise InboundError(400, "Create the email key first.", "invalid_settings")
        self.store.meta_set({"inbound_enabled": "1" if enabled else "0", "inbound_senders": json.dumps(clean)})
        return self.settings()

    def new_key(self):
        """A new key for the Cloudflare Worker. Shown once; only its hash is kept. Replaces any old key."""
        key = "tmin_" + secrets.token_urlsafe(32)
        self.store.meta_set({"inbound_key": hashlib.sha256(key.encode("ascii")).hexdigest()})
        return {"key": key}

    # ------------------------------------------------------------ receiving (Cloudflare Worker)

    def check_key(self, env):
        client = "inbound|" + self.app.client_id(env)
        client = hashlib.sha256(client.encode("utf-8")).hexdigest()
        now = time.time()
        with self.store.connect() as db:
            db.execute("DELETE FROM login_attempts WHERE attempted_at<?", (now - FAIL_WINDOW,))
            if db.execute("SELECT COUNT(*) FROM login_attempts WHERE client=?", (client,)).fetchone()[0] >= FAIL_LIMIT:
                raise InboundError(429, "Too many wrong keys. Try again later.", "rate_limited")
        stored = self.store.meta_get("inbound_key")
        if not stored:
            raise InboundError(403, "Bookings by email are not set up in Travel Manager.", "inbound_disabled")
        supplied = env.get("HTTP_X_INBOUND_SECRET", "")
        if not supplied or not hmac.compare_digest(hashlib.sha256(supplied.encode("utf-8", "replace")).hexdigest(), stored):
            with self.store.connect() as db:
                db.execute("INSERT INTO login_attempts VALUES (?, ?)", (client, now))
            raise InboundError(401, "Wrong email key.", "bad_key")

    def _event(self, status, detail, key=None, document_id=""):
        with self.store.connect() as db:
            db.execute("DELETE FROM inbound_mail WHERE received<?", (time.time() - KEEP_DAYS * 86400,))
            db.execute("INSERT OR REPLACE INTO inbound_mail VALUES (?, ?, ?, ?, ?)",
                       (key or "event-" + secrets.token_hex(8), time.time(), status, detail, document_id))

    def receive(self, env):
        self.check_key(env)
        if self.store.meta_get("inbound_enabled") != "1":
            raise InboundError(403, "Bookings by email are switched off in Travel Manager.", "inbound_disabled")
        if env.get("CONTENT_TYPE", "").split(";", 1)[0].strip().lower() not in ("message/rfc822", "application/octet-stream"):
            raise InboundError(415, "Send the raw email as message/rfc822.", "unsupported_type")
        try:
            length = int(env.get("CONTENT_LENGTH", ""))
        except ValueError:
            raise InboundError(411, "A Content-Length is required.", "length_required") from None
        if not 0 < length <= MAX_EMAIL_BYTES:
            self._event("rejected", "too large (over 25 MB)")
            raise InboundError(413, "The email is larger than 25 MB.", "too_large")
        raw = env["wsgi.input"].read(length)
        if len(raw) != length:
            raise InboundError(400, "The email was cut off.", "incomplete")
        if CALENDAR_REPLY.search(raw[:200000]):
            # "Accepted / Declined" answers to the work-calendar invites come back here: nothing to read.
            self._event("ignored", "calendar reply")
            return {"accepted": True, "duplicate": False, "ignored": True}
        headers = BytesHeaderParser(policy=policy.default).parsebytes(raw)
        try:
            sender = _address(str(headers.get("From", "")))
        except Exception:
            sender = ""
        allowed = json.loads(self.store.meta_get("inbound_senders") or "[]")
        if not sender or sender not in allowed:
            self._event("rejected", "sender not on the allowed list")
            raise InboundError(403, "This sender may not add bookings.", "sender_not_allowed")
        if not sender_verified(headers, sender):
            self._event("rejected", "sender could not be verified (DKIM/SPF/DMARC)")
            raise InboundError(403, "The sender could not be verified.", "sender_unverified")
        try:
            message_id = str(headers.get("Message-ID", "")).strip()[:500]
        except Exception:
            message_id = ""
        key = hashlib.sha256(("mid:" + message_id if message_id else "raw:").encode("utf-8", "replace") +
                             (b"" if message_id else raw)).hexdigest()
        with self.store.connect() as db:
            if db.execute("SELECT 1 FROM inbound_mail WHERE key=?", (key,)).fetchone():
                return {"accepted": True, "duplicate": True}
        target = self.spool / (key + ".eml")
        with open(target, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(target, 0o600)
        self._event("received", "waiting to be read", key)
        self._start()
        return {"accepted": True, "duplicate": False}

    # ------------------------------------------------------------ processing (background)

    def _loop(self):
        while True:
            self.wake.wait(60)
            self.wake.clear()
            try:
                self.process_pending()
            except Exception:
                LOGGER.exception("Inbound email processing failed; email contents are never logged")

    def process_pending(self):
        with self.store.connect() as db:
            keys = [r["key"] for r in db.execute("SELECT key FROM inbound_mail WHERE status='received' ORDER BY received")]
        for key in keys:
            path = self.spool / (key + ".eml")
            if not path.is_file():
                self._event("failed", "the stored email is missing", key)
                continue
            try:
                detail, document_id = self._process(key, path)
                self._event("added", detail, key, document_id)
                path.unlink(missing_ok=True)
            except Exception as exc:
                code = getattr(exc, "code", "")
                if code in ("reader_busy", "rate_limited", "overloaded", "ai_busy"):
                    LOGGER.info("Inbound email waits for a free reader (%s)", code)
                    return  # stays queued; retried on the next wake-up
                LOGGER.exception("Inbound email could not be processed")
                self._event("failed", "could not be processed: " + (code or "error"), key)

    def _process(self, key, path):
        from extractor import ReaderError, split_email
        email_id = "mail-" + key[:20]
        received = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        try:
            split = self.app._with_reader(lambda: split_email(path, self.app.work_dir))
        except Exception as exc:
            if getattr(exc, "code", "") == "reader_busy":
                raise
            # Unreadable structure: keep the original email so nothing is lost.
            raw = path.read_bytes()
            if len(raw) > self.store.max_file:
                raise
            self._add_documents(email_id, [{
                **self._base(email_id, "Email " + received[:10] + ".eml", "message/rfc822", raw, received),
                "status": "Review needed · the email could not be split",
                "auto": {"state": "Reader error", "reason": "The email could not be split into text and attachments: " + str(exc)[:200]}}])
            return "stored, could not be split", email_id
        attachments, skipped = split["attachments"], [(s.get("name", ""), s.get("reason", "")) for s in split["skipped"]]
        email_doc = {**self._base(email_id, split["subject"] + ".eml", "message/rfc822", split["email"], received),
                     "emailAttachments": len(attachments),
                     "emailSkipped": "; ".join("%s: %s" % s for s in skipped)[:2000],
                     "status": "Reading on the NAS…" if self.app.ai.enabled else "Received by email · not read yet"}
        children, not_stored = [], []
        budget = self._free_storage() - len(split["email"])
        for index, item in enumerate(attachments):
            child = {**self._base(email_id + "-a%d" % index, item["name"], item["mime"], item["data"], received),
                     "sourceEmail": email_id, "status": "Read together with its email",
                     "auto": {"state": "Already linked", "reason": "Read together with the email “" + split["subject"] + "”."}}
            if len(item["data"]) > budget:
                child.pop("data")
                child.update(size=len(item["data"]), notes="Not stored: the NAS storage limit for documents is reached.")
                not_stored.append(item["name"])
            else:
                budget -= len(item["data"])
            child["category"] = "Invoice" if re.search(r"invoice|receipt|rechnung|beleg", item["name"], re.I) else "Other"
            children.append(child)
        for index, item in enumerate(s for s in split["skipped"] if s.get("data")):
            mime = "application/pdf" if item["data"][:5] == b"%PDF-" else "application/octet-stream"
            child = {**self._base(email_id + "-s%d" % index, item["name"], mime, item["data"], received),
                     "sourceEmail": email_id, "status": "Not read · " + item["reason"],
                     "auto": {"state": "Ignored", "reason": "Not read automatically: " + item["reason"] + "."}}
            if len(item["data"]) > budget:
                child.pop("data")
                child.update(size=len(item["data"]), notes="Not stored: the NAS storage limit for documents is reached.")
            else:
                budget -= len(item["data"])
            children.append(child)
        if not_stored:
            email_doc["emailSkipped"] = (email_doc["emailSkipped"] + "; " if email_doc["emailSkipped"] else "") + \
                "; ".join(n + ": not stored, storage limit reached" for n in not_stored)
        self._add_documents(email_id, [email_doc, *children])
        detail = "%d attachment(s)%s" % (len(attachments), ", %d skipped" % len(skipped) if skipped else "")
        if self.app.ai.enabled:
            try:
                with self.app.jobs.slot:
                    result = self.app.analyze_email(split["text"], attachments, email_doc["name"])
                result["created"] = time.time()
                self.store.save_ai_result(email_id, hashlib.sha256(split["email"]).hexdigest(), result, result["created"])
                detail += ", read by Claude"
            except Exception as exc:
                if getattr(exc, "code", "") == "reader_busy":
                    raise
                LOGGER.warning("Inbound email stored but AI reading failed (%s)", getattr(exc, "code", "error"))
                self._update_document(email_id, {"status": "Review needed · Claude could not read it",
                                                 "auto": {"state": "Reader error", "reason": str(exc)[:300] or "AI reading failed.",
                                                          "processedAt": received}})
                detail += ", reading failed"
        LOGGER.info("Inbound email stored: %s", detail)
        return detail, email_id

    @staticmethod
    def _base(document_id, name, mime, binary, received):
        return {"documentId": document_id, "tripId": "", "activityId": "", "name": name, "mime": mime,
                "category": "Other", "confirmation": "", "status": "", "notes": "", "data": _data_uri(mime, binary),
                "source": "email", "emailReceived": received}

    def _free_storage(self):
        state = self.store.metadata()
        used = sum(int(d.get("size") or 0) for d in state.get("documents", []) if d.get("hash"))
        return self.store.max_current - used - STORAGE_MARGIN

    def _change_state(self, change):
        from store import Conflict
        for _ in range(8):
            current = self.store.get(originals=False)
            state = current["state"]
            if not change(state):
                return
            try:
                return self.store.put(state, current["revision"])
            except Conflict:
                time.sleep(0.25)
        raise RuntimeError("The travel data kept changing; try again.")

    def _add_documents(self, email_id, documents):
        def change(state):
            if any(d.get("documentId") == email_id for d in state["documents"]):
                return False  # already added before a restart
            state["documents"].extend(documents)
            return True
        self._change_state(change)

    def _update_document(self, document_id, values):
        def change(state):
            document = next((d for d in state["documents"] if d.get("documentId") == document_id), None)
            if document is None:
                return False
            document.update(values)
            return True
        self._change_state(change)
