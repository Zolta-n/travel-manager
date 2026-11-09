"""Build the NAS upgrade ZIP and refresh PACKAGE.sha256.

    python tools/build_nas_package.py            -> dist/Travel_Manager_NAS_Easy_Setup_v2.zip

The real itinerary (private/seed.json) is never put in the ZIP or in git: it already sits on
the NAS and File Station extraction leaves it alone. Its checksum is taken from the local copy
if present, otherwise from the existing PACKAGE.sha256, because the installer verifies it.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
FOLDER = "Travel_Manager_NAS_Easy_Setup_v2"
# Must match the allowlist in install.sh (16 files).
PACKAGE = [".easy-package", "install.sh", "Dockerfile", ".dockerignore", "compose.yaml", "easy_data.py",
           "ai_reader.py", "app.py", "store.py", "extractor.py", "reader_worker.py", "server.py",
           "initialize_data.py", "requirements.txt", "web/index.html", "private/seed.json"]
EXTRA = ["START_HERE.md", "INSTALLER_DETAILS.md", "README.md", "VERIFICATION.md", "HANDOFF.md", ".env.example"]
SEED = "private/seed.json"


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def seed_digest():
    local = ROOT / SEED
    if local.exists():
        return digest(local)
    for line in (ROOT / "PACKAGE.sha256").read_text().splitlines():
        if line.endswith("  " + SEED):
            return line.split("  ", 1)[0]
    sys.exit("No seed checksum available: keep the private/seed.json line in PACKAGE.sha256.")


def manifest():
    lines = [(seed_digest() if name == SEED else digest(ROOT / name)) + "  " + name for name in PACKAGE]
    (ROOT / "PACKAGE.sha256").write_text("\n".join(lines) + "\n")


def build():
    manifest()
    out = ROOT / "dist" / (FOLDER + ".zip")
    out.parent.mkdir(exist_ok=True)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as archive:
        for name in PACKAGE + ["PACKAGE.sha256"] + EXTRA:
            if name == SEED:
                continue
            archive.write(ROOT / name, FOLDER + "/" + name)
    print(out)


if __name__ == "__main__":
    build()
