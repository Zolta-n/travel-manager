"""Same-origin NAS API; a stdlib WSGI core shared by Flask and local tests."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import hmac
from http import HTTPStatus
from http.cookies import SimpleCookie
import ipaddress
import json
import logging
import os
from pathlib import Path
import re
import secrets
import time
from urllib.parse import parse_qs, urlsplit

from ai_reader import AIConfig, AIError, JobBoard, build_content, build_context, build_request, call_claude, normalize_result
from extractor import extract_document, is_image, is_pdf, prepare_ai_image, reader_health, ReaderError, ReaderGate
from store import Conflict, StateError, Store

MAX_REQUEST_BYTES = 60 * 1024 * 1024
SESSION_SECONDS = 30 * 24 * 60 * 60  # sliding: renewed (at most daily) while a device keeps using the app
SESSION_RENEW_AFTER = 24 * 60 * 60
LOGIN_WINDOW, LOGIN_PER_DEVICE, LOGIN_TOTAL = 600, 5, 60
QUIET_PATHS = ("/api/revision", "/api/session", "/api/analysis", "/api/analyze/")
LOGGER = logging.getLogger("travel_manager")
CSP = ("default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
       "img-src 'self' data: blob:; font-src 'self' data:; connect-src 'self'; worker-src 'self' blob:; "
       "frame-src 'self' blob:; object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'")


class APIError(Exception):
    def __init__(self, status, message, code="request_error", extra=None):
        self.status, self.message, self.code, self.extra = status, message, code, extra or {}


def authority(value):
    if not isinstance(value, str) or not value or len(value) > 253 or any(c in value for c in "/\\@?#%\r\n\t "):
        raise ValueError("Invalid host authority.")
    parsed = urlsplit("//" + value)
    if not parsed.hostname or parsed.username or parsed.password or parsed.path:
        raise ValueError("Invalid host authority.")
    host = parsed.hostname.lower()
    try:
        ipaddress.ip_address(host)
    except ValueError:
        if not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", host) or any(not label or len(label) > 63 for label in host.split(".")):
            raise ValueError("Invalid host name.")
    port = parsed.port
    if port is not None and not 1 <= port <= 65535:
        raise ValueError("Invalid port.")
    return (host, port)


@dataclass(frozen=True)
class Config:
    password: str
    allowed_hosts: tuple[str, ...]
    data_dir: Path
    web_dir: Path
    seed_path: Path | None = None
    work_dir: Path | None = None
    secure_cookie: bool = True
    ai: AIConfig | None = None

    @classmethod
    def from_env(cls):
        secure = os.environ.get("TRAVEL_SECURE_COOKIE", "true").lower()
        if secure not in ("true", "false"):
            raise ValueError("TRAVEL_SECURE_COOKIE must be true or false.")
        root = Path(__file__).resolve().parent
        seed = os.environ.get("TRAVEL_SEED_PATH")
        work = os.environ.get("TRAVEL_WORK_DIR")
        return cls(password=os.environ.get("TRAVEL_PASSWORD", ""),
                   allowed_hosts=tuple(x.strip() for x in os.environ.get("TRAVEL_ALLOWED_HOSTS", "").split(",") if x.strip()),
                   data_dir=Path(os.environ.get("TRAVEL_DATA_DIR", str(root / "data"))),
                   web_dir=root / "web", seed_path=Path(seed) if seed else None,
                   work_dir=Path(work) if work else None, secure_cookie=secure == "true",
                   ai=AIConfig.from_env())

    def validate(self):
        password = self.password
        classes = sum((any(c.islower() for c in password), any(c.isupper() for c in password),
                       any(c.isdigit() for c in password), any(not c.isalnum() for c in password)))
        if not 16 <= len(password) <= 1024 or classes < 3:
            raise ValueError("Set a unique TRAVEL_PASSWORD of at least 16 characters using at least three of lowercase, uppercase, digits and symbols. There is no default password.")
        if not self.allowed_hosts:
            raise ValueError("TRAVEL_ALLOWED_HOSTS is mandatory (exact comma-separated hosts, including non-default ports; no wildcards).")
        for host in self.allowed_hosts:
            authority(host)
        web = self.web_dir.resolve()
        for path in (self.data_dir, self.seed_path, self.work_dir):
            if path and (path.resolve() == web or web in path.resolve().parents):
                raise ValueError("Private data, seed and reader work must be outside the public web directory.")


class Application:
    def __init__(self, config):
        config.validate()
        self.config = config
        self.allowed_hosts = {authority(host) for host in config.allowed_hosts}
        self.store = Store(config.data_dir, config.seed_path)
        self.work_dir = (config.work_dir or (self.store.root / "reader-work")).resolve()
        self.work_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
        self._load_password(config.password)
        self.cookie_name = "__Host-travel_session" if config.secure_cookie else "travel_session"
        self.readers = ReaderGate()
        self.ai = config.ai or AIConfig(api_key="")
        self.jobs = JobBoard()

    def _load_password(self, password):
        """Sessions survive restarts; only a changed password signs every device out."""
        salt, stored = self.store.meta_get("password_salt"), self.store.meta_get("password_scrypt")
        if salt and stored:
            self.salt = bytes.fromhex(salt)
            self.password_hash = self._password_hash(password)
            if hmac.compare_digest(self.password_hash.hex(), stored):
                with self.store.connect() as db:
                    kept = db.execute("SELECT COUNT(*) FROM sessions WHERE expires>?", (time.time(),)).fetchone()[0]
                LOGGER.info("Started; %d signed-in device session(s) kept", kept)
                return
        self.salt = secrets.token_bytes(32)
        self.password_hash = self._password_hash(password)
        with self.store.connect() as db:
            db.execute("DELETE FROM sessions")
        self.store.meta_set({"password_salt": self.salt.hex(), "password_scrypt": self.password_hash.hex()})
        LOGGER.info("Started; password is new or changed, so all sessions were ended")

    @staticmethod
    def client_id(env):
        """The real device address. Behind the local Cloudflare tunnel every request comes from
        127.0.0.1/the Docker bridge, so use CF-Connecting-IP (set by Cloudflare) in that case only."""
        remote = env.get("REMOTE_ADDR", "")
        try:
            local = ipaddress.ip_address(remote).is_private or ipaddress.ip_address(remote).is_loopback
        except ValueError:
            local = False
        forwarded = env.get("HTTP_CF_CONNECTING_IP", "").strip()
        if local and forwarded:
            try:
                return str(ipaddress.ip_address(forwarded))
            except ValueError:
                pass
        return remote or "unknown"

    @staticmethod
    def short(value):
        return hashlib.sha256(value.encode("utf-8")).hexdigest()[:8]

    def _password_hash(self, password):
        return hashlib.scrypt(password.encode("utf-8"), salt=self.salt, n=16384, r=8, p=1, dklen=32)

    def _security_headers(self):
        headers = [("X-Content-Type-Options", "nosniff"), ("X-Frame-Options", "DENY"),
                   ("Referrer-Policy", "no-referrer"), ("Permissions-Policy", "camera=(), microphone=(), geolocation=()"),
                   ("Content-Security-Policy", CSP), ("Cross-Origin-Resource-Policy", "same-origin"),
                   ("Cache-Control", "no-store"), ("Pragma", "no-cache")]
        if self.config.secure_cookie:
            headers.append(("Strict-Transport-Security", "max-age=31536000"))
        return headers

    def __call__(self, environ, start_response):
        started = time.monotonic()
        response = self._respond(environ, start_response)
        status = environ.get("travel.status", 0)
        path = environ.get("PATH_INFO", "/")
        if path.startswith("/api/") and (status >= 400 or environ.get("REQUEST_METHOD") != "GET" or not path.startswith(QUIET_PATHS)):
            # Never logs bodies, cookies, passwords or document contents.
            LOGGER.info("%s %s -> %s in %d ms (device %s)%s", environ.get("REQUEST_METHOD"), path, status,
                        (time.monotonic() - started) * 1000, self.short(self.client_id(environ)),
                        " " + environ["travel.note"] if environ.get("travel.note") else "")
        return response

    def _respond(self, environ, start_response):
        headers = self._security_headers()
        try:
            status, result, extra, stream = self.dispatch(environ)
            headers.extend(extra)
            if environ.get("travel.cookie") and not any(k == "Set-Cookie" for k, _ in headers):
                headers.append(environ["travel.cookie"])
            if stream:
                headers.append(("Content-Type", "application/json; charset=utf-8"))
                environ["travel.status"] = status
                start_response(str(status) + " " + HTTPStatus(status).phrase, headers)
                return (chunk.encode("utf-8") for chunk in result)
            if isinstance(result, bytes):
                body = result
            else:
                body = json.dumps(result, ensure_ascii=True, allow_nan=False, separators=(",", ":")).encode("utf-8")
                headers.append(("Content-Type", "application/json; charset=utf-8"))
        except Conflict as exc:
            environ["travel.note"] = "conflict: device had revision %s, server has %s" % (environ.get("travel.base"), exc.revision)
            status, body = self.error_body(409, str(exc), "revision_conflict", {"revision": exc.revision})
        except StateError as exc:
            status, body = self.error_body(400, str(exc), "invalid_state")
        except (ReaderError, AIError) as exc:
            status, body = self.error_body(exc.status, str(exc), exc.code)
        except APIError as exc:
            environ["travel.note"] = environ.get("travel.note") or exc.code
            status, body = self.error_body(exc.status, exc.message, exc.code, exc.extra)
        except Exception:
            LOGGER.exception("Request failed; private state and credentials are never included in application log messages")
            status, body = self.error_body(500, "The NAS could not complete this request. Check available disk space and the server log.", "server_error")
        if not any(key.lower() == "content-type" for key, _ in headers):
            headers.append(("Content-Type", "application/json; charset=utf-8"))
        headers.append(("Content-Length", str(len(body))))
        environ["travel.status"] = status
        start_response(str(status) + " " + HTTPStatus(status).phrase, headers)
        return [b"" if environ.get("REQUEST_METHOD") == "HEAD" else body]

    @staticmethod
    def error_body(status, message, code, extra=None):
        return status, json.dumps({"error": message, "code": code, **(extra or {})}, ensure_ascii=True).encode("utf-8")

    def check_host_origin(self, env, require_origin=False):
        try:
            host = authority(env.get("HTTP_HOST", ""))
        except ValueError:
            raise APIError(400, "Invalid Host header.", "invalid_host") from None
        if host not in self.allowed_hosts:
            raise APIError(400, "This host is not allowed. Check TRAVEL_ALLOWED_HOSTS.", "invalid_host")
        origin = env.get("HTTP_ORIGIN")
        fetch_site = env.get("HTTP_SEC_FETCH_SITE", "")
        if fetch_site and fetch_site not in ("same-origin", "none"):
            raise APIError(403, "Cross-origin requests are not allowed.", "invalid_origin")
        if require_origin and not origin:
            raise APIError(403, "A same-origin Origin header is required.", "invalid_origin")
        if origin:
            try:
                parsed = urlsplit(origin)
                if parsed.scheme not in (("https",) if self.config.secure_cookie else ("https", "http")):
                    raise ValueError()
                if parsed.path or parsed.query or parsed.fragment or parsed.username or parsed.password:
                    raise ValueError()
                source = authority(parsed.netloc)
                # Browsers omit default ports in Host and Origin alike.
                default = 443 if parsed.scheme == "https" else 80
                if (source[0], source[1] or default) != (host[0], host[1] or default):
                    raise ValueError()
            except ValueError:
                raise APIError(403, "Cross-origin requests are not allowed.", "invalid_origin") from None

    def read_json(self, env, maximum=MAX_REQUEST_BYTES):
        if env.get("CONTENT_TYPE", "").split(";", 1)[0].lower().strip() != "application/json":
            raise APIError(415, "Use Content-Type: application/json.")
        try:
            length = int(env.get("CONTENT_LENGTH", ""))
            if length < 0:
                raise ValueError()
        except ValueError:
            raise APIError(411, "A valid Content-Length is required.") from None
        if length > maximum:
            raise APIError(413, "Request exceeds the server size limit.", "request_too_large")
        data = env["wsgi.input"].read(length)
        if len(data) != length:
            raise APIError(400, "Incomplete JSON request.")
        def pairs(items):
            result = {}
            for key, value in items:
                if key in result:
                    raise ValueError("duplicate key")
                result[key] = value
            return result
        def reject_constant(value):
            raise ValueError("non-finite number")
        try:
            result = json.loads(data, object_pairs_hook=pairs, parse_constant=reject_constant)
            if not isinstance(result, dict):
                raise ValueError()
        except (ValueError, UnicodeError, RecursionError):
            raise APIError(400, "Invalid JSON object.") from None
        return result

    def session(self, env):
        cookies = SimpleCookie()
        try:
            cookies.load(env.get("HTTP_COOKIE", ""))
        except Exception:
            return None, None
        item = cookies.get(self.cookie_name)
        if item is None or not re.fullmatch(r"[A-Za-z0-9_-]{43}", item.value):
            return None, None
        digest = hashlib.sha256(item.value.encode("ascii")).hexdigest()
        now = time.time()
        with self.store.connect() as db:
            db.execute("DELETE FROM sessions WHERE expires<=?", (now,))
            row = db.execute("SELECT * FROM sessions WHERE token_hash=? AND expires>?", (digest, now)).fetchone()
            if row is not None and row["expires"] - now < SESSION_SECONDS - SESSION_RENEW_AFTER:
                db.execute("UPDATE sessions SET expires=? WHERE token_hash=?", (now + SESSION_SECONDS, digest))
                env["travel.cookie"] = self.cookie(item.value, SESSION_SECONDS)
            return row, digest

    def require_auth(self, env, csrf=False):
        row, digest = self.session(env)
        if row is None:
            raise APIError(401, "Sign in to access private travel data.", "authentication_required")
        if csrf:
            supplied = env.get("HTTP_X_CSRF_TOKEN", "")
            if not hmac.compare_digest(supplied.encode("utf-8"), row["csrf"].encode("utf-8")):
                raise APIError(403, "Invalid CSRF token. Sign in again.", "invalid_csrf")
        return row, digest

    def cookie(self, token, max_age):
        value = self.cookie_name + "=" + token + "; Path=/; HttpOnly; SameSite=Strict; Max-Age=" + str(max_age)
        if self.config.secure_cookie:
            value += "; Secure"
        return ("Set-Cookie", value)

    def attempt_login(self, env):
        payload = self.read_json(env, 8192)
        password = payload.get("password")
        if set(payload) != {"password"} or not isinstance(password, str) or len(password) > 1024:
            raise APIError(400, "Supply a password string.")
        client = hashlib.sha256(self.client_id(env).encode("utf-8")).hexdigest()
        now = time.time()
        # Reserve an attempt before hashing, so simultaneous requests cannot bypass the limit.
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM login_attempts WHERE attempted_at<?", (now - LOGIN_WINDOW,))
            local = db.execute("SELECT COUNT(*) FROM login_attempts WHERE client=?", (client,)).fetchone()[0]
            total = db.execute("SELECT COUNT(*) FROM login_attempts").fetchone()[0]
            if local >= LOGIN_PER_DEVICE or total >= LOGIN_TOTAL:
                oldest = db.execute("SELECT MIN(attempted_at) FROM login_attempts" + (" WHERE client=?" if local >= LOGIN_PER_DEVICE else ""),
                                    (client,) if local >= LOGIN_PER_DEVICE else ()).fetchone()[0] or now
                wait = max(60, int(oldest + LOGIN_WINDOW - now))
                raise APIError(429, "Too many sign-in attempts from this device. Try again in %d minute(s)." % ((wait + 59) // 60), "rate_limited", {"retryAfter": wait})
            db.execute("INSERT INTO login_attempts VALUES (?, ?)", (client, now))
        if not hmac.compare_digest(self._password_hash(password), self.password_hash):
            env["travel.note"] = "wrong password"
            raise APIError(401, "Incorrect password. Copy it from private/LOGIN.txt; it is long and case-sensitive.", "bad_credentials")
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        old, old_digest = self.session(env)
        with self.store.connect() as db:
            db.execute("DELETE FROM login_attempts WHERE client=?", (client,))
            db.execute("DELETE FROM sessions WHERE expires<=?", (now,))
            if old is not None:
                db.execute("DELETE FROM sessions WHERE token_hash=?", (old_digest,))
            db.execute("INSERT INTO sessions VALUES (?, ?, ?)", (hashlib.sha256(token.encode("ascii")).hexdigest(), csrf, now + SESSION_SECONDS))
        return {"authenticated": True, "csrfToken": csrf}, self.cookie(token, SESSION_SECONDS)

    # ------------------------------------------------------------ routing

    def routes(self):
        return {
            ("GET", "/"): self.page, ("HEAD", "/"): self.page,
            ("GET", "/index.html"): self.page, ("HEAD", "/index.html"): self.page,
            ("GET", "/api/session"): self.get_session,
            ("POST", "/api/login"): self.login,
            ("POST", "/api/logout"): self.logout,
            ("GET", "/api/revision"): self.get_revision,
            ("GET", "/api/state"): self.get_state,
            ("GET", "/api/original"): self.get_original,
            ("GET", "/api/analysis"): self.list_analysis,
            ("GET", "/api/analysis/result"): self.get_analysis,
            ("PUT", "/api/state"): self.put_state,
            ("GET", "/api/readers"): self.get_readers,
            ("POST", "/api/read"): self.read_document,
            ("POST", "/api/analyze"): self.start_analysis,
            ("GET", "/api/backup"): self.backup,
        }

    def dispatch(self, env):
        method, path = env.get("REQUEST_METHOD", "GET"), env.get("PATH_INFO", "/")
        self.check_host_origin(env, require_origin=method in ("POST", "PUT", "PATCH", "DELETE"))
        routes = self.routes()
        handler = routes.get((method, path))
        if handler:
            return handler(env)
        job = re.fullmatch(r"/api/analyze/([a-f0-9]{32})", path)
        if job:
            if method != "GET":
                raise APIError(405, "This method is not supported.", "method_not_allowed")
            return self.analysis_status(env, job[1])
        if any(known == path for _, known in routes):
            raise APIError(405, "This method is not supported.", "method_not_allowed")
        raise APIError(404, "Not found.", "not_found")

    @staticmethod
    def ok(result, headers=None, stream=False):
        return 200, result, headers or [], stream

    # ------------------------------------------------------------ handlers

    def page(self, env):
        try:
            body = (self.config.web_dir / "index.html").read_bytes()
        except FileNotFoundError:
            raise APIError(503, "The frontend is not present. Deploy the complete project.") from None
        return self.ok(body, [("Content-Type", "text/html; charset=utf-8")])

    def get_session(self, env):
        row, _ = self.session(env)
        return self.ok({"authenticated": True, "csrfToken": row["csrf"]} if row else {"authenticated": False})

    def login(self, env):
        result, cookie = self.attempt_login(env)
        return self.ok(result, [cookie])

    def logout(self, env):
        _, digest = self.require_auth(env, csrf=True)
        with self.store.connect() as db:
            db.execute("DELETE FROM sessions WHERE token_hash=?", (digest,))
        return self.ok({"authenticated": False}, [self.cookie("", 0)])

    def get_revision(self, env):
        self.require_auth(env)
        return self.ok({"revision": self.store.revision()})

    @staticmethod
    def query(env, name):
        values = parse_qs(env.get("QUERY_STRING", ""), max_num_fields=10).get(name, [""])
        return values[0]

    def get_state(self, env):
        self.require_auth(env)
        # ?originals=0 returns documents without file contents (fast on phones); files load on demand.
        return self.ok(self.store.get(originals=self.query(env, "originals") != "0"))

    def put_state(self, env):
        self.require_auth(env, csrf=True)
        payload = self.read_json(env)
        if set(payload) != {"state", "revision"}:
            raise APIError(400, "Supply state and revision.")
        env["travel.base"] = payload["revision"]
        revision = self.store.put(payload["state"], payload["revision"])
        env["travel.note"] = "saved revision %d (%d KB)" % (revision, int(env.get("CONTENT_LENGTH") or 0) // 1024)
        return self.ok({"revision": revision})

    def _document_id(self, env):
        identifier = self.query(env, "documentId")
        if not 1 <= len(identifier) <= 256:
            raise APIError(400, "Supply a documentId.")
        return identifier

    def get_original(self, env):
        self.require_auth(env)
        try:
            binary, _, _ = self.store.original(self._document_id(env))
        except KeyError:
            raise APIError(404, "Document not found.", "not_found") from None
        # Served as an opaque download; the page builds a typed Blob itself.
        return self.ok(binary, [("Content-Type", "application/octet-stream")])

    def list_analysis(self, env):
        self.require_auth(env)
        return self.ok({"results": self.store.ai_results()})

    def get_analysis(self, env):
        self.require_auth(env)
        try:
            return self.ok(self.store.ai_result(self._document_id(env)))
        except KeyError:
            raise APIError(404, "No finished reading for this document.", "not_found") from None

    def get_readers(self, env):
        self.require_auth(env)
        return self.ok({**reader_health(), "ai": {"enabled": self.ai.enabled, "model": self.ai.model if self.ai.enabled else "",
                                                   "provider": "Anthropic Claude API" if self.ai.enabled else ""}})

    def _document_from(self, env):
        payload = self.read_json(env, 4096)
        identifier = payload.get("documentId")
        if set(payload) != {"documentId"} or not isinstance(identifier, str) or not 1 <= len(identifier) <= 256:
            raise APIError(400, "Supply a documentId string.")
        try:
            return (identifier, *self.store.document(identifier))
        except KeyError:
            raise APIError(404, "Document not found.", "not_found") from None

    def read_document(self, env):
        self.require_auth(env, csrf=True)
        _, path, mime, name = self._document_from(env)
        if not self.readers.acquire():
            raise APIError(429, "The NAS is already reading another document. Try again shortly.", "reader_busy")
        try:
            return self.ok(extract_document(path, mime, name, self.work_dir))
        finally:
            self.readers.release()

    def start_analysis(self, env):
        self.require_auth(env, csrf=True)
        if not self.ai.enabled:
            raise APIError(503, "AI reading is not configured. Add an Anthropic API key with install.sh --set-ai-key.", "ai_disabled")
        identifier, path, mime, name = self._document_from(env)
        env["travel.note"] = "AI reading queued"

        def work():
            result = self.analyze(path, mime, name)
            result["created"] = time.time()
            self.store.save_ai_result(identifier, Path(path).parent.name + Path(path).name, result, result["created"])
            LOGGER.info("AI reading finished: %d segment(s), %s", len(result["segments"]), result.get("method", ""))
            return result
        job = self.jobs.submit(identifier, work)
        return self.ok({"jobId": job})

    def analysis_status(self, env, job_id):
        self.require_auth(env)
        job = self.jobs.get(job_id)
        if job is None:
            raise APIError(404, "This reading job has expired. Read the document again.", "not_found")
        return self.ok(job)

    def backup(self, env):
        self.require_auth(env)
        current = self.query(env, "scope") == "current"
        name = ("Travel_Manager_Backup_" if current else "travel-nas-backup-") + time.strftime("%Y%m%d-%H%M%S", time.gmtime()) + ".json"
        chunks = self.store.current_backup_chunks() if current else self.store.backup_chunks()
        return self.ok(chunks, [("Content-Disposition", 'attachment; filename="' + name + '"')], stream=True)

    # ------------------------------------------------------------ AI reading (background thread)

    def _with_reader(self, work):
        if not self.readers.lock.acquire(timeout=90):
            raise AIError("The NAS reader stayed busy. Retry reading.", "reader_busy", 429)
        try:
            return work()
        except ReaderError as exc:
            raise AIError(str(exc), exc.code, exc.status) from exc
        finally:
            self.readers.release()

    def analyze(self, path, mime, name):
        binary = Path(path).read_bytes()
        source_text, method = "", "PDF read by Claude"
        if is_pdf(path, mime, name):
            content = build_content(binary, mime, name)
        elif is_image(mime, name):
            image = self._with_reader(lambda: prepare_ai_image(path, mime, name, self.work_dir))
            content, method = build_content(binary, mime, name, image=image), "Photo read by Claude"
        else:
            local = self._with_reader(lambda: extract_document(path, mime, name, self.work_dir))
            source_text = local["text"]
            content, method = build_content(binary, mime, name, text=source_text), "Text / email read by Claude"
        state = self.store.metadata()
        raw, usage = call_claude(build_request(content, build_context(state), name, self.ai.model), self.ai)
        result = normalize_result(raw, state.get("trips", []))
        result.update(method=method + " (" + usage["model"] + ")", usage=usage, sourceText=source_text[:20000],
                      processedAt=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
        return result


def create_application(config=None):
    return Application(config or Config.from_env())


def create_app(config=None):
    """Production Flask wrapper; the same WSGI core also runs without dependencies."""
    from flask import Flask
    app = Flask(__name__, static_folder=None)
    app.wsgi_app = create_application(config)
    return app
