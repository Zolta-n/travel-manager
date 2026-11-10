"""Bounded, offline extraction. External document parsers run in a worker process."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import threading
import uuid

READER_TIMEOUT = 55
MAX_TEXT = 100000
MAX_WORKER_OUTPUT = 2 * 1024 * 1024
TOOLS = ("pdfinfo", "pdftotext", "pdftoppm", "tesseract")
IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tif", ".tiff")


class ReaderError(ValueError):
    def __init__(self, message, code="reader_error", status=422):
        super().__init__(message)
        self.code = code
        self.status = status


def reader_health():
    import importlib.util
    return {"text_email": True, "pdfinfo": bool(shutil.which("pdfinfo")),
            "pdf_text": bool(shutil.which("pdftotext")), "pdf_raster": bool(shutil.which("pdftoppm")),
            "image_ocr": bool(shutil.which("tesseract")), "pillow": bool(importlib.util.find_spec("PIL")),
            "ocr_language": "eng", "offline": True, "max_pages": 20}


def is_image(mime, name):
    return (mime or "").startswith("image/") or Path(name or "").suffix.lower() in IMAGE_SUFFIXES


def is_pdf(path, mime, name):
    with open(path, "rb") as source:
        return source.read(5) == b"%PDF-" or mime == "application/pdf" or Path(name or "").suffix.lower() == ".pdf"


def _run_worker(path, mime, name, work_root, *extra, collect=None):
    """Run reader_worker.py in its own session with a hard timeout.
    `collect(job_root, result)` may read files from the job folder before it is removed."""
    root = Path(work_root).resolve()
    root.mkdir(parents=True, mode=0o700, exist_ok=True)
    job_root = root / ("request-" + uuid.uuid4().hex)
    job_root.mkdir(mode=0o700)
    command = [sys.executable, str(Path(__file__).with_name("reader_worker.py")),
               str(Path(path).resolve()), mime, name, str(job_root), *extra]
    try:
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, start_new_session=True,
                                   env={**os.environ, "OMP_THREAD_LIMIT": "1", "OMP_NUM_THREADS": "1"})
        try:
            output, _ = process.communicate(timeout=READER_TIMEOUT)
        except subprocess.TimeoutExpired as exc:
            os.killpg(process.pid, signal.SIGKILL)
            process.communicate()
            raise ReaderError("The local reader timed out. Try fewer pages or a clearer original.", "timeout") from exc
        if len(output) > MAX_WORKER_OUTPUT:
            raise ReaderError("The reader output exceeded its safety limit.", "output_limit")
        try:
            result = json.loads(output)
        except (ValueError, UnicodeDecodeError) as exc:
            raise ReaderError("The local reader stopped at its safety limit or could not read this original.") from exc
        if not isinstance(result, dict):
            raise ReaderError("The local reader could not read this original.")
        if "error" in result:
            raise ReaderError(result["error"], result.get("code", "reader_error"), result.get("status", 422))
        if process.returncode != 0:
            raise ReaderError("The local reader could not read this original.")
        return collect(job_root, result) if collect else result
    finally:
        shutil.rmtree(job_root, ignore_errors=True)


def extract_document(path, mime, name, work_root):
    result = _run_worker(path, mime, name, work_root)
    if not isinstance(result.get("text"), str):
        raise ReaderError("The local reader could not read this original.")
    return result


def prepare_ai_image(path, mime, name, work_root):
    """Return (jpeg_bytes, media_type) produced by the sandboxed worker."""
    def collect(job_root, result):
        image = job_root / str(result.get("image", ""))
        if image.parent != job_root or not image.is_file():
            raise ReaderError("The photo could not be prepared for AI reading.")
        return image.read_bytes(), "image/jpeg"
    return _run_worker(path, mime, name, work_root, "--ai-image", collect=collect)


def split_email(path, work_root):
    """Split an email in the sandboxed worker. Returns the worker result with each
    attachment's bytes under "data" (files are read before the job folder is removed)."""
    def collect(job_root, result):
        def read(name):
            target = job_root / str(name)
            if target.parent != job_root or not target.is_file():
                raise ReaderError("The email could not be split safely.", "invalid_email")
            return target.read_bytes()
        attachments = result.get("attachments") if isinstance(result.get("attachments"), list) else []
        return {"text": str(result.get("text") or ""), "subject": str(result.get("subject") or "Email"),
                "email": read(result.get("email")), "skipped": result.get("skipped") or [],
                "attachments": [{**{k: a.get(k) for k in ("name", "mime", "kind", "size")}, "data": read(a.get("file"))}
                                for a in attachments if isinstance(a, dict)]}
    return _run_worker(path, "message/rfc822", "email.eml", work_root, "--split-email", collect=collect)


class ReaderGate:
    def __init__(self):
        self.lock = threading.BoundedSemaphore(1)

    def acquire(self):
        return self.lock.acquire(blocking=False)

    def release(self):
        self.lock.release()
