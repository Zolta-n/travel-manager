"""One-time GUI-launchable ownership setup, restricted to a fresh app data folder."""
import os
from pathlib import Path
import stat


def initialize(directory="/data"):
    root = Path(directory)
    if root.is_symlink() or not root.is_dir():
        raise SystemExit("Refusing: the selected app data folder is not a normal directory.")
    entries = list(root.iterdir())
    if any(entry.name != "seed.json" for entry in entries):
        raise SystemExit("Refusing: this folder is not fresh. Only the supplied seed.json may be present. Never run this helper on existing app data or a shared/volume folder.")
    seed = root / "seed.json"
    if not seed.exists() or not stat.S_ISREG(seed.lstat().st_mode) or seed.is_symlink():
        raise SystemExit("Copy private/seed.json into the new app data folder as seed.json before running this helper.")
    os.chown(seed, 10001, 10001, follow_symlinks=False)
    os.chmod(seed, 0o600, follow_symlinks=False)
    os.chown(root, 10001, 10001, follow_symlinks=False)
    os.chmod(root, 0o700, follow_symlinks=False)
    print("Only the new app data folder and its seed.json were prepared for UID/GID 10001. Start the normal Travel Manager project next.")


if __name__ == "__main__":
    initialize()
