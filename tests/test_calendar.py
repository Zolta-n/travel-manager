"""Work-calendar invites: which events, iCalendar content, outbox timing, updates and cancellations."""
from datetime import date
import email
from email import policy
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
from app import Application, Config
import calendar_invites
from calendar_invites import SETTLE_SECONDS, desired_events, ics
from inbound import Inbound
from store import empty_state

TODAY = date(2026, 11, 10)


def calendar_text(raw):
    message = email.message_from_string(raw, policy=policy.default)
    return next(p for p in message.walk() if p.get_content_type() == "text/calendar").get_content()


def trip(trip_id, kind="Business", start="2026-12-14", end="2026-12-18", **extra):
    return {"tripId": trip_id, "name": "Munich " + trip_id, "type": kind, "destination": "Munich, Germany", "startDate": start,
            "endDate": end, "notes": "private note", "sample": False, "privateDays": "", **extra}


def flight(activity_id, trip_id, day="2026-12-14", **extra):
    return {"activityId": activity_id, "tripId": trip_id, "category": "Flight", "title": "LH727 PVG → MUC", "startDate": day,
            "startTime": "00:10", "startZone": "Asia/Shanghai", "endDate": day, "endTime": "06:15", "endZone": "Europe/Berlin",
            "from": "Shanghai Pudong (PVG)", "to": "Munich (MUC)", "provider": "Lufthansa", "confirmation": "UQ4YP",
            "status": "Planned", "notes": "seat 32A", **extra}


def state(trips, activities=()):
    s = empty_state()
    s["trips"], s["activities"] = list(trips), list(activities)
    return s


class EventTests(unittest.TestCase):
    def test_which_trips_and_flights(self):
        events = desired_events(state([trip("b"), trip("p", "Private"), trip("p2", "Private", calendar="on"), trip("x", calendar="off"),
                                       trip("old", start="2026-10-01", end="2026-10-05")],
                                      [flight("f1", "b"), flight("f2", "p"), flight("f3", "b", startTime="")]), TODAY)
        self.assertEqual(sorted(events), ["flight-f1@travel-manager", "trip-b-0@travel-manager", "trip-p2-0@travel-manager"])
        trip_event, flight_event = events["trip-b-0@travel-manager"], events["flight-f1@travel-manager"]
        self.assertEqual((trip_event["start"], trip_event["end"], trip_event["allDay"]), ("2026-12-14", "2026-12-19", True))
        # 00:10 Shanghai (UTC+8) = 16:10Z the day before; 06:15 Munich (UTC+1) = 05:15Z.
        self.assertEqual((flight_event["start"], flight_event["end"]), ("20261213T161000Z", "20261214T051500Z"))
        self.assertIn("UQ4YP", flight_event["description"])
        self.assertNotIn("private note", trip_event["description"] + flight_event["description"])
        self.assertNotIn("seat 32A", flight_event["description"])

    def test_mixed_trip_has_business_days_only(self):
        mixed = trip("m", "Mixed", start="2026-12-14", end="2027-01-03", privateDays="|".join("2026-12-%d" % d for d in range(19, 32)))
        events = desired_events(state([mixed], [flight("out", "m"), flight("ski", "m", day="2026-12-20")]), TODAY)
        runs = sorted((e["start"], e["end"]) for e in events.values() if e["kind"] == "trip")
        self.assertEqual(runs, [("2026-12-14", "2026-12-19"), ("2027-01-01", "2027-01-04")])
        self.assertIn("(business days)", events["trip-m-0@travel-manager"]["summary"])
        self.assertNotIn("flight-ski@travel-manager", events)  # flight on a private day stays private

    def test_ics_is_outlook_friendly(self):
        event = desired_events(state([trip("b", name="Munich; fair, day 1")], [flight("f1", "b")]), TODAY)
        text = ics(event["trip-b-0@travel-manager"], "trip-b-0@travel-manager", 0, "REQUEST", "travel@example.org", "me@work.example")
        self.assertIn("METHOD:REQUEST\r\n", text)
        self.assertIn("DTSTART;VALUE=DATE:20261214\r\n", text)
        self.assertIn("SUMMARY:Munich\\; fair\\, day 1\r\n", text)
        self.assertIn("ORGANIZER;CN=Travel Manager:mailto:travel@example.org", text.replace("\r\n ", ""))
        self.assertTrue(all(len(line.encode()) <= 75 for line in text.split("\r\n")))
        flight_text = ics(event["flight-f1@travel-manager"], "flight-f1@travel-manager", 2, "CANCEL", "travel@example.org", "me@work.example")
        self.assertIn("SEQUENCE:2\r\n", flight_text)
        self.assertIn("STATUS:CANCELLED", flight_text)
        self.assertIn("DTSTART:20261213T161000Z", flight_text)


class OutboxTests(unittest.TestCase):
    def setUp(self):
        self.work = ROOT / (".qa-" + uuid.uuid4().hex)
        self.work.mkdir(mode=0o700)
        (self.work / "seed.json").write_text(json.dumps(state([trip("b")], [flight("f1", "b")])))
        self.no_thread = patch.object(Inbound, "_start", lambda self: None)
        self.no_thread.start()
        self.app = Application(Config(password="Correct-Horse-Battery-9", allowed_hosts=("travel.example",),
                                      data_dir=self.work / "data", web_dir=ROOT / "web", seed_path=self.work / "seed.json", secure_cookie=False))
        self.key = self.app.inbound.new_key()["key"]
        self.cal = self.app.calendar
        self.cal.save_settings({"enabled": True, "to": "Me@Work.example", "from": "travel@example.org"})
        self.today = patch.object(calendar_invites, "date", type("D", (date,), {"today": staticmethod(lambda: TODAY)}))
        self.today.start()

    def tearDown(self):
        self.today.stop()
        self.no_thread.stop()
        shutil.rmtree(self.work)

    def call(self, method, path, body=None, key=None):
        data = json.dumps(body).encode() if body is not None else b""
        env = {"REQUEST_METHOD": method, "PATH_INFO": path, "HTTP_HOST": "travel.example", "wsgi.input": io.BytesIO(data),
               "CONTENT_LENGTH": str(len(data)), "CONTENT_TYPE": "application/json", "REMOTE_ADDR": "127.0.0.1", "HTTP_CF_CONNECTING_IP": "198.51.100.30"}
        if key is not None:
            env["HTTP_X_INBOUND_SECRET"] = key
        captured = {}
        chunks = self.app(env, lambda status, headers: captured.update(status=int(status.split()[0])))
        return captured["status"], json.loads(b"".join(chunks) or b"{}")

    def change(self, mutate):
        current = self.app.store.get(originals=False)
        mutate(current["state"])
        self.app.store.put(current["state"], current["revision"])

    def poll(self, now):
        return self.cal.outbox(now=now, today=TODAY)

    def send_all(self, now):
        due = self.poll(now)
        self.cal.acknowledge([{"id": m["id"], "ok": True} for m in due], now=now)
        return due

    def test_worker_needs_the_key(self):
        self.assertEqual(self.call("GET", "/api/outbox")[0], 401)
        self.assertEqual(self.call("GET", "/api/outbox", key="tmin_wrong")[0], 401)
        status, body = self.call("GET", "/api/outbox", key=self.key)
        self.assertEqual((status, body["messages"]), (200, []))  # new events wait first
        self.assertEqual(self.call("POST", "/api/outbox", {"results": []}, key=self.key)[0], 200)  # no Origin needed

    def test_first_invites_wait_then_send_then_update_and_cancel(self):
        self.assertEqual(self.poll(1000), [])
        self.assertEqual(self.poll(1000 + SETTLE_SECONDS - 1), [])
        due = self.send_all(1000 + SETTLE_SECONDS)
        self.assertEqual(len(due), 2)
        message = email.message_from_string(due[0]["raw"], policy=policy.default)
        self.assertEqual((due[0]["to"], due[0]["from"]), ("me@work.example", "travel@example.org"))
        calendar_part = next(p for p in message.walk() if p.get_content_type() == "text/calendar")
        self.assertEqual(calendar_part.get_param("method"), "REQUEST")
        self.assertIn("SEQUENCE:0", calendar_part.get_content())
        self.assertEqual(self.poll(2000), [])  # nothing changed, nothing sent
        # The flight moves: one update with a higher SEQUENCE, after it settles.
        self.change(lambda s: s["activities"][0].update(startTime="01:10"))
        self.assertEqual(self.poll(3000), [])
        updates = self.send_all(3000 + SETTLE_SECONDS)
        # The flight and the trip event (it lists the flight times) are both updated, with SEQUENCE 1.
        self.assertEqual(sorted(m["id"].split("|")[:2][0] for m in updates), ["flight-f1@travel-manager", "trip-b-0@travel-manager"])
        self.assertTrue(all("SEQUENCE:1" in calendar_text(m["raw"]) and "Updated: " in m["raw"] for m in updates))
        # The flight is deleted: the invite is cancelled.
        self.change(lambda s: s["activities"].clear())
        self.poll(5000)
        cancel = next(m for m in self.send_all(5000 + SETTLE_SECONDS) if m["id"].startswith("flight-"))
        self.assertIn("METHOD:CANCEL", calendar_text(cancel["raw"]))
        self.assertIn("|2|CANCEL", cancel["id"])
        self.assertEqual(self.poll(9000), [])
        recent = self.cal.settings()["recent"]
        self.assertIn("cancelled", [r["status"] for r in recent])

    def test_failed_send_is_retried_after_settling(self):
        self.poll(1000)
        due = self.poll(1000 + SETTLE_SECONDS)
        self.cal.acknowledge([{"id": m["id"], "ok": False, "error": "destination not verified"} for m in due], now=1400)
        self.assertEqual(self.poll(1401), [])
        self.assertIn("destination not verified", [r["error"] for r in self.cal.settings()["recent"]])
        self.assertEqual(len(self.poll(1400 + SETTLE_SECONDS)), 2)

    def test_switching_off_cancels_and_nothing_when_disabled(self):
        self.poll(0)
        self.send_all(SETTLE_SECONDS)
        self.change(lambda s: s["trips"][0].update(calendar="off"))
        self.poll(1000)
        due = self.poll(1000 + SETTLE_SECONDS)
        self.assertEqual(sorted("METHOD:CANCEL" in calendar_text(m["raw"]) for m in due), [True, True])
        self.cal.save_settings({"enabled": False, "to": "me@work.example", "from": "travel@example.org"})
        self.change(lambda s: s["trips"][0].update(calendar="on"))
        self.assertEqual(self.poll(5000), [])

    def test_settings_validation(self):
        with self.assertRaises(calendar_invites.CalendarError):
            self.cal.save_settings({"enabled": True, "to": "", "from": "travel@example.org"})
        with self.assertRaises(calendar_invites.CalendarError):
            self.cal.save_settings({"enabled": True, "to": "not-an-address", "from": "travel@example.org"})


class ReplyTests(unittest.TestCase):
    def test_outlook_replies_are_ignored(self):
        from inbound import CALENDAR_REPLY
        reply = b'Subject: Accepted: Munich\r\nContent-Type: text/calendar; charset="utf-8";\r\n\tmethod=REPLY\r\n\r\nBEGIN:VCALENDAR'
        self.assertTrue(CALENDAR_REPLY.search(reply))
        self.assertFalse(CALENDAR_REPLY.search(b'Content-Type: text/calendar; method=REQUEST\r\n'))


if __name__ == "__main__":
    unittest.main()
