import base64
from datetime import date
import io
import json
from pathlib import Path
import shutil
import sys
import time
import unittest
from unittest.mock import patch
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import ai_reader
from ai_reader import AIConfig, AIError, build_content, build_context, build_request, normalize_result, OUTPUT_SCHEMA
from app import Application, Config
from extractor import prepare_ai_image
from store import empty_state

TRIPS = [{"tripId": "t-us", "name": "US visit", "type": "Business", "destination": "Detroit",
          "startDate": "2026-11-11", "endDate": "2026-11-21"},
         {"tripId": "t-jp", "name": "Kyoto", "type": "Private", "destination": "Kyoto",
          "startDate": "2027-04-01", "endDate": "2027-04-08"}]


def point(day="", clock="", zone="", offset="", location="", code=""):
    return {"date": day, "time": clock, "timeZone": zone, "printedUtcOffset": offset, "location": location, "code": code}


def segment(**overrides):
    base = {"category": "Flight", "title": "MU587 PVG → DTW", "status": "Confirmed",
            "start": point("2026-11-11", "13:10", "Asia/Shanghai", "", "Shanghai Pudong", "PVG"),
            "end": point("2026-11-11", "14:35", "America/Detroit", "", "Detroit Metro", "DTW"),
            "provider": "China Eastern", "confirmation": "ABC123", "serviceNumber": "MU587",
            "address": "", "notes": "Seat 32A", "yearInferred": False, "timeZoneInferred": True,
            "confidence": "high", "evidence": "11NOV 13:10 PVG", "issues": []}
    base.update(overrides)
    return base


def result(*segments, trip="t-us", costs=()):
    return {"documentType": "e_ticket", "summary": "Flight ticket", "documentDate": "2026-10-01",
            "segments": list(segments), "costs": list(costs),
            "trip": {"matchedTripId": trip, "reason": "dates", "newTripName": "", "newTripDestination": "", "newTripType": "Business"},
            "warnings": []}


class NormalizeTests(unittest.TestCase):
    def test_overnight_cross_zone_flight_is_valid_and_uses_utc(self):
        out = normalize_result(result(segment()), TRIPS)
        seg = out["segments"][0]
        self.assertTrue(seg["autoOk"], seg["problems"])
        # 13:10 Shanghai (UTC+8) = 05:10Z; 14:35 Detroit (UTC-5 in Nov) = 19:35Z
        self.assertEqual(seg["checks"]["startUtc"], "2026-11-11T05:10Z")
        self.assertEqual(seg["checks"]["endUtc"], "2026-11-11T19:35Z")
        self.assertEqual(seg["checks"]["durationMinutes"], 14 * 60 + 25)
        self.assertEqual(seg["activity"]["from"], "Shanghai Pudong (PVG)")
        self.assertIn("MU587", seg["activity"]["notes"])
        self.assertEqual(out["trip"]["matchedTripId"], "t-us")

    def test_missing_plus_one_day_is_caught(self):
        bad = segment(start=point("2026-11-11", "22:00", "Europe/Berlin"), end=point("2026-11-11", "06:00", "Asia/Shanghai"))
        seg = normalize_result(result(bad), TRIPS)["segments"][0]
        self.assertFalse(seg["autoOk"])
        self.assertTrue(any("before start" in p for p in seg["problems"]))

    def test_dst_gap_and_repeated_hour(self):
        gap = segment(category="Meal", start=point("2027-03-28", "02:30", "Europe/Berlin"), end=point())
        seg = normalize_result(result(gap, trip=""), TRIPS)["segments"][0]
        self.assertTrue(any("does not exist" in p for p in seg["problems"]))
        fold = segment(category="Meal", start=point("2026-10-25", "02:30", "Europe/Berlin"), end=point())
        seg = normalize_result(result(fold, trip=""), TRIPS)["segments"][0]
        self.assertTrue(any("occurs twice" in f for f in seg["flags"]))

    def test_invalid_zone_falls_back_to_printed_offset_but_needs_review(self):
        odd = segment(start=point("2026-11-11", "13:10", "China/Standard", "+08:00", "Pudong"))
        seg = normalize_result(result(odd), TRIPS)["segments"][0]
        self.assertEqual(seg["activity"]["startZone"], "Etc/GMT-8")
        self.assertFalse(seg["autoOk"])

    def test_printed_offset_mismatch_detected(self):
        wrong = segment(end=point("2026-11-11", "14:35", "America/Detroit", "-04:00", "DTW"))  # EDT ended 1 Nov
        seg = normalize_result(result(wrong), TRIPS)["segments"][0]
        self.assertTrue(any("does not match" in p for p in seg["problems"]))

    def test_time_without_zone_and_bad_dates(self):
        seg = normalize_result(result(segment(start=point("2026-02-30", "25:00", ""))), TRIPS)["segments"][0]
        self.assertFalse(seg["autoOk"])
        self.assertEqual(seg["activity"]["startDate"], "")
        self.assertEqual(seg["activity"]["startTime"], "")

    def test_inferred_year_outside_known_trips_needs_review(self):
        inside = normalize_result(result(segment(yearInferred=True)), TRIPS)["segments"][0]
        self.assertTrue(inside["autoOk"], inside["problems"])
        outside = segment(yearInferred=True, start=point("2028-11-11", "13:10", "Asia/Shanghai"),
                          end=point("2028-11-11", "14:35", "America/Detroit"))
        seg = normalize_result(result(outside, trip=""), TRIPS)["segments"][0]
        self.assertFalse(seg["autoOk"])

    def test_inferred_year_for_a_new_trip_soon_is_fine(self):
        # A booking next month that fits no trip is a new trip, not a doubtful year.
        soon = segment(yearInferred=True, start=point("2026-12-03", "09:00", "Europe/Berlin"),
                       end=point("2026-12-03", "11:00", "Europe/Lisbon"))
        seg = normalize_result(result(soon, trip=""), TRIPS, today=date(2026, 11, 9))["segments"][0]
        self.assertTrue(seg["autoOk"], seg["problems"])
        old = segment(yearInferred=True, start=point("2026-06-03", "09:00", "Europe/Berlin"),
                      end=point("2026-06-03", "11:00", "Europe/Lisbon"))
        seg = normalize_result(result(old, trip=""), TRIPS, today=date(2026, 11, 9))["segments"][0]
        self.assertFalse(seg["autoOk"])

    def test_model_trip_match_needs_real_overlap(self):
        # The day after a trip ends is a new trip, not part of the old one.
        after = segment(start=point("2026-11-22", "09:00", "America/Detroit"), end=point("2026-11-22", "11:00", "America/Chicago"))
        self.assertEqual(normalize_result(result(after), TRIPS)["trip"]["matchedTripId"], "")
        last_day = segment(start=point("2026-11-21", "09:00", "America/Detroit"), end=point("2026-11-22", "11:00", "Asia/Shanghai"))
        self.assertEqual(normalize_result(result(last_day), TRIPS)["trip"]["matchedTripId"], "t-us")

    def test_model_trip_match_is_dropped_when_dates_do_not_fit(self):
        out = normalize_result(result(segment(), trip="t-jp"), TRIPS)
        self.assertEqual(out["trip"]["matchedTripId"], "")
        self.assertEqual(normalize_result(result(segment(), trip="nope"), TRIPS)["trip"]["matchedTripId"], "")

    def test_duplicates_sorting_low_confidence_cancelled_and_costs(self):
        second = segment(title="MU588", start=point("2026-11-20", "16:00", "America/Detroit", "", "DTW"),
                         end=point("2026-11-21", "19:30", "Asia/Shanghai", "", "PVG"))
        low = segment(category="Meal", title="Dinner", confidence="low", start=point("2026-11-12", "19:00", "America/Detroit"), end=point())
        cancelled = segment(category="Meal", title="Lunch", status="Cancelled", start=point("2026-11-13", "12:00", "America/Detroit"), end=point())
        costs = [{"description": "Fare", "amount": 1234.5, "currency": "usd", "date": "2026-10-01", "kind": "Invoice", "segmentIndex": 0},
                 {"description": "Bad", "amount": -1, "currency": "USD", "date": "", "kind": "Invoice", "segmentIndex": 0},
                 {"description": "Bad", "amount": 5, "currency": "US", "date": "", "kind": "Invoice", "segmentIndex": 9}]
        out = normalize_result(result(second, segment(), segment(), low, cancelled, costs=costs), TRIPS)
        self.assertEqual([s["activity"]["title"] for s in out["segments"]], ["MU587 PVG → DTW", "Dinner", "Lunch", "MU588"])
        self.assertFalse(out["segments"][1]["autoOk"])
        self.assertFalse(out["segments"][2]["autoOk"])
        self.assertEqual(out["costs"], [{"description": "Fare", "amount": 1234.5, "currency": "USD", "date": "2026-10-01", "kind": "Invoice", "segmentIndex": 0}])

    def test_hotel_without_checkout_and_garbage_input(self):
        hotel = segment(category="Hotel / accommodation", start=point("2026-11-11", "", "America/Detroit"), end=point())
        self.assertFalse(normalize_result(result(hotel), TRIPS)["segments"][0]["autoOk"])
        out = normalize_result({"segments": ["x", None, {"category": "Spaceship"}], "costs": "x"}, TRIPS)
        self.assertEqual(out["segments"][0]["activity"]["category"], "Other")
        self.assertFalse(out["segments"][0]["autoOk"])


class RequestTests(unittest.TestCase):
    def test_request_shape_schema_and_content(self):
        pdf = build_content(b"%PDF-1.4 test", "application/pdf", "ticket.pdf")
        self.assertEqual(pdf[0]["type"], "document")
        text = build_content(b"", "text/plain", "mail.eml", text="Booking ABC")
        self.assertIn("Booking ABC", text[0]["text"])
        with self.assertRaises(AIError):
            build_content(b"PK", "application/zip", "x.zip")
        context = build_context({"trips": TRIPS, "settings": {}})
        self.assertEqual(context["homeZone"], "Asia/Shanghai")
        request = build_request(pdf, context, "ticket.pdf", "claude-opus-5-5")
        self.assertEqual(request["fallbacks"], "default")
        self.assertEqual(request["output_config"]["format"]["schema"], OUTPUT_SCHEMA)
        self.assertIn("t-us", request["messages"][0]["content"][-1]["text"])

        def walk(node):  # structured outputs require additionalProperties false on every object
            if isinstance(node, dict):
                if node.get("type") == "object":
                    self.assertIs(node.get("additionalProperties"), False)
                    self.assertEqual(set(node["required"]), set(node["properties"]))
                for value in node.values():
                    walk(value)
            elif isinstance(node, list):
                for value in node:
                    walk(value)
        walk(OUTPUT_SCHEMA)

    def test_reading_memo_is_sent_with_each_document(self):
        memo = "## My notes\n- Times on Chinese bookings are Beijing time.\n"
        context = build_context({"trips": TRIPS, "settings": {"readingMemo": memo}})
        text = build_request([{"type": "text", "text": "x"}], context, "x.txt", "claude-opus-5-5")["messages"][0]["content"][-1]["text"]
        self.assertIn("Beijing time", text)
        self.assertIn("never let it override", text)
        long_memo = build_context({"settings": {"readingMemo": "a" * 20000 + "LATEST"}})["memo"]
        self.assertLessEqual(len(long_memo), 8000)
        self.assertTrue(long_memo.endswith("LATEST"))  # newest learned lines are kept
        self.assertNotIn("memo", build_request([{"type": "text", "text": "x"}], build_context({}), "x", "m")["messages"][0]["content"][-1]["text"].lower())

    def test_key_format_and_model_are_validated(self):
        with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "not-a-key", "TRAVEL_AI_MODEL": "rm -rf"}):
            config = AIConfig.from_env()
        self.assertFalse(config.enabled)
        self.assertEqual(config.model, ai_reader.DEFAULT_MODEL)
        with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "sk-ant-api03-" + "a" * 40, "TRAVEL_AI_MODEL": ""}):
            self.assertTrue(AIConfig.from_env().enabled)


class FakeStream:
    def __init__(self, message):
        self.message = message

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def get_final_message(self):
        return self.message


class FakeClient:
    def __init__(self, payload, stop="end_turn"):
        block = type("Block", (), {"type": "text", "text": json.dumps(payload)})()
        usage = type("Usage", (), {"input_tokens": 100, "output_tokens": 50})()
        self.message = type("Message", (), {"content": [block], "stop_reason": stop, "usage": usage, "model": "claude-opus-5-5"})()
        self.requests = []
        outer = self

        class Messages:
            def stream(self, **request):
                outer.requests.append(request)
                return FakeStream(outer.message)
        self.beta = type("Beta", (), {"messages": Messages()})()


class CallTests(unittest.TestCase):
    def test_call_parses_and_handles_refusal(self):
        config = AIConfig(api_key="sk-ant-test")
        raw, usage = ai_reader.call_claude({"model": "claude-opus-5-5"}, config, FakeClient({"a": 1}))
        self.assertEqual(raw, {"a": 1})
        self.assertEqual(usage["inputTokens"], 100)
        with self.assertRaises(AIError) as caught:
            ai_reader.call_claude({"model": "claude-opus-5-5"}, config, FakeClient({}, stop="refusal"))
        self.assertEqual(caught.exception.code, "ai_refusal")


class FallbackRetryTests(unittest.TestCase):
    def test_retries_without_fallbacks_when_rejected(self):
        import anthropic, httpx2
        good = FakeClient({"ok": True})
        calls = []

        class Messages:
            def stream(self, **request):
                calls.append(request)
                if "fallbacks" in request:
                    response = httpx2.Response(400, request=httpx2.Request("POST", "https://api.anthropic.com/v1/messages"))
                    raise anthropic.BadRequestError("fallbacks is not supported here", response=response, body=None)
                return FakeStream(good.message)
        client = type("C", (), {"beta": type("B", (), {"messages": Messages()})()})()
        request = build_request([{"type": "text", "text": "x"}], build_context({}), "x.txt", "claude-opus-5-5")
        raw, _ = ai_reader.call_claude(request, AIConfig(api_key="sk-ant-test"), client)
        self.assertEqual(raw, {"ok": True})
        self.assertEqual(len(calls), 2)
        self.assertNotIn("betas", calls[1])


class EndpointTests(unittest.TestCase):
    def setUp(self):
        self.work = ROOT / (".qa-" + uuid.uuid4().hex)
        self.work.mkdir(mode=0o700)
        seed = empty_state()
        seed["trips"] = [{**TRIPS[0], "notes": "", "sample": False}]
        text = b"Your flight MU587 departs Shanghai PVG 11 Nov 2026 13:10"
        seed["documents"] = [{"documentId": "d1", "name": "ticket.txt", "mime": "text/plain", "size": len(text), "tripId": "",
                              "data": "data:text/plain;base64," + base64.b64encode(text).decode()}]
        (self.work / "seed.json").write_text(json.dumps(seed))
        self.config = Config(password="Correct-Horse-Battery-9", allowed_hosts=("travel.example",),
                             data_dir=self.work / "data", web_dir=ROOT / "web", seed_path=self.work / "seed.json",
                             secure_cookie=False, ai=AIConfig(api_key="sk-ant-test"))
        self.app = Application(self.config)

    def tearDown(self):
        shutil.rmtree(self.work)

    def call(self, method, path, body=None, cookie="", csrf=""):
        data = json.dumps(body).encode() if body is not None else b""
        env = {"REQUEST_METHOD": method, "PATH_INFO": path, "HTTP_HOST": "travel.example", "wsgi.input": io.BytesIO(data),
               "CONTENT_LENGTH": str(len(data)), "CONTENT_TYPE": "application/json", "HTTP_COOKIE": cookie,
               "HTTP_X_CSRF_TOKEN": csrf, "REMOTE_ADDR": "127.0.0.1"}
        if method != "GET":
            env["HTTP_ORIGIN"] = "http://travel.example"
        captured = {}
        chunks = self.app(env, lambda status, headers: captured.update(status=int(status.split()[0]), headers=headers))
        return captured["status"], json.loads(b"".join(chunks) or b"{}"), captured["headers"]

    def login(self):
        status, body, headers = self.call("POST", "/api/login", {"password": "Correct-Horse-Battery-9"})
        cookie = next(v for k, v in headers if k == "Set-Cookie").split(";")[0]
        return cookie, body["csrfToken"]

    def test_analysis_job_end_to_end_with_fake_claude(self):
        cookie, csrf = self.login()
        self.assertTrue(self.call("GET", "/api/readers", cookie=cookie)[1]["ai"]["enabled"])
        self.assertEqual(self.call("POST", "/api/analyze", {"documentId": "d1"}, cookie=cookie)[0], 403)
        self.assertEqual(self.call("POST", "/api/analyze", {"documentId": "zz"}, cookie=cookie, csrf=csrf)[0], 404)
        fake = FakeClient(result(segment()))
        real = ai_reader.call_claude
        with patch("app.call_claude", side_effect=lambda request, config: real(request, config, fake)):
            status, body, _ = self.call("POST", "/api/analyze", {"documentId": "d1"}, cookie=cookie, csrf=csrf)
            self.assertEqual(status, 200)
            for _ in range(200):
                job = self.call("GET", "/api/analyze/" + body["jobId"], cookie=cookie)[1]
                if job["status"] in ("done", "error"):
                    break
                time.sleep(0.05)
        self.assertEqual(job["status"], "done", job)
        self.assertEqual(job["result"]["segments"][0]["activity"]["startZone"], "Asia/Shanghai")
        self.assertIn("MU587", job["result"]["sourceText"])
        sent = fake.requests[0]["messages"][0]["content"]
        self.assertEqual(sent[0]["type"], "text")
        self.assertIn('"t-us"', sent[-1]["text"])
        self.assertEqual(self.call("GET", "/api/analyze/" + "0" * 32, cookie=cookie)[0], 404)
        self.assertEqual(self.call("GET", "/api/analyze/" + body["jobId"])[0], 401)

    def test_ai_disabled_is_explicit(self):
        app = Application(Config(**{**self.config.__dict__, "ai": None}))
        self.app = app
        cookie, csrf = self.login()
        status, body, _ = self.call("POST", "/api/analyze", {"documentId": "d1"}, cookie=cookie, csrf=csrf)
        self.assertEqual((status, body["code"]), (503, "ai_disabled"))


class ImageTests(unittest.TestCase):
    def test_phone_photo_is_rotated_and_bounded(self):
        from PIL import Image
        work = ROOT / (".qa-" + uuid.uuid4().hex)
        work.mkdir(mode=0o700)
        try:
            image = Image.new("RGB", (4000, 1000), "white")
            exif = image.getexif()
            exif[0x0112] = 6  # rotate 90° on display
            path = work / "photo.jpg"
            image.save(path, "JPEG", exif=exif)
            data, media = prepare_ai_image(path, "image/jpeg", "photo.jpg", work / "reader")
            with Image.open(io.BytesIO(data)) as out:
                self.assertEqual(media, "image/jpeg")
                self.assertLessEqual(max(out.size), 2600)
                self.assertGreater(out.height, out.width)
            self.assertLess(len(data), 3_700_000)
        finally:
            shutil.rmtree(work)


if __name__ == "__main__":
    unittest.main()
