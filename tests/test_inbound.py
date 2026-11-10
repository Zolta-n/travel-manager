"""Bookings by email: key, sender checks, duplicates, splitting, storage and reading."""
from email.message import EmailMessage
import hashlib
import io
import json
from pathlib import Path
import shutil
import sys
import unittest
from unittest.mock import patch
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
import ai_reader
from ai_reader import AIConfig, AIError
from app import Application, Config
import inbound
from inbound import Inbound, sender_verified
from store import empty_state
from test_ai import FakeClient, TRIPS, result, segment

PDF = b"%PDF-1.4\n% e-ticket\n" + b"x" * 20000
CF_PASS = "mx.cloudflare.net; dkim=pass header.d=example.com header.s=s1; spf=pass smtp.mailfrom=owner@example.com; dmarc=pass header.from=example.com"


def email(sender="Owner <owner@example.com>", auth=CF_PASS, message_id="<m1@example.com>", attachments=True):
    m = EmailMessage()
    if auth:
        m["Authentication-Results"] = auth
    m["Subject"] = "Fwd: Your flight"
    m["From"] = sender
    m["To"] = "travel@example.org"
    if message_id:
        m["Message-ID"] = message_id
    m.set_content("Booking reference ABC123. Flight MU587 on 11 Nov.")
    if attachments:
        m.add_attachment(PDF, maintype="application", subtype="pdf", filename="e-ticket.pdf")
        m.add_attachment(b"BEGIN:VCALENDAR", maintype="text", subtype="calendar", filename="invite.ics")
    return bytes(m)


class SenderTests(unittest.TestCase):
    def headers(self, *results):
        m = EmailMessage()
        for value in results:
            m["Authentication-Results"] = value
        return m

    def test_alignment_rules(self):
        self.assertTrue(sender_verified(self.headers(CF_PASS), "owner@example.com"))
        self.assertTrue(sender_verified(self.headers("mx.cloudflare.net; dkim=pass header.d=mail.example.com"), "owner@example.com"))
        self.assertFalse(sender_verified(self.headers("mx.cloudflare.net; dkim=pass header.d=attacker.net; spf=fail"), "owner@example.com"))
        self.assertFalse(sender_verified(self.headers("mx.cloudflare.net; dmarc=fail header.from=example.com"), "owner@example.com"))
        self.assertFalse(sender_verified(self.headers(), "owner@example.com"))

    def test_only_the_topmost_result_counts(self):
        # A forged "pass" copied in by the sender sits below the receiving server's own result.
        forged = self.headers("mx.cloudflare.net; dmarc=fail header.from=example.com", CF_PASS)
        self.assertFalse(sender_verified(forged, "owner@example.com"))


class InboundTests(unittest.TestCase):
    def setUp(self):
        self.work = ROOT / (".qa-" + uuid.uuid4().hex)
        self.work.mkdir(mode=0o700)
        seed = empty_state()
        seed["trips"] = [{**TRIPS[0], "notes": "", "sample": False}]
        (self.work / "seed.json").write_text(json.dumps(seed))
        self.config = Config(password="Correct-Horse-Battery-9", allowed_hosts=("travel.example",),
                             data_dir=self.work / "data", web_dir=ROOT / "web", seed_path=self.work / "seed.json",
                             secure_cookie=False, ai=AIConfig(api_key="sk-ant-test"))
        self.no_thread = patch.object(Inbound, "_start", lambda self: None)
        self.no_thread.start()
        self.app = Application(self.config)
        self.fake = FakeClient(result(segment(), trip=""))
        real = ai_reader.call_claude
        self.claude = patch("app.call_claude", side_effect=lambda request, config: real(request, config, self.fake))
        self.claude.start()

    def tearDown(self):
        self.claude.stop()
        self.no_thread.stop()
        shutil.rmtree(self.work)

    def call(self, method, path, body=None, cookie="", csrf="", raw=None, key=None, ip="198.51.100.7"):
        data = raw if raw is not None else json.dumps(body).encode() if body is not None else b""
        env = {"REQUEST_METHOD": method, "PATH_INFO": path, "HTTP_HOST": "travel.example", "wsgi.input": io.BytesIO(data),
               "CONTENT_LENGTH": str(len(data)), "CONTENT_TYPE": "message/rfc822" if raw is not None else "application/json",
               "HTTP_COOKIE": cookie, "HTTP_X_CSRF_TOKEN": csrf, "REMOTE_ADDR": "127.0.0.1", "HTTP_CF_CONNECTING_IP": ip}
        if key is not None:
            env["HTTP_X_INBOUND_SECRET"] = key
        if method != "GET" and raw is None:
            env["HTTP_ORIGIN"] = "http://travel.example"
        captured = {}
        chunks = self.app(env, lambda status, headers: captured.update(status=int(status.split()[0])))
        return captured["status"], json.loads(b"".join(chunks) or b"{}")

    def signed_in(self):
        env_capture = {}
        data = json.dumps({"password": "Correct-Horse-Battery-9"}).encode()
        env = {"REQUEST_METHOD": "POST", "PATH_INFO": "/api/login", "HTTP_HOST": "travel.example", "wsgi.input": io.BytesIO(data),
               "CONTENT_LENGTH": str(len(data)), "CONTENT_TYPE": "application/json", "REMOTE_ADDR": "127.0.0.1",
               "HTTP_ORIGIN": "http://travel.example"}
        chunks = self.app(env, lambda status, headers: env_capture.update(headers=headers))
        cookie = next(v for k, v in env_capture["headers"] if k == "Set-Cookie").split(";")[0]
        return cookie, json.loads(b"".join(chunks))["csrfToken"]

    def enable(self, senders=("owner@example.com",)):
        cookie, csrf = self.signed_in()
        status, body = self.call("POST", "/api/inbound/key", {}, cookie=cookie, csrf=csrf)
        self.assertEqual(status, 200)
        key = body["key"]
        status, body = self.call("POST", "/api/inbound/settings", {"enabled": True, "senders": list(senders)}, cookie=cookie, csrf=csrf)
        self.assertEqual(status, 200, body)
        return key, cookie, csrf

    def documents(self):
        return self.app.store.get(originals=False)["state"]["documents"]

    # ---------------------------------------------------------------- settings

    def test_settings_need_sign_in_and_valid_addresses(self):
        self.assertEqual(self.call("GET", "/api/inbound/settings")[0], 401)
        cookie, csrf = self.signed_in()
        self.assertEqual(self.call("POST", "/api/inbound/key", {}, cookie=cookie)[0], 403)  # CSRF
        status, body = self.call("POST", "/api/inbound/settings", {"enabled": True, "senders": ["owner@example.com"]}, cookie=cookie, csrf=csrf)
        self.assertEqual((status, body["code"]), (400, "invalid_settings"))  # no key yet
        self.call("POST", "/api/inbound/key", {}, cookie=cookie, csrf=csrf)
        self.assertEqual(self.call("POST", "/api/inbound/settings", {"enabled": True, "senders": ["not an address"]}, cookie=cookie, csrf=csrf)[0], 400)
        self.assertEqual(self.call("POST", "/api/inbound/settings", {"enabled": True, "senders": []}, cookie=cookie, csrf=csrf)[0], 400)
        status, body = self.call("POST", "/api/inbound/settings", {"enabled": True, "senders": [" Owner@Example.com "]}, cookie=cookie, csrf=csrf)
        self.assertEqual((status, body["senders"], body["keySet"]), (200, ["owner@example.com"], True))
        self.assertNotIn("key", self.call("GET", "/api/inbound/settings", cookie=cookie)[1])

    # ---------------------------------------------------------------- receiving

    def test_key_and_sender_checks(self):
        self.assertEqual(self.call("POST", "/api/inbound", raw=email(), key="tmin_x")[1]["code"], "inbound_disabled")
        key, cookie, csrf = self.enable()
        self.assertEqual(self.call("POST", "/api/inbound", raw=email(), key="tmin_wrong")[0], 401)
        self.assertEqual(self.call("POST", "/api/inbound", raw=email(sender="Other <someone@else.com>"), key=key)[1]["code"], "sender_not_allowed")
        self.assertEqual(self.call("POST", "/api/inbound", raw=email(auth=""), key=key)[1]["code"], "sender_unverified")
        self.assertEqual(self.call("POST", "/api/inbound", raw=email(auth="mx.cloudflare.net; dmarc=fail"), key=key)[0], 403)
        status, body = self.call("POST", "/api/inbound", raw=email(), key=key)
        self.assertEqual((status, body["duplicate"]), (202, False))
        self.assertEqual(self.call("POST", "/api/inbound", raw=email(), key=key)[1]["duplicate"], True)  # Worker retry
        events = self.call("GET", "/api/inbound/settings", cookie=cookie)[1]["events"]
        self.assertEqual([e["status"] for e in events].count("rejected"), 3)
        self.call("POST", "/api/inbound/settings", {"enabled": False, "senders": ["owner@example.com"]}, cookie=cookie, csrf=csrf)
        self.assertEqual(self.call("POST", "/api/inbound", raw=email(message_id="<m2@x>"), key=key)[1]["code"], "inbound_disabled")

    def test_wrong_keys_are_rate_limited_per_client(self):
        key, _, _ = self.enable()
        for _ in range(5):
            self.assertEqual(self.call("POST", "/api/inbound", raw=email(), key="tmin_wrong", ip="203.0.113.9")[0], 401)
        self.assertEqual(self.call("POST", "/api/inbound", raw=email(), key=key, ip="203.0.113.9")[0], 429)
        self.assertEqual(self.call("POST", "/api/inbound", raw=email(), key=key)[0], 202)

    def test_new_key_replaces_old(self):
        key, cookie, csrf = self.enable()
        self.call("POST", "/api/inbound/key", {}, cookie=cookie, csrf=csrf)
        self.assertEqual(self.call("POST", "/api/inbound", raw=email(), key=key)[0], 401)

    # ---------------------------------------------------------------- processing

    def test_email_and_attachment_are_stored_and_read_together(self):
        key, cookie, csrf = self.enable()
        self.assertEqual(self.call("POST", "/api/inbound", raw=email(), key=key)[0], 202)
        self.app.inbound.process_pending()
        docs = self.documents()
        mail = next(d for d in docs if d.get("source") == "email" and not d.get("sourceEmail"))
        ticket = next(d for d in docs if d.get("sourceEmail") == mail["documentId"])
        self.assertEqual(mail["name"], "Fwd - Your flight.eml")
        self.assertEqual((mail["emailAttachments"], ticket["name"], ticket["auto"]["state"]), (1, "e-ticket.pdf", "Already linked"))
        self.assertIn("invite.ics: calendar file", mail["emailSkipped"])
        # The stored email is small: the attachment is kept once, as its own document.
        self.assertLess(mail["size"], 2000)
        self.assertEqual(ticket["size"], len(PDF))
        # One Claude call with the email text and the PDF together.
        sent = self.fake.requests[0]["messages"][0]["content"]
        self.assertEqual([b["type"] for b in sent], ["text", "text", "document", "text"])
        self.assertIn("ABC123", sent[0]["text"])
        self.assertIn("ONE email", sent[-1]["text"])
        # The reading waits on the NAS for the next device that opens the app.
        listed = self.call("GET", "/api/analysis", cookie=cookie)[1]["results"]
        self.assertEqual([(r["documentId"], r["sha256"]) for r in listed], [(mail["documentId"], mail["hash"])])
        events = self.call("GET", "/api/inbound/settings", cookie=cookie)[1]["events"]
        self.assertEqual(events[0]["status"], "added")
        self.assertEqual(list((self.work / "data" / "inbound").iterdir()), [])  # spool emptied

    def test_retry_reading_includes_stored_attachments(self):
        key, cookie, csrf = self.enable()
        self.call("POST", "/api/inbound", raw=email(), key=key)
        self.app.inbound.process_pending()
        mail = next(d for d in self.documents() if d.get("source") == "email" and not d.get("sourceEmail"))
        self.fake.requests.clear()
        path, mime, name = self.app.store.document(mail["documentId"])
        self.app.analyze(path, mime, name, mail["documentId"])
        self.assertIn("document", [b["type"] for b in self.fake.requests[0]["messages"][0]["content"]])

    def test_reading_failure_is_shown_on_the_document(self):
        key, _, _ = self.enable()
        self.call("POST", "/api/inbound", raw=email(), key=key)
        with patch("app.call_claude", side_effect=AIError("Claude is overloaded.", "ai_error", 503)):
            self.app.inbound.process_pending()
        mail = next(d for d in self.documents() if d.get("source") == "email" and not d.get("sourceEmail"))
        self.assertEqual(mail["auto"]["state"], "Reader error")
        self.assertIn("overloaded", mail["auto"]["reason"])

    def test_storage_limit_keeps_the_email_and_notes_the_attachment(self):
        key, _, _ = self.enable()
        self.app.store.max_current = 10000 + inbound.STORAGE_MARGIN
        self.call("POST", "/api/inbound", raw=email(), key=key)
        self.app.inbound.process_pending()
        docs = self.documents()
        ticket = next(d for d in docs if d.get("sourceEmail"))
        self.assertNotIn("stored", ticket)
        self.assertIn("storage limit", ticket["notes"])
        self.assertIn("storage limit", next(d for d in docs if d.get("emailAttachments") is not None)["emailSkipped"])

    def test_email_without_attachments_and_without_ai(self):
        app = Application(Config(**{**self.config.__dict__, "ai": None}))
        self.app = app
        key, _, _ = self.enable()
        self.call("POST", "/api/inbound", raw=email(attachments=False, message_id=""), key=key)
        self.app.inbound.process_pending()
        (mail,) = [d for d in self.documents() if d.get("source") == "email"]
        self.assertEqual((mail["emailAttachments"], mail["status"]), (0, "Received by email · not read yet"))
        self.assertEqual(self.fake.requests, [])


if __name__ == "__main__":
    unittest.main()
