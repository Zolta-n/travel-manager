"""Every file the Dockerfile copies must be allowed by .dockerignore (CI builds the image only after merge)."""
from pathlib import Path
import fnmatch
import re
import unittest

ROOT = Path(__file__).resolve().parents[1]


def allowed(path):
    verdict = False
    for line in (ROOT / ".dockerignore").read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        negate, pattern = line.startswith("!"), line.lstrip("!").rstrip("/")
        if pattern == "**" or fnmatch.fnmatch(path, pattern) or path.startswith(pattern + "/"):
            verdict = negate
    return verdict


class DockerContextTests(unittest.TestCase):
    def test_copied_files_are_in_the_build_context(self):
        copied = []
        for line in (ROOT / "Dockerfile").read_text().splitlines():
            match = re.match(r"\s*COPY\s+(?!--)(.+?)\s+\S+\s*$", line)
            if match:
                copied += [p.rstrip("/") for p in match.group(1).split()]
        self.assertIn("inbound.py", copied)
        for name in copied:
            files = [p.relative_to(ROOT).as_posix() for p in (ROOT / name).rglob("*") if p.is_file()] if (ROOT / name).is_dir() else [name]
            self.assertTrue((ROOT / name).exists(), name)
            self.assertTrue(any(allowed(f) for f in files), name + " is copied by the Dockerfile but excluded by .dockerignore")


if __name__ == "__main__":
    unittest.main()
