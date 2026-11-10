"""Private extraction worker. Never accepts a URL or invokes a shell."""
from __future__ import annotations

import csv
from email import policy
from email.parser import BytesParser
from html.parser import HTMLParser
import io
import json
import os
from pathlib import Path
import re
import resource
import shutil
import subprocess
import sys
import time
import uuid
import warnings

MAX_PAGES = 20
MAX_PIXELS = 20_000_000
MAX_TEXT = 100000
MAX_OUTPUT_BYTES = 8 * 1024 * 1024


class Failure(Exception):
    def __init__(self, message, code="reader_error", status=422):
        self.message, self.code, self.status = message, code, status


class PlainHTML(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.suppressed = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "head"):
            self.suppressed += 1
        if tag in ("br", "p", "div", "tr", "li", "h1", "h2", "h3"):
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style", "head") and self.suppressed:
            self.suppressed -= 1
        if tag in ("p", "div", "tr", "li"):
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.suppressed:
            self.parts.append(data)


def text_decode(binary):
    for encoding in ("utf-8-sig", "utf-16" if binary[:2] in (b"\xff\xfe", b"\xfe\xff") else "utf-8", "cp1252"):
        try:
            return binary.decode(encoding)
        except UnicodeError:
            pass
    return binary.decode("utf-8", errors="replace")


def html_text(text):
    parser = PlainHTML()
    parser.feed(text)
    return "".join(parser.parts)


def run(command, cwd, deadline):
    executable = shutil.which(command[0])
    if not executable:
        raise Failure("A required local reader is unavailable: " + command[0] + ". Rebuild the container image.", "reader_unavailable", 503)
    seconds = min(25, deadline - time.monotonic())
    if seconds <= 0:
        raise Failure("The local reader reached its time limit.", "timeout")
    stdout = cwd / (uuid.uuid4().hex + ".stdout")
    stderr = cwd / (uuid.uuid4().hex + ".stderr")
    try:
        with stdout.open("wb") as out, stderr.open("wb") as err:
            try:
                result = subprocess.run([executable, *command[1:]], stdin=subprocess.DEVNULL,
                                        stdout=out, stderr=err, cwd=cwd, timeout=seconds,
                                        env={**os.environ, "LC_ALL": "C.UTF-8", "OMP_THREAD_LIMIT": "1"})
            except subprocess.TimeoutExpired as exc:
                raise Failure("The local reader reached its time limit.", "timeout") from exc
        if stdout.stat().st_size > MAX_OUTPUT_BYTES or stderr.stat().st_size > MAX_OUTPUT_BYTES:
            raise Failure("The local reader output exceeded its safety limit.", "output_limit")
        return result.returncode, stdout.read_bytes(), stderr.read_bytes()
    finally:
        stdout.unlink(missing_ok=True)
        stderr.unlink(missing_ok=True)


def image_check(path):
    try:
        from PIL import Image
    except ImportError as exc:
        raise Failure("Pillow is unavailable. Rebuild the container image.", "reader_unavailable", 503) from exc
    Image.MAX_IMAGE_PIXELS = MAX_PIXELS
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(path) as image:
                width, height = image.size
                if width <= 0 or height <= 0 or width * height > MAX_PIXELS or max(width, height) > 12000:
                    raise Failure("Image exceeds the 20-million-pixel or 12,000-pixel edge safety limit.", "pixel_limit")
                if getattr(image, "n_frames", 1) != 1:
                    raise Failure("Multi-frame images are not automatically read. Use one image per page.", "page_limit")
                image.verify()
    except Failure:
        raise
    except Exception as exc:
        raise Failure("Image is invalid or exceeds the image safety limit.", "invalid_image") from exc


def ocr(path, cwd, deadline):
    image_check(path)
    code, output, _ = run(["tesseract", str(path), "stdout", "-l", "eng", "--psm", "3", "tsv"], cwd, deadline)
    if code:
        raise Failure("The English OCR reader could not read this image. Try a clearer PNG or JPEG.")
    words, confidences = [], []
    previous_line = None
    for row in csv.DictReader(io.StringIO(output.decode("utf-8", errors="replace")), delimiter="\t"):
        word = (row.get("text") or "").strip()
        if not word:
            continue
        line = tuple(row.get(key) for key in ("page_num", "block_num", "par_num", "line_num"))
        words.append(("\n" if previous_line is not None and line != previous_line else " ") + word)
        previous_line = line
        try:
            confidence = float(row.get("conf", "-1"))
            if confidence >= 0:
                confidences.append(confidence)
        except ValueError:
            pass
    return "".join(words).strip()[:MAX_TEXT], round(sum(confidences) / len(confidences), 1) if confidences else 0


def read_email(binary):
    message = BytesParser(policy=policy.default).parsebytes(binary)
    headers = [key + ": " + str(message[key]) for key in ("Subject", "From", "To", "Date") if message[key]]
    bodies = []
    html_bodies = []
    count = 0
    for part in message.walk():
        count += 1
        if count > 200:
            raise Failure("Email contains too many MIME parts.", "part_limit")
        if part.is_multipart() or part.get_content_disposition() == "attachment":
            continue
        mime = part.get_content_type()
        if mime not in ("text/plain", "text/html"):
            continue
        payload = part.get_payload(decode=True) or b""
        try:
            text = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
        except LookupError:
            text = text_decode(payload)
        if mime == "text/plain":
            bodies.append(text)
        else:
            html_bodies.append(html_text(text))
    return "\n".join([*headers, "", *(bodies or html_bodies)])[:MAX_TEXT]


MAX_ATTACHMENTS = 10
MAX_ATTACHMENT_BYTES = 12 * 1024 * 1024
SMALL_INLINE_IMAGE = 10 * 1024  # logos, signatures and tracking pixels
IMAGE_MAGIC = ((b"\xff\xd8\xff", "image/jpeg", ".jpg"), (b"\x89PNG\r\n\x1a\n", "image/png", ".png"),
               (b"GIF8", "image/gif", ".gif"), (b"RIFF", "image/webp", ".webp"))


def _attachment_kind(mime, name, payload):
    """(kind, mime, suffix) for a part we can read, else (None, reason, '')."""
    suffix = Path(name).suffix.lower()
    if payload[:5] == b"%PDF-" or mime == "application/pdf" or suffix == ".pdf":
        if payload[:5] != b"%PDF-":
            return None, "not a valid PDF", ""
        if b"/Encrypt" in payload[:4096] or b"/Encrypt" in payload[-4096:]:
            return None, "password-protected PDF", ""
        return "pdf", "application/pdf", ".pdf"
    for magic, media, ext in IMAGE_MAGIC:
        if payload.startswith(magic) and (media != "image/webp" or payload[8:12] == b"WEBP"):
            return "image", media, ext
    if mime.startswith("image/") or suffix in (".heic", ".heif", ".tif", ".tiff", ".bmp"):
        return None, "photo format not supported (use JPEG or PNG)", ""
    if payload.startswith(b"PK\x03\x04") and (suffix == ".docx" or mime == DOCX_MIME):
        return "docx", DOCX_MIME, ".docx"
    if suffix == ".doc" or payload.startswith(b"\xd0\xcf\x11\xe0"):
        return None, "old Word .doc (save as .docx or PDF)", ""
    if mime == "text/calendar" or suffix == ".ics":
        return None, "calendar file (not read yet)", ""
    if suffix == ".pkpass" or mime == "application/vnd.apple.pkpass":
        return None, "Apple Wallet pass (not read yet)", ""
    if mime in ("text/plain", "text/html") or suffix in (".txt", ".htm", ".html"):
        return "text", "text/plain", ".txt"
    return None, "file type not supported", ""


def _safe_name(value, fallback):
    name = re.sub(r"\s+", " ", re.sub(r"[\x00-\x1f\x7f/\\*?\"<>|]+", " ", str(value or "")).replace(":", " -")).strip(" .")[:120]
    return name or fallback


def split_email(path, root):
    """Body text, readable attachments (written to `root`) and a small copy of the email without them.
    Forwarded emails attached as .eml are walked too, so their PDFs are found."""
    binary = path.read_bytes()
    message = BytesParser(policy=policy.default).parsebytes(binary)
    attachments, skipped, count = [], [], 0
    for part in message.walk():
        count += 1
        if count > 200:
            raise Failure("Email contains too many MIME parts.", "part_limit")
        if part.is_multipart():
            continue
        mime, disposition = part.get_content_type(), part.get_content_disposition()
        try:
            filename = part.get_filename() or ""
        except Exception:
            filename = ""
        if mime in ("text/plain", "text/html") and disposition != "attachment" and not filename:
            continue  # message body: read_email() covers it
        payload = part.get_payload(decode=True) or b""
        if not payload:
            continue
        kind, media, ext = _attachment_kind(mime, filename, payload)
        if kind == "image" and len(payload) < SMALL_INLINE_IMAGE:
            continue
        name = _safe_name(filename, "attachment-%d%s" % (len(attachments) + 1, ext))
        if kind is None:
            skipped.append({"name": name, "reason": media})
        elif len(payload) > MAX_ATTACHMENT_BYTES:
            skipped.append({"name": name, "reason": "larger than 12 MB"})
        elif len(attachments) >= MAX_ATTACHMENTS:
            skipped.append({"name": name, "reason": "more than %d attachments" % MAX_ATTACHMENTS})
        else:
            if ext and not name.lower().endswith(ext) and kind != "text":
                name += ext
            target = Path(root) / ("part-%d%s" % (len(attachments), ext))
            target.write_bytes(payload)
            attachments.append({"file": target.name, "name": name, "mime": media, "kind": kind, "size": len(payload)})
    text = read_email(binary)
    # A small stand-in for the original: headers and body text only, so attachments are stored once.
    from email.message import EmailMessage
    copy = EmailMessage()
    for key in ("Subject", "From", "To", "Cc", "Date", "Message-ID"):
        try:
            if message[key]:
                copy[key] = str(message[key])
        except Exception:
            pass
    body = text.split("\n\n", 1)[1] if "\n\n" in text else text
    copy.set_content(body or "(no text)")
    (Path(root) / "email.eml").write_bytes(copy.as_bytes())
    try:
        subject = str(message["Subject"] or "")
    except Exception:
        subject = ""
    return {"text": text, "subject": _safe_name(subject, "Email")[:100], "email": "email.eml",
            "attachments": attachments, "skipped": skipped}


DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
MAX_DOCX_XML = 20 * 1024 * 1024
W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def _docx_part(archive, name):
    try:
        info = archive.getinfo(name)
    except KeyError:
        return None
    if info.file_size > MAX_DOCX_XML or (info.compress_size and info.file_size / info.compress_size > 200):
        raise Failure("This Word file is too large or too compressed to read safely.", "size_limit")
    with archive.open(info) as source:
        data = source.read(MAX_DOCX_XML + 1)
    if len(data) > MAX_DOCX_XML or b"<!DOCTYPE" in data[:4096] or b"<!ENTITY" in data:
        raise Failure("This Word file contains unsupported XML declarations.", "invalid_docx")
    from xml.etree import ElementTree
    try:
        return ElementTree.fromstring(data)
    except ElementTree.ParseError as exc:
        raise Failure("This Word file is damaged and could not be read.", "invalid_docx") from exc


def _docx_text(element):
    """Paragraphs on their own lines; table cells separated by ' | ' so rows stay readable."""
    lines = []

    def paragraph(p):
        parts = []
        for node in p.iter():
            if node.tag == W + "t" and node.text:
                parts.append(node.text)
            elif node.tag == W + "tab":
                parts.append("\t")
            elif node.tag in (W + "br", W + "cr"):
                parts.append("\n")
        return "".join(parts).strip()

    def walk(parent):
        for child in parent:
            if child.tag == W + "p":
                text = paragraph(child)
                if text:
                    lines.append(text)
            elif child.tag == W + "tbl":
                for row in child.iter(W + "tr"):
                    cells = [" ".join(filter(None, (paragraph(p) for p in cell.iter(W + "p")))) for cell in row.findall(W + "tc")]
                    if any(cells):
                        lines.append(" | ".join(cells))
            elif child.tag in (W + "sdt", W + "sdtContent", W + "body", W + "hdr", W + "ftr", W + "customXml"):
                walk(child)
    walk(element)
    return "\n".join(lines)


def read_docx(path):
    import zipfile
    try:
        archive = zipfile.ZipFile(path)
    except zipfile.BadZipFile as exc:
        raise Failure("This Word file is damaged and could not be read.", "invalid_docx") from exc
    with archive:
        names = archive.namelist()
        if len(names) > 2000:
            raise Failure("This Word file has too many parts.", "part_limit")
        body = _docx_part(archive, "word/document.xml")
        if body is None:
            raise Failure("This is not a Word .docx document.", "invalid_docx")
        texts = []
        for name in sorted(n for n in names if n.startswith("word/header") and n.endswith(".xml"))[:6]:
            part = _docx_part(archive, name)
            if part is not None:
                texts.append(_docx_text(part))
        texts.append(_docx_text(body.find(W + "body") if body.find(W + "body") is not None else body))
        for name in sorted(n for n in names if n.startswith("word/footer") and n.endswith(".xml"))[:6]:
            part = _docx_part(archive, name)
            if part is not None:
                texts.append(_docx_text(part))
    text = "\n".join(t for t in texts if t.strip())
    if not text.strip():
        raise Failure("This Word file contains no text. If it only holds a picture of the booking, upload the picture or a PDF instead.", "no_text")
    return text[:MAX_TEXT]


def extract(path, mime, name, cwd):
    deadline = time.monotonic() + 48
    with path.open("rb") as source:
        first = source.read(16)
    suffix = Path(name).suffix.lower()
    if first.startswith(b"%PDF-") or mime == "application/pdf" or suffix == ".pdf":
        code, output, error = run(["pdfinfo", "-f", "1", "-l", str(MAX_PAGES), "-box", str(path)], cwd, deadline)
        info = output.decode("utf-8", errors="replace")
        if re.search(r"^Encrypted:\s+yes", info, re.I | re.M) or re.search(r"password|encrypt", error.decode("utf-8", errors="replace"), re.I):
            raise Failure("This PDF is encrypted. Remove its protection in a trusted local PDF tool before reading it.", "encrypted_pdf")
        if code:
            raise Failure("This PDF is invalid or cannot be read.", "invalid_pdf")
        match = re.search(r"^Pages:\s+(\d+)", info, re.M)
        if not match or not 1 <= int(match[1]) <= MAX_PAGES:
            raise Failure("PDFs must have between 1 and 20 pages for automatic reading.", "page_limit")
        pages = int(match[1])
        sizes = re.findall(r"(?:Page(?:\s+\d+)?\s+size):\s*([0-9.]+)\s+x\s+([0-9.]+)", info)
        for width, height in sizes:
            if float(width) * float(height) * (120 / 72) ** 2 > MAX_PIXELS or max(float(width), float(height)) * 120 / 72 > 12000:
                raise Failure("A PDF page exceeds the raster pixel safety limit.", "pixel_limit")
        code, output, _ = run(["pdftotext", "-f", "1", "-l", str(pages), "-layout", str(path), "-"], cwd, deadline)
        if code:
            raise Failure("The PDF text reader could not read this original.", "invalid_pdf")
        # Evaluate each page so hybrid PDFs do not silently omit scanned pages.
        text_pages = output.decode("utf-8", errors="replace").split("\f")
        texts, confidences = [], []
        used_ocr = False
        for page in range(1, pages + 1):
            text = text_pages[page - 1].strip() if page <= len(text_pages) else ""
            if sum(char.isalnum() for char in text) >= 15:
                texts.append(text)
                continue
            used_ocr = True
            prefix = cwd / ("page-" + str(page))
            code, _, _ = run(["pdftoppm", "-f", str(page), "-l", str(page), "-singlefile", "-r", "120", "-scale-to", "3500", "-gray", "-png", str(path), str(prefix)], cwd, deadline)
            if code:
                raise Failure("The local PDF raster reader could not read a scanned page.")
            image = prefix.with_suffix(".png")
            text, confidence = ocr(image, cwd, deadline)
            image.unlink(missing_ok=True)
            texts.append(text)
            confidences.append(confidence)
        return {"text": "\n\n".join(texts)[:MAX_TEXT], "method": "PDF OCR (pdftoppm + tesseract eng)" if used_ocr else "PDF text (pdftotext)",
                "confidence": min(confidences) if confidences else 100}
    if suffix == ".docx" or mime == DOCX_MIME or (first.startswith(b"PK\x03\x04") and suffix in (".docm", ".dotx")):
        return {"text": read_docx(path), "method": "Word document (Python stdlib)", "confidence": 100}
    if first.startswith(b"\xd0\xcf\x11\xe0") or suffix == ".doc":
        raise Failure("Old Word .doc files cannot be read automatically. In Word choose File → Save as → Word Document (.docx) or PDF, then upload that.", "unsupported_type")
    if mime == "message/rfc822" or suffix == ".eml":
        return {"text": read_email(path.read_bytes()), "method": "Email (Python stdlib MIME)", "confidence": 100}
    if mime.startswith("text/") or suffix in (".txt", ".csv", ".ics", ".html", ".htm"):
        text = text_decode(path.read_bytes())
        if mime == "text/html" or suffix in (".html", ".htm"):
            text = html_text(text)
        return {"text": text[:MAX_TEXT], "method": "Text (Python stdlib)", "confidence": 100}
    if mime.startswith("image/") or suffix in (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tif", ".tiff"):
        text, confidence = ocr(path, cwd, deadline)
        return {"text": text, "method": "Image OCR (tesseract eng)", "confidence": confidence}
    raise Failure("This format is kept intact but is not supported for automatic reading. Use PDF, Word (.docx), a single-page image, EML or text.", "unsupported_type")


AI_IMAGE_EDGE = 2600
AI_IMAGE_BYTES = 3_700_000


def prepare_ai_image(path, root):
    """Upright, size-bounded JPEG for Claude. Phone photos often carry EXIF rotation."""
    image_check(path)
    from PIL import Image, ImageOps
    Image.MAX_IMAGE_PIXELS = MAX_PIXELS
    with Image.open(path) as source:
        image = ImageOps.exif_transpose(source).convert("RGB")
    image.thumbnail((AI_IMAGE_EDGE, AI_IMAGE_EDGE))
    output = Path(root) / "ai-image.jpg"
    for quality in (88, 75, 60):
        image.save(output, "JPEG", quality=quality, optimize=True)
        if output.stat().st_size <= AI_IMAGE_BYTES:
            return {"image": output.name, "media": "image/jpeg", "text": ""}
        image.thumbnail((image.width * 3 // 4, image.height * 3 // 4))
    raise Failure("The photo is too large to prepare for AI reading.", "pixel_limit")


def main():
    os.umask(0o077)
    resource.setrlimit(resource.RLIMIT_AS, (768 * 1024 * 1024,) * 2)
    resource.setrlimit(resource.RLIMIT_FSIZE, (16 * 1024 * 1024,) * 2)
    resource.setrlimit(resource.RLIMIT_CPU, (50, 50))
    resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
    path, mime, name, root, *mode = sys.argv[1:]
    if mode == ["--split-email"]:
        try:
            result = split_email(Path(path), root)
        except Failure as exc:
            result = {"error": exc.message, "code": exc.code, "status": exc.status}
        except Exception:
            result = {"error": "The email could not be split safely.", "code": "invalid_email", "status": 422}
        print(json.dumps(result, ensure_ascii=True, allow_nan=False))
        return
    if mode == ["--ai-image"]:
        try:
            result = prepare_ai_image(Path(path), root)
        except Failure as exc:
            result = {"error": exc.message, "code": exc.code, "status": exc.status}
        except Exception:
            result = {"error": "The photo could not be prepared safely.", "code": "invalid_image", "status": 422}
        print(json.dumps(result, ensure_ascii=True, allow_nan=False))
        return
    work = Path(root) / ("job-" + uuid.uuid4().hex)
    work.mkdir(mode=0o700)
    try:
        result = extract(Path(path), mime, name, work)
        if len(result["text"]) >= MAX_TEXT:
            warning = "[Reader output truncated at 100,000 characters; review the original for remaining details.]"
            result["text"] = result["text"][:MAX_TEXT - len(warning) - 2] + "\n\n" + warning
            result["truncated"] = True
            result["warning"] = warning
            result["confidence"] = min(result["confidence"], 50)
    except Failure as exc:
        result = {"error": exc.message, "code": exc.code, "status": exc.status}
    except Exception:
        result = {"error": "The local reader could not read this original safely.", "code": "reader_error", "status": 422}
    finally:
        shutil.rmtree(work, ignore_errors=True)
    print(json.dumps(result, ensure_ascii=True, allow_nan=False))


if __name__ == "__main__":
    main()
