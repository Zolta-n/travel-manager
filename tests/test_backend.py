import base64
from concurrent.futures import ThreadPoolExecutor
import hashlib
import http.client
import importlib.util
import io
import json
import os
from pathlib import Path
import secrets
import shutil
import sys
import threading
import unittest
from unittest.mock import patch
import uuid
from wsgiref.simple_server import make_server, WSGIRequestHandler

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app import APIError, Application, Config, MAX_REQUEST_BYTES, create_app
from extractor import extract_document, ReaderError
from store import Conflict, empty_state, MAX_FILE_BYTES, prepare_state, StateError, Store
import reader_worker


class WorkspaceCase(unittest.TestCase):
    def setUp(self):
        self.work = ROOT / (".qa-" + uuid.uuid4().hex)
        self.work.mkdir(mode=0o700)

    def tearDown(self):
        shutil.rmtree(self.work)


def document(binary=b"Hotel confirmation ZX9042", identifier="doc1", mime="text/plain", name="confirmation.txt"):
    return {"documentId": identifier, "name": name, "mime": mime, "size": len(binary),
            "data": "data:" + mime + ";base64," + base64.b64encode(binary).decode("ascii"), "tripId": ""}


def state_with(*documents):
    state = empty_state()
    state["documents"] = list(documents)
    return state


def make_pdf(text="TRAVEL HOTEL CONFIRMATION ZX9042", pages=1):
    objects = [b"<< /Type /Catalog /Pages 2 0 R >>", b""]
    page_ids = []
    for number in range(pages):
        page_id, stream_id = len(objects) + 1, len(objects) + 2
        page_ids.append(page_id)
        font_id = 3 + pages * 2
        objects.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 {font_id} 0 R >> >> /Contents {stream_id} 0 R >>".encode())
        stream = f"BT /F1 18 Tf 40 700 Td ({text}) Tj ET".encode("ascii")
        objects.append(b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream")
    objects[1] = ("<< /Type /Pages /Count " + str(pages) + " /Kids [" + " ".join(str(i) + " 0 R" for i in page_ids) + "] >>").encode()
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    result = b"%PDF-1.4\n"
    offsets = [0]
    for index, body in enumerate(objects, 1):
        offsets.append(len(result))
        result += str(index).encode() + b" 0 obj\n" + body + b"\nendobj\n"
    xref = len(result)
    result += b"xref\n0 " + str(len(objects) + 1).encode() + b"\n0000000000 65535 f \n"
    for offset in offsets[1:]:
        result += f"{offset:010} 00000 n \n".encode()
    return result + f"trailer\n<< /Size {len(objects)+1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()


class StoreTests(WorkspaceCase):
    def test_roundtrip_private_files_and_metadata(self):
        store = Store(self.work / "data")
        doc = document(identifier="../../never-a-path", name="../../outside.txt")
        state = state_with(doc)
        state["documents"][0]["auto"] = {"state": "Reviewed", "items": [{"title": "Hotel"}], "createdIds": ["a1"], "sourceText": "original"}
        self.assertEqual(store.put(state, 0), 1)
        result = store.get()
        self.assertEqual(result["state"]["documents"][0]["data"], doc["data"])
        self.assertEqual(result["state"]["documents"][0]["auto"]["items"][0]["title"], "Hotel")
        with store.connect() as db:
            metadata = db.execute("SELECT state_json FROM snapshots WHERE revision=1").fetchone()[0]
        self.assertNotIn("base64,", metadata)
        self.assertFalse((self.work / "outside.txt").exists())
        path, _, _ = store.document("../../never-a-path")
        self.assertTrue(path.is_relative_to(store.originals))
        self.assertEqual(path.read_bytes(), b"Hotel confirmation ZX9042")

    def test_seed_once_and_original_retention(self):
        seed = self.work / "seed.json"
        seed.write_text(json.dumps(state_with(document())), encoding="utf-8")
        store = Store(self.work / "data", seed)
        path, _, _ = store.document("doc1")
        store.put(empty_state(), 0)
        seed.write_text("not JSON", encoding="utf-8")
        reopened = Store(self.work / "data", seed)
        self.assertEqual(reopened.get()["state"]["documents"], [])
        self.assertTrue(path.exists())
        backup = json.loads("".join(reopened.backup_chunks()))
        self.assertEqual(len(backup["history"]), 2)
        # v2: history refers to files by hash; files only older revisions use are listed once.
        old = backup["history"][0]["state"]["documents"][0]
        self.assertNotIn("data", old)
        self.assertEqual(backup["originals"][old["hash"]], document()["data"])
        self.assertEqual(backup["revision"], 1)

    def test_revision_conflict_and_concurrent_writers(self):
        store = Store(self.work / "data")
        def put():
            try:
                return store.put(empty_state(), 0)
            except Conflict:
                return "conflict"
        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertCountEqual(list(pool.map(lambda _: put(), range(2))), [1, "conflict"])
        self.assertEqual(store.revision(), 1)

    def test_failed_transaction_cannot_publish_revision(self):
        store = Store(self.work / "data")
        with patch.object(store, "_write_original", side_effect=OSError("simulated disk failure")):
            with self.assertRaises(OSError):
                store.put(state_with(document()), 0)
        self.assertEqual(store.revision(), 0)
        self.assertEqual(store.get()["state"]["documents"], [])

    def test_insert_failure_after_atomic_file_leaves_only_safe_orphan(self):
        store = Store(self.work / "data")
        original_insert = store._insert
        def fail_after_insert(*args):
            original_insert(*args)
            raise OSError("simulated commit interruption")
        with patch.object(store, "_insert", side_effect=fail_after_insert):
            with self.assertRaises(OSError):
                store.put(state_with(document()), 0)
        self.assertEqual(store.revision(), 0)
        self.assertEqual(len(list(store.originals.glob("*/*"))), 1)
        self.assertEqual(store.put(state_with(document()), 0), 1)

    def test_duplicate_bytes_are_content_addressed(self):
        store = Store(self.work / "data")
        store.put(state_with(document(identifier="a"), document(identifier="b")), 0)
        self.assertEqual(len(list(store.originals.glob("*/*"))), 1)

    def test_corrupted_original_is_not_returned(self):
        store = Store(self.work / "data")
        store.put(state_with(document()), 0)
        path, _, _ = store.document("doc1")
        path.write_bytes(b"corrupted")
        with self.assertRaises(OSError):
            store.get()

    def test_validation_file_budget_total_budget_and_urls(self):
        with self.assertRaises(StateError):
            prepare_state(state_with(document(b"a" * (MAX_FILE_BYTES + 1))))
        with self.assertRaises(StateError):
            prepare_state(state_with(document(b"123", identifier="a"), document(b"456", identifier="b")), max_file=10, max_current=5)
        invalid = document()
        invalid["data"] = "http://127.0.0.1/private"
        with self.assertRaises(StateError):
            prepare_state(state_with(invalid))
        with self.assertRaises(StateError):
            prepare_state(state_with(document(identifier="a"), document(identifier="a")))
        with self.assertRaises(StateError):
            prepare_state({**empty_state(), "version": True})

    def test_internal_digest_rejects_path_traversal(self):
        store = Store(self.work / "data")
        with self.assertRaises(StateError):
            store.original_path("../../private/seed.json")

    def test_auto_metadata_limits(self):
        doc = document()
        doc["auto"] = {"state": "Done", "sourceText": "x" * 120001}
        with self.assertRaises(StateError):
            prepare_state(state_with(doc))


class QuietHandler(WSGIRequestHandler):
    def log_message(self, *args):
        pass


class HTTPTests(WorkspaceCase):
    def setUp(self):
        super().setUp()
        self.password = "Qa!" + secrets.token_urlsafe(24)
        web = self.work / "web"
        web.mkdir()
        (web / "index.html").write_text("<!doctype html><title>Public empty app shell</title>", encoding="utf-8")
        self.server = make_server("127.0.0.1", 0, lambda e, s: [], handler_class=QuietHandler)
        self.host = "127.0.0.1:" + str(self.server.server_port)
        config = Config(password=self.password, allowed_hosts=(self.host,), data_dir=self.work / "data", web_dir=web, secure_cookie=False)
        self.app = Application(config)
        self.server.set_app(self.app)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.cookie = None
        self.csrf = None

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        super().tearDown()

    def request(self, method, path, body=None, *, headers=None, cookie=True):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=75)
        use = {"Host": self.host}
        if method in ("POST", "PUT", "DELETE"):
            use["Origin"] = "http://" + self.host
        if body is not None:
            body = json.dumps(body).encode()
            use["Content-Type"] = "application/json"
        if cookie and self.cookie:
            use["Cookie"] = self.cookie
        if self.csrf:
            use["X-CSRF-Token"] = self.csrf
        use.update(headers or {})
        connection.request(method, path, body=body, headers=use)
        response = connection.getresponse()
        status, result_headers, data = response.status, dict(response.getheaders()), response.read()
        connection.close()
        try:
            result = json.loads(data)
        except ValueError:
            result = data
        return status, result_headers, result

    def login(self):
        status, headers, body = self.request("POST", "/api/login", {"password": self.password})
        self.assertEqual(status, 200, body)
        self.cookie = headers["Set-Cookie"].split(";", 1)[0]
        self.csrf = body["csrfToken"]
        return headers

    def test_auth_cookie_csrf_logout_and_hash_storage(self):
        self.assertEqual(self.request("GET", "/api/session")[2], {"authenticated": False})
        for endpoint in ("/api/state", "/api/backup", "/api/readers", "/api/revision"):
            self.assertEqual(self.request("GET", endpoint)[0], 401)
        headers = self.login()
        for attribute in ("HttpOnly", "SameSite=Strict", "Path=/"):
            self.assertIn(attribute, headers["Set-Cookie"])
        self.assertNotIn("; Secure", headers["Set-Cookie"])
        session = self.request("GET", "/api/session")[2]
        self.assertEqual(session["csrfToken"], self.csrf)
        token = self.cookie.split("=", 1)[1]
        with self.app.store.connect() as db:
            row = db.execute("SELECT * FROM sessions").fetchone()
            self.assertNotEqual(row["token_hash"], token)
            self.assertEqual(row["token_hash"], hashlib.sha256(token.encode()).hexdigest())
        self.assertEqual(self.request("POST", "/api/logout", {})[0], 200)
        self.assertEqual(self.request("GET", "/api/state")[0], 401)

    def test_bad_credentials_rate_limit(self):
        for _ in range(5):
            self.assertEqual(self.request("POST", "/api/login", {"password": "wrong"})[0], 401)
        self.assertEqual(self.request("POST", "/api/login", {"password": self.password})[0], 429)

    def test_host_origin_forwarded_headers_and_csrf(self):
        self.assertEqual(self.request("GET", "/api/session", headers={"Host": "attacker.example"})[0], 400)
        self.assertEqual(self.request("POST", "/api/login", {"password": self.password}, headers={"Origin": "https://attacker.example"})[0], 403)
        self.assertEqual(self.request("POST", "/api/login", {"password": self.password}, headers={"Origin": ""})[0], 403)
        self.assertEqual(self.request("GET", "/api/session", headers={"Host": "attacker.example", "X-Forwarded-Host": self.host})[0], 400)
        self.login()
        payload = {"state": empty_state(), "revision": 0}
        for endpoint, method, data in (("/api/state", "PUT", payload), ("/api/read", "POST", {"documentId": "d"}), ("/api/logout", "POST", {})):
            self.assertEqual(self.request(method, endpoint, data, headers={"X-CSRF-Token": ""})[0], 403)
        self.assertEqual(self.request("GET", "/api/state", headers={"Sec-Fetch-Site": "cross-site"})[0], 403)

    def test_state_conflict_retention_revision_and_consistent_backup(self):
        self.login()
        doc = document()
        status, _, body = self.request("PUT", "/api/state", {"state": state_with(doc), "revision": 0})
        self.assertEqual((status, body["revision"]), (200, 1))
        status, _, body = self.request("PUT", "/api/state", {"state": empty_state(), "revision": 0})
        self.assertEqual((status, body["revision"]), (409, 1))
        self.assertEqual(self.request("GET", "/api/state")[2]["state"]["documents"][0]["data"], doc["data"])
        self.assertEqual(self.request("GET", "/api/revision")[2], {"revision": 1})
        self.assertEqual(self.request("PUT", "/api/state", {"state": empty_state(), "revision": 1})[0], 200)
        status, headers, backup = self.request("GET", "/api/backup")
        self.assertEqual(status, 200)
        self.assertIn("attachment", headers["Content-Disposition"])
        self.assertEqual(backup["state"]["documents"], [])
        self.assertEqual(backup["originals"][backup["history"][1]["state"]["documents"][0]["hash"]], doc["data"])

    def test_public_route_never_serves_private_files(self):
        status, headers, body = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertNotIn(b"base64,", body)
        for endpoint in ("/private/seed.json", "/data/travel.sqlite3", "/../private/seed.json", "/%2e%2e/private/seed.json", "/app.py", "/requirements.txt"):
            self.assertEqual(self.request("GET", endpoint)[0], 404)
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        self.assertIn("no-store", headers["Cache-Control"])
        self.assertNotIn("Access-Control-Allow-Origin", headers)
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])

    def test_request_limits_and_json_shape(self):
        self.login()
        self.assertEqual(self.request("POST", "/api/read", {"documentId": "x" * 5000})[0], 413)
        self.assertEqual(self.request("POST", "/api/login", {"password": "x" * 9000})[0], 413)
        self.assertEqual(self.request("PUT", "/api/state", {"state": empty_state(), "revision": True})[0], 400)
        env = {"CONTENT_TYPE": "application/json", "CONTENT_LENGTH": str(MAX_REQUEST_BYTES + 1), "wsgi.input": io.BytesIO(b"")}
        with self.assertRaises(APIError) as failure:
            self.app.read_json(env)
        self.assertEqual(failure.exception.status, 413)
        with self.assertRaises(APIError):
            self.app.read_json({"CONTENT_TYPE": "application/json", "CONTENT_LENGTH": "13", "wsgi.input": io.BytesIO(b'{"a":1,"a":2}')})

    def test_readers_text_reader_and_no_revision_side_effect(self):
        self.login()
        self.assertTrue(self.request("GET", "/api/readers")[2]["offline"])
        self.request("PUT", "/api/state", {"state": state_with(document()), "revision": 0})
        status, _, result = self.request("POST", "/api/read", {"documentId": "doc1"})
        self.assertEqual(status, 200, result)
        self.assertIn("ZX9042", result["text"])
        self.assertEqual(self.request("GET", "/api/revision")[2], {"revision": 1})
        self.assertEqual(self.request("POST", "/api/read", {"documentId": "../../private/seed.json"})[0], 404)

    def test_secure_default_and_config_requirements(self):
        config = Config(self.password, (self.host,), self.work / "secure-data", self.work / "web")
        app = Application(config)
        self.assertIn("; Secure", app.cookie("test", 10)[1])
        self.assertTrue(app.cookie_name.startswith("__Host-"))
        for password, hosts in (("", (self.host,)), ("short", (self.host,)), (self.password, ())):
            with self.assertRaises(ValueError):
                Config(password, hosts, self.work / "bad", self.work / "web").validate()
        with self.assertRaises(ValueError):
            Config(self.password, (self.host,), self.work / "web" / "private", self.work / "web").validate()

    def test_relogin_revokes_old_token(self):
        self.login()
        old = self.cookie
        self.login()
        self.assertNotEqual(old, self.cookie)
        self.assertEqual(self.request("GET", "/api/state", headers={"Cookie": old})[0], 401)


class ExtractionTests(WorkspaceCase):
    def extract(self, binary, mime, name):
        original = self.work / "original"
        original.write_bytes(binary)
        return extract_document(original, mime, name, self.work / "reader-work")

    def test_stdlib_email_and_plain_text(self):
        result = self.extract(b"Subject: Hotel ZX9042\nMIME-Version: 1.0\nContent-Type: text/plain; charset=utf-8\n\nArrival 12 November 2026", "message/rfc822", "test.eml")
        self.assertIn("Hotel ZX9042", result["text"])
        self.assertIn("Arrival 12 November", result["text"])
        result = self.extract(b"Local confirmation ZX9042", "text/plain", "test.txt")
        self.assertEqual(result["text"], "Local confirmation ZX9042")

    def test_html_email_never_fetches_external_resources(self):
        result = self.extract(b'Subject: Hotel\nContent-Type: text/html; charset=utf-8\n\n<p>ZX9042</p><img src="https://invalid.example/private"><script>do_not_execute()</script>', "message/rfc822", "test.eml")
        self.assertIn("ZX9042", result["text"])
        self.assertNotIn("do_not_execute", result["text"])
        self.assertNotIn("invalid.example", result["text"])

    def test_text_output_is_bounded(self):
        result = self.extract(b"a" * 110000, "text/plain", "long.txt")
        self.assertEqual(len(result["text"]), 100000)
        self.assertTrue(result.get("truncated"))

    @unittest.skipUnless(shutil.which("pdfinfo") and shutil.which("pdftotext"), "Poppler tools not installed")
    def test_actual_pdf_text_and_page_limit(self):
        result = self.extract(make_pdf(), "application/pdf", "test.pdf")
        self.assertIn("ZX9042", result["text"])
        self.assertIn("pdftotext", result["method"])
        with self.assertRaises(ReaderError) as failure:
            self.extract(make_pdf(pages=21), "application/pdf", "long.pdf")
        self.assertEqual(failure.exception.code, "page_limit")

    def test_password_pdf_stops_before_any_text_or_ocr(self):
        original = self.work / "original.pdf"
        original.write_bytes(make_pdf())
        with patch.object(reader_worker, "run", return_value=(1, b"", b"Command Line Error: Incorrect password\n")) as runner:
            with self.assertRaises(reader_worker.Failure) as failure:
                reader_worker.extract(original, "application/pdf", "x.pdf", self.work)
        self.assertEqual(failure.exception.code, "encrypted_pdf")
        self.assertEqual(runner.call_count, 1)

    @unittest.skipUnless(shutil.which("pdfinfo") and shutil.which("pdftotext"), "Poppler tools not installed")
    def test_copy_restricted_pdf_is_read(self):
        # Opens without a password; only copying/editing is restricted (common for hotel invoices).
        result = self.extract((ROOT / "tests" / "fixtures" / "copy-restricted.pdf").read_bytes(), "application/pdf", "invoice.pdf")
        self.assertIn("INVOICE Hotel Example", result["text"])
        with self.assertRaises(ReaderError) as failure:
            self.extract((ROOT / "tests" / "fixtures" / "password.pdf").read_bytes(), "application/pdf", "locked.pdf")
        self.assertEqual(failure.exception.code, "encrypted_pdf")

    @unittest.skipUnless(shutil.which("tesseract") and importlib.util.find_spec("PIL"), "Tesseract or Pillow not installed")
    def test_actual_image_ocr_and_pixel_limit(self):
        from PIL import Image, ImageDraw, ImageFont
        image = Image.new("RGB", (1600, 500), "white")
        draw = ImageDraw.Draw(image)
        font = ImageFont.truetype("DejaVuSans.ttf", 48)
        draw.multiline_text((70, 60), "HOTEL CONFIRMATION ZX9042\nArrival 12 November 2026\nTOTAL USD 150.00", fill="black", font=font, spacing=20)
        output = io.BytesIO()
        image.save(output, format="PNG")
        result = self.extract(output.getvalue(), "image/png", "test.png")
        self.assertIn("HOTEL", result["text"])
        self.assertIn("ZX9042", result["text"])
        self.assertIn("OCR", result["method"])
        self.assertGreater(result["confidence"], 50)
        large = Image.new("1", (5000, 5000), "white")
        output = io.BytesIO()
        large.save(output, format="PNG")
        with self.assertRaises(ReaderError):
            self.extract(output.getvalue(), "image/png", "large.png")

    @unittest.skipUnless(all(shutil.which(x) for x in ("tesseract", "pdftoppm", "pdfinfo", "pdftotext")) and importlib.util.find_spec("PIL"), "OCR/Poppler/Pillow not installed")
    def test_actual_scanned_pdf_ocr(self):
        from PIL import Image, ImageDraw, ImageFont
        image = Image.new("RGB", (1600, 500), "white")
        draw = ImageDraw.Draw(image)
        draw.text((60, 80), "HOTEL CONFIRMATION ZX9042", fill="black", font=ImageFont.truetype("DejaVuSans.ttf", 54))
        output = io.BytesIO()
        image.save(output, format="PDF", resolution=150)
        result = self.extract(output.getvalue(), "application/pdf", "scan.pdf")
        self.assertIn("ZX9042", result["text"])
        self.assertIn("PDF OCR", result["method"])

    def test_unknown_format_and_no_url_fetch(self):
        with self.assertRaises(ReaderError) as failure:
            self.extract(b"opaque", "application/octet-stream", "test.bin")
        self.assertEqual(failure.exception.code, "unsupported_type")


class AdditionalSafetyTests(WorkspaceCase):
    def test_backup_keeps_consistent_revision_boundary_during_save(self):
        store = Store(self.work / "data")
        chunks = store.backup_chunks()
        first = next(chunks)
        store.put(state_with(document()), 0)
        result = json.loads(first + "".join(chunks))
        self.assertEqual(result["revision"], 0)
        self.assertEqual(len(result["history"]), 1)
        self.assertEqual(result["state"]["documents"], [])
        self.assertEqual(store.revision(), 1)

    def test_permission_helper_refuses_existing_database(self):
        from initialize_data import initialize
        (self.work / "travel.sqlite3").write_text("existing")
        with patch("initialize_data.os.chown") as change:
            with self.assertRaises(SystemExit):
                initialize(self.work)
            change.assert_not_called()

    def test_permission_helper_changes_only_fresh_directory_and_seed(self):
        from initialize_data import initialize
        seed = self.work / "seed.json"
        seed.write_text("{}")
        with patch("initialize_data.os.chown") as change, patch("initialize_data.os.chmod") as modes:
            initialize(self.work)
        self.assertEqual(change.call_count, 2)
        self.assertEqual({call.args[0] for call in change.call_args_list}, {seed, self.work})
        self.assertTrue(all(call.args[1:3] == (10001, 10001) for call in change.call_args_list))
        self.assertEqual(modes.call_count, 2)

    def test_permission_helper_refuses_symlink_seed(self):
        from initialize_data import initialize
        (self.work / "seed.json").write_text("{}")
        # The workspace filesystem forbids symlink creation; exercise the guard directly.
        with patch("initialize_data.Path.is_symlink", autospec=True, side_effect=lambda path: path.name == "seed.json"), patch("initialize_data.os.chown") as change:
            with self.assertRaises(SystemExit):
                initialize(self.work)
            change.assert_not_called()

    @unittest.skipUnless(importlib.util.find_spec("pypdf") and shutil.which("pdfinfo"), "pypdf/Poppler not installed")
    def test_actual_encrypted_pdf_is_rejected(self):
        from pypdf import PdfWriter
        writer = PdfWriter()
        writer.add_blank_page(width=612, height=792)
        writer.encrypt(secrets.token_urlsafe(24))
        output = io.BytesIO()
        writer.write(output)
        original = self.work / "encrypted.pdf"
        original.write_bytes(output.getvalue())
        with self.assertRaises(ReaderError) as failure:
            extract_document(original, "application/pdf", "encrypted.pdf", self.work / "reader-work")
        self.assertEqual(failure.exception.code, "encrypted_pdf")
        self.assertEqual(list((self.work / "reader-work").iterdir()), [])


@unittest.skipUnless(importlib.util.find_spec("flask"), "Flask absent; identical WSGI core is exercised over real loopback HTTP instead")
class FlaskClientTests(WorkspaceCase):
    def test_flask_client_authentication(self):
        config = Config("Qa!" + secrets.token_urlsafe(24), ("localhost",), self.work / "data", ROOT / "web", secure_cookie=False)
        flask_app = create_app(config)
        client = flask_app.test_client()
        response = client.post("/api/login", json={"password": config.password}, headers={"Origin": "http://localhost"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(client.get("/api/state").status_code, 200)


if __name__ == "__main__":
    unittest.main(verbosity=2)


class StorageAndBackupTests(WorkspaceCase):
    def test_limit_is_250_mb(self):
        from store import MAX_CURRENT_BYTES, prepare_state
        self.assertEqual(MAX_CURRENT_BYTES, 250 * 1024 * 1024)
        with self.assertRaises(StateError) as failure:
            prepare_state(state_with(document(b"x" * 3000)), max_current=2000)
        self.assertIn("MB limit", str(failure.exception))

    def test_history_backup_holds_each_file_once(self):
        store = Store(self.work / "data")
        kept, dropped = document(b"kept ticket", "keep"), document(b"old voucher", "old")
        store.put(state_with(kept, dropped), 0)
        current = store.get(originals=False)["state"]
        for revision in range(1, 5):  # several saves that keep the same files
            current["trips"] = [{"tripId": "t%d" % revision, "name": "Trip <%d>" % revision}]
            store.put(current, revision)
        current["documents"] = [d for d in current["documents"] if d["documentId"] == "keep"]
        store.put(current, 5)
        raw = "".join(store.backup_chunks())
        backup = json.loads(raw)
        self.assertEqual(backup["format"], "travel-manager-nas-backup-v2")
        self.assertEqual(raw.count(kept["data"].split(",", 1)[1]), 1)     # current file: inline in "state" only
        self.assertEqual(raw.count(dropped["data"].split(",", 1)[1]), 1)  # deleted file: once, in "originals"
        self.assertEqual(backup["state"]["documents"][0]["data"], kept["data"])
        self.assertEqual(list(backup["originals"].values()), [dropped["data"]])
        self.assertNotIn("<", raw)

    def test_current_backup_is_complete_and_importable(self):
        store = Store(self.work / "data")
        store.put(state_with(document(b"ticket one", "a"), {**document(b"", "b"), "data": "", "size": 0}), 0)
        backup = json.loads("".join(store.current_backup_chunks()))
        self.assertEqual(backup["version"], 2)
        self.assertEqual([d["data"] for d in backup["documents"]], [document(b"ticket one", "a")["data"], ""])
        self.assertTrue(all("stored" not in d for d in backup["documents"]))
        # Importing it into a fresh NAS works as before.
        fresh = Store(self.work / "fresh")
        fresh.put(backup, 0)
        self.assertEqual(fresh.get()["state"]["documents"][0]["data"], document(b"ticket one", "a")["data"])
