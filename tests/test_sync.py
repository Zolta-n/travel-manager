"""v4.1: sessions across restarts, per-device sign-in limits, lightweight sync, stored AI results."""
import base64
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
from ai_reader import AIConfig
import app as appmod
from app import Application, Config
from store import empty_state, StateError, Store

PASSWORD = "Correct-Horse-Battery-9"
PDF = b"%PDF-1.4 fake ticket"


class SyncCase(unittest.TestCase):
    def setUp(self):
        self.work = ROOT / (".qa-" + uuid.uuid4().hex)
        self.work.mkdir(mode=0o700)
        seed = empty_state()
        seed["trips"] = [{"tripId": "t1", "name": "US", "type": "Business", "destination": "Detroit",
                          "startDate": "2026-11-11", "endDate": "2026-11-21", "notes": "", "sample": False}]
        seed["documents"] = [{"documentId": "d1", "name": "ticket.pdf", "tripId": "t1", "size": len(PDF),
                              "data": "data:application/pdf;base64," + base64.b64encode(PDF).decode()}]
        (self.work / "seed.json").write_text(json.dumps(seed))
        self.app = self.make(PASSWORD)

    def tearDown(self):
        shutil.rmtree(self.work)

    def make(self, password, ai=None):
        return Application(Config(password=password, allowed_hosts=("travel.example",), data_dir=self.work / "data",
                                  web_dir=ROOT / "web", seed_path=self.work / "seed.json", secure_cookie=False, ai=ai))

    def call(self, method, path, body=None, cookie="", csrf="", remote="172.17.0.1", device=None, query=""):
        data = json.dumps(body).encode() if body is not None else b""
        env = {"REQUEST_METHOD": method, "PATH_INFO": path, "QUERY_STRING": query, "HTTP_HOST": "travel.example",
               "wsgi.input": io.BytesIO(data), "CONTENT_LENGTH": str(len(data)), "CONTENT_TYPE": "application/json",
               "HTTP_COOKIE": cookie, "HTTP_X_CSRF_TOKEN": csrf, "REMOTE_ADDR": remote}
        if device:
            env["HTTP_CF_CONNECTING_IP"] = device
        if method != "GET":
            env["HTTP_ORIGIN"] = "http://travel.example"
        captured = {}
        raw = b"".join(self.app(env, lambda status, headers: captured.update(status=int(status.split()[0]), headers=headers)))
        try:
            parsed = json.loads(raw or b"{}")
        except ValueError:
            parsed = raw
        return captured["status"], parsed, dict(captured["headers"])

    def login(self, device="203.0.113.5"):
        status, body, headers = self.call("POST", "/api/login", {"password": PASSWORD}, device=device)
        self.assertEqual(status, 200, body)
        return headers["Set-Cookie"].split(";")[0], body["csrfToken"]


class SessionTests(SyncCase):
    def test_sessions_survive_restart_but_not_password_change(self):
        cookie, _ = self.login()
        self.app = self.make(PASSWORD)  # container restart
        self.assertEqual(self.call("GET", "/api/revision", cookie=cookie)[0], 200)
        self.app = self.make(PASSWORD + "x")  # --rotate-password
        self.assertEqual(self.call("GET", "/api/revision", cookie=cookie)[0], 401)

    def test_session_lasts_30_days_and_slides(self):
        cookie, _ = self.login()
        self.assertIn("Max-Age=" + str(30 * 86400), self.call("POST", "/api/login", {"password": PASSWORD}, device="198.51.100.1")[2]["Set-Cookie"])
        later = time.time() + 2 * 86400
        with patch("app.time.time", return_value=later):
            status, _, headers = self.call("GET", "/api/revision", cookie=cookie)
        self.assertEqual(status, 200)
        self.assertIn("Max-Age=" + str(30 * 86400), headers.get("Set-Cookie", ""))
        with patch("app.time.time", return_value=later + 29 * 86400):
            self.assertEqual(self.call("GET", "/api/revision", cookie=cookie)[0], 200)  # day 31: past the original 30 days, valid because renewed on day 2

    def test_sign_in_limit_is_per_device_behind_tunnel(self):
        for _ in range(5):
            self.assertEqual(self.call("POST", "/api/login", {"password": "wrong-password-123"}, device="203.0.113.7")[0], 401)
        status, body, _ = self.call("POST", "/api/login", {"password": PASSWORD}, device="203.0.113.7")
        self.assertEqual(status, 429)
        self.assertIn("this device", body["error"])
        self.login(device="203.0.113.8")  # the iPhone is not locked out by the PC's attempts
        # A forged header from a non-local address is ignored.
        self.assertEqual(Application.client_id({"REMOTE_ADDR": "8.8.8.8", "HTTP_CF_CONNECTING_IP": "1.2.3.4"}), "8.8.8.8")
        self.assertEqual(Application.client_id({"REMOTE_ADDR": "127.0.0.1", "HTTP_CF_CONNECTING_IP": "junk"}), "127.0.0.1")


class LightSyncTests(SyncCase):
    def test_light_state_original_endpoint_and_kept_originals(self):
        cookie, csrf = self.login()
        status, data, _ = self.call("GET", "/api/state", cookie=cookie, query="originals=0")
        doc = data["state"]["documents"][0]
        self.assertTrue(doc["stored"])
        self.assertNotIn("data", doc)
        self.assertEqual(len(doc["hash"]), 64)
        status, binary, headers = self.call("GET", "/api/original", cookie=cookie, query="documentId=d1")
        self.assertEqual((status, binary, headers["Content-Type"]), (200, PDF, "application/octet-stream"))
        state = data["state"]
        state["trips"][0]["name"] = "US renamed"
        status, body, _ = self.call("PUT", "/api/state", {"state": state, "revision": data["revision"]}, cookie=cookie, csrf=csrf)
        self.assertEqual(status, 200, body)
        full = self.app.store.get()["state"]
        self.assertEqual(full["trips"][0]["name"], "US renamed")
        self.assertTrue(full["documents"][0]["data"].endswith(base64.b64encode(PDF).decode()))

    def test_unknown_hash_is_not_kept_and_missing_file_is_refused(self):
        state = self.app.store.get(originals=False)["state"]
        state["documents"][0]["hash"] = "0" * 64
        self.app.store.put(state, 0)
        self.assertEqual(self.app.store.get()["state"]["documents"][0]["data"], "")  # metadata only, nothing invented
        store = self.app.store
        good = store.get(originals=False)["state"]
        with store.connect() as db:
            digest = next(iter(store.known_originals(db)))
        store.original_path(digest).unlink()
        good["documents"][0].update(stored=True, hash=digest)
        with self.assertRaises(StateError):
            store.put(good, store.revision())

    def test_current_backup_and_ai_results(self):
        cookie, _ = self.login()
        status, raw, headers = self.call("GET", "/api/backup", cookie=cookie, query="scope=current")
        self.assertEqual(status, 200)
        self.assertIn("Travel_Manager_Backup_", headers["Content-Disposition"])
        self.assertEqual(raw["version"], 2)
        self.app.store.save_ai_result("d1", "a" * 64, {"segments": [], "version": "ai-1"})
        listed = self.call("GET", "/api/analysis", cookie=cookie)[1]["results"]
        self.assertEqual([r["documentId"] for r in listed], ["d1"])
        result = self.call("GET", "/api/analysis/result", cookie=cookie, query="documentId=d1")[1]
        self.assertEqual(result["result"]["version"], "ai-1")
        self.assertEqual(self.call("GET", "/api/analysis/result", cookie=cookie, query="documentId=zz")[0], 404)

    def test_finished_ai_job_is_kept_on_the_nas(self):
        self.app = self.make(PASSWORD, ai=AIConfig(api_key="sk-ant-test"))
        cookie, csrf = self.login()
        canned = {"documentType": "e_ticket", "summary": "", "documentDate": "", "segments": [], "costs": [],
                  "trip": {"matchedTripId": "", "reason": "", "newTripName": "", "newTripDestination": "", "newTripType": "Business"}, "warnings": []}
        with patch("app.call_claude", return_value=(canned, {"model": "m", "inputTokens": 1, "outputTokens": 1})):
            job = self.call("POST", "/api/analyze", {"documentId": "d1"}, cookie=cookie, csrf=csrf)[1]["jobId"]
            for _ in range(200):
                if self.call("GET", "/api/analyze/" + job, cookie=cookie)[1]["status"] in ("done", "error"):
                    break
                time.sleep(0.05)
        listed = self.call("GET", "/api/analysis", cookie=cookie)[1]["results"]
        self.assertEqual(listed[0]["documentId"], "d1")
        self.assertEqual(listed[0]["sha256"], self.app.store.get(originals=False)["state"]["documents"][0]["hash"])


if __name__ == "__main__":
    unittest.main()
