"""Immutable SQLite revisions and private, content-addressed attachment storage."""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import time
import uuid
from contextlib import contextmanager

MAX_FILE_BYTES = 12 * 1024 * 1024
# Originals of the current documents. A backup of this much (base64) must still fit a browser's
# string limit (~512 MB) so it can be restored through the app.
MAX_CURRENT_BYTES = 250 * 1024 * 1024
MAX_METADATA_BYTES = 4 * 1024 * 1024
DATA_URI = re.compile(r"data:([a-zA-Z0-9.+/-]*);base64,([A-Za-z0-9+/]*={0,2})\Z")
COLLECTIONS = {"trips": "tripId", "activities": "activityId", "documents": "documentId", "expenses": "expenseId"}


class StateError(ValueError):
    pass


class Conflict(StateError):
    def __init__(self, revision):
        super().__init__("Another device saved first. Reload the current state before saving again.")
        self.revision = revision


def empty_state():
    return {"version": 2, "selected": "", "trips": [], "activities": [], "documents": [], "expenses": [], "settings": {"owner": "", "lastSaved": ""}}


def _scalar(value):
    return value is None or isinstance(value, (str, bool, int, float))


def _validate_auto(value, depth=0):
    if depth > 16:
        raise StateError("Reading metadata is nested too deeply.")
    if isinstance(value, dict):
        if len(value) > 1000:
            raise StateError("Oversized reading metadata object.")
        for key, item in value.items():
            if not isinstance(key, str) or len(key) > 256:
                raise StateError("Invalid reading metadata key.")
            _validate_auto(item, depth + 1)
    elif isinstance(value, list):
        if len(value) > 1000:
            raise StateError("Oversized reading metadata list.")
        for item in value:
            _validate_auto(item, depth + 1)
    elif not _scalar(value) or (isinstance(value, float) and not math.isfinite(value)) or len(str(value)) > 120000:
        raise StateError("Invalid reading metadata field.")


def prepare_state(state, max_file=MAX_FILE_BYTES, max_current=MAX_CURRENT_BYTES, known=None):
    """Validate a state. `known` maps sha256 -> size of originals already on the NAS; a document
    sent without `data` but with `stored: true` and a known `hash` keeps that original (no re-upload)."""
    known = known or {}
    if not isinstance(state, dict) or type(state.get("version")) is not int or state["version"] != 2:
        raise StateError("Expected a version 2 travel state.")
    if set(state) != {"version", "selected", "settings", *COLLECTIONS}:
        raise StateError("State must contain version, selected, settings and the four record collections.")
    if not isinstance(state["selected"], str) or len(state["selected"]) > 256:
        raise StateError("Invalid selected trip ID.")
    settings = state["settings"]
    if not isinstance(settings, dict) or len(settings) > 100:
        raise StateError("Invalid settings.")
    clean = {"version": 2, "selected": state["selected"], "settings": dict(settings)}
    for key, value in settings.items():
        if not isinstance(key, str) or len(key) > 100 or not _scalar(value) or len(str(value)) > 120000:
            raise StateError("Invalid settings field.")
    originals = []
    total = 0
    for collection, id_key in COLLECTIONS.items():
        rows = state.get(collection)
        if not isinstance(rows, list) or len(rows) > 10000:
            raise StateError("Invalid or oversized " + collection + " collection.")
        clean[collection] = []
        ids = set()
        for row in rows:
            if not isinstance(row, dict) or len(row) > 100:
                raise StateError("Invalid record in " + collection + ".")
            identifier = row.get(id_key)
            if not isinstance(identifier, str) or not identifier or len(identifier) > 256 or identifier in ids:
                raise StateError("Missing, duplicate or oversized " + id_key + ".")
            ids.add(identifier)
            document = dict(row)
            keep = document.pop("stored", False) is True
            for key, value in document.items():
                if collection == "documents" and key == "auto":
                    if not isinstance(value, dict):
                        raise StateError("Invalid reading metadata.")
                    _validate_auto(value)
                    if len(json.dumps(value, allow_nan=False, ensure_ascii=True)) > 600000:
                        raise StateError("Reading metadata exceeds 600,000 characters.")
                    continue
                if not isinstance(key, str) or len(key) > 100 or key.startswith("_") or not _scalar(value):
                    raise StateError("Invalid record field.")
                if isinstance(value, float) and not math.isfinite(value):
                    raise StateError("Non-finite numeric fields are not supported.")
                if not (collection == "documents" and key == "data") and len(str(value)) > 120000:
                    raise StateError("A metadata field exceeds 120,000 characters.")
            if collection == "documents":
                data = document.pop("data", "")
                if data:
                    if not isinstance(data, str) or len(data) > ((max_file + 2) // 3) * 4 + 256:
                        raise StateError("Original exceeds the 12 MiB per-file limit.")
                    match = DATA_URI.fullmatch(data)
                    if not match:
                        raise StateError("Original must be a base64 data URI, not a URL or file path.")
                    try:
                        binary = base64.b64decode(match[2], validate=True)
                    except (ValueError, binascii.Error) as exc:
                        raise StateError("Invalid base64 original.") from exc
                    if len(binary) > max_file:
                        raise StateError("Original exceeds the 12 MiB per-file limit.")
                    total += len(binary)
                    if total > max_current:
                        raise StateError("Current originals exceed the %d MB limit." % (max_current // 1048576))
                    mime = match[1].lower() or "application/octet-stream"
                    digest = hashlib.sha256(binary).hexdigest()
                    document.update(size=len(binary), mime=mime, hash=digest)
                    originals.append((identifier, digest, mime, binary))
                elif keep and isinstance(document.get("hash"), str) and document["hash"] in known:
                    digest = document["hash"]
                    mime = document.get("mime") if isinstance(document.get("mime"), str) and re.fullmatch(r"[a-zA-Z0-9.+/-]{1,100}", document.get("mime", "")) else "application/octet-stream"
                    total += known[digest]
                    if total > max_current:
                        raise StateError("Current originals exceed the %d MB limit." % (max_current // 1048576))
                    document.update(size=known[digest], mime=mime.lower())
                    originals.append((identifier, digest, mime.lower(), None))
                else:
                    if data not in (None, ""):
                        raise StateError("Invalid document data.")
                    size = document.get("size", 0)
                    if isinstance(size, bool) or not isinstance(size, (int, float)) or not math.isfinite(size) or not 0 <= size <= max_file:
                        raise StateError("Invalid document size.")
                    document["size"] = int(size)
            clean[collection].append(document)
    try:
        metadata = json.dumps(clean, ensure_ascii=True, allow_nan=False, separators=(",", ":"))
    except (ValueError, TypeError) as exc:
        raise StateError("Invalid state values.") from exc
    if len(metadata.encode("utf-8")) > MAX_METADATA_BYTES:
        raise StateError("State metadata exceeds 4 MiB.")
    return metadata, originals


def _fsync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class Store:
    def __init__(self, directory, seed_path=None, *, max_file=MAX_FILE_BYTES, max_current=MAX_CURRENT_BYTES):
        self.root = Path(directory).resolve()
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.originals = self.root / "originals"
        self.originals.mkdir(mode=0o700, exist_ok=True)
        self.database = self.root / "travel.sqlite3"
        self.max_file = max_file
        self.max_current = max_current
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS snapshots (
                    revision INTEGER PRIMARY KEY, created_at REAL NOT NULL, state_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS originals (
                    sha256 TEXT PRIMARY KEY, size INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS snapshot_originals (
                    revision INTEGER NOT NULL REFERENCES snapshots(revision),
                    document_id TEXT NOT NULL,
                    sha256 TEXT NOT NULL REFERENCES originals(sha256), mime TEXT NOT NULL,
                    PRIMARY KEY(revision, document_id)
                );
                CREATE TABLE IF NOT EXISTS sessions (
                    token_hash TEXT PRIMARY KEY, csrf TEXT NOT NULL, expires REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS login_attempts (client TEXT NOT NULL, attempted_at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS ai_results (
                    document_id TEXT NOT NULL, sha256 TEXT NOT NULL, created REAL NOT NULL, result_json TEXT NOT NULL,
                    PRIMARY KEY(document_id, sha256)
                );
                CREATE INDEX IF NOT EXISTS login_attempts_time ON login_attempts(attempted_at);
            """)
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM snapshots LIMIT 1").fetchone() is None:
                if seed_path:
                    with open(seed_path, encoding="utf-8") as source:
                        state = json.load(source)
                else:
                    state = empty_state()
                metadata, originals = prepare_state(state, self.max_file, self.max_current)
                self._insert(db, 0, metadata, originals)
        os.chmod(self.database, 0o600)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.database, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA synchronous=FULL")
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def original_path(self, digest):
        if not re.fullmatch(r"[a-f0-9]{64}", digest):
            raise StateError("Invalid internal original identifier.")
        return self.originals / digest[:2] / digest[2:]

    def _write_original(self, digest, binary):
        final = self.original_path(digest)
        if final.exists():
            if final.stat().st_size != len(binary) or hashlib.sha256(final.read_bytes()).hexdigest() != digest:
                raise OSError("Stored original failed its integrity check.")
            return
        final.parent.mkdir(mode=0o700, exist_ok=True)
        _fsync_directory(self.originals)
        pending = final.parent / (".pending-" + uuid.uuid4().hex)
        try:
            fd = os.open(pending, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as output:
                output.write(binary)
                output.flush()
                os.fsync(output.fileno())
            os.replace(pending, final)
            _fsync_directory(final.parent)
        finally:
            pending.unlink(missing_ok=True)

    def _insert(self, db, revision, metadata, originals):
        # Files become durable before any database reference can commit. An interrupted
        # write can leave an unreferenced file, never a committed missing original.
        for _, digest, _, binary in originals:
            if binary is None:
                # Kept original: it must still be on disk with its recorded size.
                row = db.execute("SELECT size FROM originals WHERE sha256=?", (digest,)).fetchone()
                path = self.original_path(digest)
                if row is None or not path.is_file() or path.stat().st_size != row["size"]:
                    raise StateError("A stored original is missing on the NAS. Reattach it.")
                continue
            self._write_original(digest, binary)
            db.execute("INSERT OR IGNORE INTO originals VALUES (?, ?)", (digest, len(binary)))
        db.execute("INSERT INTO snapshots VALUES (?, ?, ?)", (revision, time.time(), metadata))
        db.executemany("INSERT INTO snapshot_originals VALUES (?, ?, ?, ?)",
                       [(revision, identifier, digest, mime) for identifier, digest, mime, _ in originals])

    def _reconstruct(self, db, row, originals=True):
        state = json.loads(row["state_json"])
        refs = {r["document_id"]: r for r in db.execute("SELECT * FROM snapshot_originals WHERE revision=?", (row["revision"],))}
        for document in state["documents"]:
            ref = refs.get(document["documentId"])
            document["data"] = ""
            if ref and not originals:
                del document["data"]
                document.update(stored=True, hash=ref["sha256"], mime=ref["mime"])
            elif ref:
                binary = self.original_path(ref["sha256"]).read_bytes()
                if hashlib.sha256(binary).hexdigest() != ref["sha256"]:
                    raise OSError("Stored original failed its integrity check.")
                document["data"] = "data:" + ref["mime"] + ";base64," + base64.b64encode(binary).decode("ascii")
        return state

    def revision(self):
        with self.connect() as db:
            return db.execute("SELECT MAX(revision) FROM snapshots").fetchone()[0]

    def get(self, originals=True):
        """Latest state. With originals=False documents carry `stored`/`hash` instead of file contents."""
        with self.connect() as db:
            db.execute("BEGIN")
            row = db.execute("SELECT * FROM snapshots ORDER BY revision DESC LIMIT 1").fetchone()
            return {"state": self._reconstruct(db, row, originals), "revision": row["revision"]}

    def known_originals(self, db):
        return {r["sha256"]: r["size"] for r in db.execute("SELECT sha256, size FROM originals")}

    def original(self, identifier):
        path, mime, name = self.document(identifier)
        return path.read_bytes(), mime, name

    # -------- small key/value settings (password fingerprint etc.)
    def meta_get(self, key):
        with self.connect() as db:
            row = db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
            return row["value"] if row else None

    def meta_set(self, values):
        with self.connect() as db:
            db.executemany("INSERT INTO meta VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", list(values.items()))

    # -------- finished AI readings, kept so any device can apply them
    def save_ai_result(self, document_id, digest, result, created=None):
        created = created or time.time()
        with self.connect() as db:
            db.execute("DELETE FROM ai_results WHERE created<?", (time.time() - 60 * 86400,))
            db.execute("INSERT INTO ai_results VALUES (?, ?, ?, ?) ON CONFLICT(document_id, sha256) DO UPDATE SET created=excluded.created, result_json=excluded.result_json",
                       (document_id, digest, created, json.dumps(result, ensure_ascii=True, allow_nan=False)))

    def ai_results(self):
        with self.connect() as db:
            return [{"documentId": r["document_id"], "sha256": r["sha256"], "created": r["created"]}
                    for r in db.execute("SELECT document_id, sha256, created FROM ai_results ORDER BY created DESC LIMIT 500")]

    def ai_result(self, document_id):
        with self.connect() as db:
            row = db.execute("SELECT * FROM ai_results WHERE document_id=? ORDER BY created DESC LIMIT 1", (document_id,)).fetchone()
            if row is None:
                raise KeyError(document_id)
            return {"documentId": row["document_id"], "sha256": row["sha256"], "created": row["created"], "result": json.loads(row["result_json"])}

    def metadata(self):
        """Latest state without attachment contents (cheap; used for AI trip context)."""
        with self.connect() as db:
            row = db.execute("SELECT state_json FROM snapshots ORDER BY revision DESC LIMIT 1").fetchone()
            return json.loads(row["state_json"])

    def put(self, state, revision):
        if type(revision) is not int or revision < 0:
            raise StateError("revision must be a non-negative integer.")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            metadata, originals = prepare_state(state, self.max_file, self.max_current, self.known_originals(db))
            current = db.execute("SELECT MAX(revision) FROM snapshots").fetchone()[0]
            if current != revision:
                raise Conflict(current)
            self._insert(db, current + 1, metadata, originals)
            return current + 1

    def document(self, identifier):
        with self.connect() as db:
            db.execute("BEGIN")
            row = db.execute("SELECT * FROM snapshots ORDER BY revision DESC LIMIT 1").fetchone()
            document = next((d for d in json.loads(row["state_json"])["documents"] if d["documentId"] == identifier), None)
            if document is None:
                raise KeyError("Document not found.")
            ref = db.execute("SELECT * FROM snapshot_originals WHERE revision=? AND document_id=?", (row["revision"], identifier)).fetchone()
            if ref is None:
                raise StateError("This document is metadata-only. Reattach its original first.")
            path = self.original_path(ref["sha256"])
            if hashlib.sha256(path.read_bytes()).hexdigest() != ref["sha256"]:
                raise OSError("Stored original failed its integrity check.")
            return path, ref["mime"], document.get("name", "")

    def _data_uri(self, digest, mime):
        binary = self.original_path(digest).read_bytes()
        if hashlib.sha256(binary).hexdigest() != digest:
            raise OSError("Stored original failed its integrity check.")
        return "data:" + mime + ";base64," + base64.b64encode(binary).decode("ascii")

    def _stream_state(self, db, row):
        """A state as JSON with its files inline, one document at a time, so memory stays at one file."""
        state = self._reconstruct(db, row, originals=False)
        documents = state.pop("documents")
        head = json.dumps(state, ensure_ascii=True, allow_nan=False).replace("<", "\\u003c")
        yield head[:-1] + ',"documents":['
        for index, document in enumerate(documents):
            if document.pop("stored", False):
                document["data"] = self._data_uri(document["hash"], document["mime"])
            yield ("," if index else "") + json.dumps(document, ensure_ascii=True, allow_nan=False).replace("<", "\\u003c")
        yield "]}"

    def current_backup_chunks(self):
        """Current state only, with originals - restorable through the app."""
        with self.connect() as db:
            db.execute("BEGIN")
            latest = db.execute("SELECT * FROM snapshots ORDER BY revision DESC LIMIT 1").fetchone()
            yield from self._stream_state(db, latest)

    def backup_chunks(self):
        # One read transaction fixes the revision boundary; originals are immutable.
        with self.connect() as db:
            db.execute("BEGIN")
            latest = db.execute("SELECT * FROM snapshots ORDER BY revision DESC LIMIT 1").fetchone()
            # v2: every file appears once. "state" has the current files inline (restorable through the app);
            # history states refer to files by "hash"; files only older revisions use are in "originals".
            yield '{"format":"travel-manager-nas-backup-v2","exportedAt":' + json.dumps(time.time())
            yield ',"revision":' + str(latest["revision"]) + ',"state":'
            yield from self._stream_state(db, latest)
            current = {r["sha256"] for r in db.execute("SELECT sha256 FROM snapshot_originals WHERE revision=?", (latest["revision"],))}
            yield ',"history":['
            first = True
            for row in db.execute("SELECT * FROM snapshots WHERE revision<=? ORDER BY revision", (latest["revision"],)):
                if not first:
                    yield ","
                first = False
                yield json.dumps({"revision": row["revision"], "createdAt": row["created_at"], "state": self._reconstruct(db, row, originals=False)},
                                 ensure_ascii=True, allow_nan=False).replace("<", "\\u003c")
            yield '],"originals":{'
            older = db.execute("SELECT DISTINCT sha256, mime FROM snapshot_originals WHERE revision<=? ORDER BY sha256", (latest["revision"],)).fetchall()
            first = True
            for ref in older:
                if ref["sha256"] in current:
                    continue
                yield ("" if first else ",") + json.dumps(ref["sha256"]) + ":" + json.dumps(self._data_uri(ref["sha256"], ref["mime"]))
                first = False
            yield "}}"
