"""Narrow, offline data initializer; never repairs/adopts existing unrelated data."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys

MARKER = ".easy-install-id"
PENDING = ".easy-seed-pending"
UID = GID = 10001


class Refusal(ValueError):
    """A refusal with a non-secret reason code that the installer may print."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def regular(path):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise Refusal("E_DATA_LINK", "Refusing nonregular, symlink or hardlinked data file.")
    return info


def read_private(path):
    regular(path)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as stream:
        if os.fstat(stream.fileno()).st_nlink != 1:
            raise Refusal("E_DATA_LINK", "Refusing linked file.")
        return stream.read()


def create_file(path, content):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())


def sync_dir(root):
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def prepare(directory, project, seed_hash, seed, *, validate_only=False):
    if not re.fullmatch(r"[a-f0-9]{24}", project) or not re.fullmatch(r"[a-f0-9]{64}", seed_hash):
        raise Refusal("E_ARGUMENTS", "Invalid project identity or seed digest.")
    # Validate all input before even creating the marker. No network is ever used.
    if hashlib.sha256(seed).hexdigest() != seed_hash:
        raise Refusal("E_SEED_INTEGRITY", "Seed integrity check failed.")
    if not isinstance(json.loads(seed), dict):
        raise Refusal("E_SEED_INVALID", "Seed must be a JSON object.")
    root = Path(directory)
    if not stat.S_ISDIR(root.lstat().st_mode) or root.is_symlink():
        raise Refusal("E_DATA_NOT_DIR", "Data root must be a real directory.")
    marker = root / MARKER
    entries = set(os.listdir(root))
    if MARKER not in entries:
        if entries or validate_only:
            raise Refusal("E_DATA_UNMANAGED", "Unmanaged data is not empty or not initialized; no adoption or migration.")
        create_file(marker, (project + "\n").encode())
        # This nonsecret identity remains root-owned and app-readable. Unlike the
        # seed it never needs to be opened across a partial ownership handoff.
        os.chmod(marker, 0o644, follow_symlinks=False)
        sync_dir(root)
    elif read_private(marker) != (project + "\n").encode():
        raise Refusal("E_DATA_IDENTITY", "Data belongs to a different installation.")
    if not validate_only:
        os.chmod(marker, 0o644, follow_symlinks=False)
    # Read-only inspection: no recursive chmod/chown, and no following any links.
    allowed = {MARKER, PENDING, "seed.json", "originals", "travel.sqlite3", "travel.sqlite3-wal", "travel.sqlite3-shm"}
    if set(os.listdir(root)) - allowed:
        raise Refusal("E_DATA_UNKNOWN_ENTRY", "Unrecognized entries in managed data.")
    for entry in root.iterdir():
        if entry.name == "originals":
            if not stat.S_ISDIR(entry.lstat().st_mode):
                raise Refusal("E_DATA_LINK", "Originals must not be a link.")
            for parent, dirs, files in os.walk(entry, followlinks=False):
                for name in dirs:
                    if not stat.S_ISDIR((Path(parent) / name).lstat().st_mode):
                        raise Refusal("E_DATA_LINK", "Originals directory contains a link.")
                for name in files:
                    try:
                        regular(Path(parent) / name)
                    except FileNotFoundError:
                        # The running app may finish its own atomic pending write.
                        continue
        else:
            try:
                regular(entry)
            except FileNotFoundError:
                # SQLite may remove its own WAL/SHM while the app is running.
                if entry.name not in {"travel.sqlite3-wal", "travel.sqlite3-shm"}:
                    raise
    final = root / "seed.json"
    if final.exists():
        # A root helper with only CHOWN/FOWNER cannot read a 10001:10001/0600
        # seed after a partial handoff. The mandatory unprivileged validation
        # pass below the shell's root pass checks its contents before startup.
        if validate_only or final.stat().st_uid == os.geteuid():
            if read_private(final) != seed:
                raise Refusal("E_SEED_DIFFERS", "Existing seed differs; it is never overwritten.")
        if (root / PENDING).exists():
            raise Refusal("E_SEED_PENDING", "Unexpected pending seed beside existing seed; inspect privately.")
    else:
        if validate_only:
            raise Refusal("E_SEED_MISSING", "Managed seed is missing; no reseeding during validation.")
        if (root / "travel.sqlite3").exists() or (root / "originals").exists():
            raise Refusal("E_SEED_MISSING", "Managed database/originals exist without seed; no reseeding.")
        pending = root / PENDING
        if pending.exists():
            # Only this exact installer-owned scratch name can be resumed.
            regular(pending)
            os.chown(pending, os.geteuid(), os.getegid(), follow_symlinks=False)
            fd = os.open(pending, os.O_WRONLY | os.O_NOFOLLOW)
            with os.fdopen(fd, "wb") as stream:
                info = os.fstat(stream.fileno())
                if info.st_nlink != 1:
                    raise Refusal("E_SEED_PENDING", "Unsafe pending seed.")
                stream.truncate(0)
                stream.write(seed)
                stream.flush()
                os.fsync(stream.fileno())
        else:
            create_file(pending, seed)
        os.chown(pending, UID, GID, follow_symlinks=False)
        os.chmod(pending, 0o600, follow_symlinks=False)
        # App is not running during fresh preparation. No existing file is replaced.
        if final.exists() or final.is_symlink():
            raise Refusal("E_SEED_PENDING", "Seed appeared concurrently; refusing replacement.")
        os.rename(pending, final)
        sync_dir(root)
    if validate_only:
        if root.stat().st_uid != UID or stat.S_IMODE(root.stat().st_mode) != 0o700:
            raise Refusal("E_DATA_MODE", "Data ownership or mode changed; refusing automatic repair.")
        if final.stat().st_uid != UID or stat.S_IMODE(final.stat().st_mode) != 0o600:
            raise Refusal("E_SEED_MODE", "Seed ownership or mode changed; refusing automatic repair.")
        return
    # Only the fresh directory, seed and named pending file are ever chowned.
    # The identity is nonsecret/root-owned; databases/originals are never changed.
    regular(final)
    os.chown(final, UID, GID, follow_symlinks=False)
    os.chmod(final, 0o600, follow_symlinks=False)
    os.chmod(root, 0o700, follow_symlinks=False)
    sync_dir(root)
    os.chown(root, UID, GID, follow_symlinks=False)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True)
    parser.add_argument("--seed-sha256", required=True)
    parser.add_argument("--validate", action="store_true")
    args = parser.parse_args()
    try:
        prepare("/data", args.project, args.seed_sha256, sys.stdin.buffer.read(), validate_only=args.validate)
    except Refusal as exc:
        reason = exc.code
    except PermissionError:
        reason = "E_PERMISSION"
    except FileNotFoundError:
        reason = "E_MISSING"
    except ValueError:
        reason = "E_SEED_INVALID"
    except OSError:
        reason = "E_IO"
    else:
        return 0
    # Never emit seed data or raw exception text to Docker/DSM task logs; only a fixed code.
    print("Data preparation refused (" + reason + "). Consult INSTALLER_DETAILS.md.", file=sys.stderr)
    print("REASON: " + reason, file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
