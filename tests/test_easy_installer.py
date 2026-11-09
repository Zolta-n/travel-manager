"""Runs the real POSIX installer in private fixtures; all Docker/root writes are fake.

The test copy injects shell command/metadata doubles and refreshes its test
manifest. Production install.sh has no environment/test-mode privilege bypass.
This workspace cannot create links or persist POSIX chmod, so link/mode tests
use explicit metadata doubles too. No test requires Docker, root or a NAS.
"""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import unittest
from unittest.mock import patch
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import easy_data

PACKAGE_FILES = (
    ".easy-package", "install.sh", "Dockerfile", ".dockerignore", "compose.yaml", "easy_data.py",
    "ai_reader.py", "app.py", "store.py", "extractor.py", "reader_worker.py", "server.py", "initialize_data.py",
    "requirements.txt", "web/index.html", "private/seed.json",
)

MOCK_TOOL = r'''import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

state_path = Path(os.environ["MOCK_STATE"])
state = json.loads(state_path.read_text())
command = sys.argv[1]
args = sys.argv[2:]
project_dir = Path(os.environ["MOCK_PROJECT"])
def save():
    state_path.write_text(json.dumps(state))
    metadata = {}
    for name in (".env", ".installer/id", "private/LOGIN.txt", "private/STATUS.txt"):
        metadata[("%a", str(project_dir / name))] = "600"
    for name in (".installer", ".install.lock"):
        metadata[("%a", str(project_dir / name))] = "700"
    for name in (".env", ".installer/id"):
        metadata[("%u", str(project_dir / name))] = "0"
    for field, key in (("%u", "owners"), ("%a", "modes")):
        for path, value in state[key].items(): metadata[(field, path)] = str(value)
    for path in state["links"]: metadata[("link", path)] = "1"
    for path in state["hardlinks"]: metadata[("%h", path)] = "2"
    Path(os.environ["MOCK_METADATA"]).write_text("".join(field + "\t" + path + "\t" + value + "\n" for (field, path), value in metadata.items()))
def out(value):
    print(value)
def die(code=1):
    save()
    raise SystemExit(code)
def owner_key(value):
    return str(Path(value).absolute())
if command == "id":
    out("0")
    die(0)
if command == "is_link":
    die(0 if owner_key(args[0]) in state.get("links", []) else 1)
if command == "chmod":
    state["modes"][owner_key(args[-1])] = args[0]
    die(0)
if command == "stat":
    key = owner_key(args[2])
    if args[:2] == ["-c", "%h"] and key in state.get("hardlinks", []):
        out(2); die(0)
    if args[:2] == ["-c", "%a"]:
        if key in state.get("modes", {}):
            out(state["modes"][key]); die(0)
        if key == str(project_dir / ".installer") or key == str(project_dir / ".install.lock"):
            out(700); die(0)
        if key in [str(project_dir / name) for name in (".env", ".installer/id", "private/LOGIN.txt", "private/STATUS.txt")]:
            out(600); die(0)
    if args[:2] == ["-c", "%u"]:
        if key in state.get("owners", {}):
            out(state["owners"][key]); die(0)
        if key == str(project_dir / ".env") or key.startswith(str(project_dir / ".installer") + "/"):
            out("0"); die(0)
    die(subprocess.run(["/usr/bin/stat", *args]).returncode)
if command == "chown":
    state["chowns"].append(args)
    uid = int(args[0].split(":")[0])
    state["owners"][owner_key(args[1])] = uid
    die(0)
if command == "sleep":
    state["sleeps"].append(args); die(0)
if command == "date":
    state["clock"] += state.get("clock_step", 0)
    out(1000 + state["clock"]); die(0)
if command == "docker-compose":
    args = ["compose", *args]
state["calls"].append(args)

def leak_then_fail():
    # Deliberately simulate a misbehaving Compose error containing the secret.
    env = project_dir / ".env"
    if env.exists():
        print(env.read_text(), file=sys.stderr)
    die()
if args == ["info"]:
    die(1 if state.get("daemon_fail") else 0)
if args[:2] == ["compose", "version"]:
    die(1 if state.get("legacy") and command == "docker" else 0)
if args == ["version"]:
    die(0)
if args[:2] == ["image", "inspect"]:
    image = args[-1]
    if image not in state["images"]:
        die()
    if "--format" in args:
        out(state["images"][image])
    else:
        out("[]")
    die(0)
if args[:2] == ["container", "inspect"]:
    found = any(args[-1] in (cid, item["name"]) for cid, item in state["containers"].items())
    die(0 if found else 1)
if args[0] == "ps":
    items = state["containers"].items()
    if "--filter" in args:
        label = args[args.index("--filter") + 1].split("=", 2)[-1]
        items = [(cid, item) for cid, item in items if item["project"] == label]
    elif "-a" not in args:
        items = [(cid, item) for cid, item in items if item.get("running", True)]
    for cid, item in items:
        out(cid)
    die(0)
if args[0] == "inspect":
    target, fmt = args[-1], args[args.index("--format") + 1]
    item = state["containers"].get(target)
    if item is None:
        item = next((i for i in state["containers"].values() if i["name"] == target), None)
    if item is None:
        die()
    if "org.travel-manager.easy.install" in fmt: out(item["id"])
    elif '"com.docker.compose.project"' in fmt: out(item["project"])
    elif "com.docker.compose.service" in fmt: out(item.get("service", "travel-manager"))
    elif "working_dir" in fmt: out(item["dir"])
    elif "config_files" in fmt: out(item["dir"] + "/compose.yaml")
    elif ".Config.Image" in fmt: out(item["image"])
    elif "PortBindings" in fmt: out(item.get("ports", "8787/tcp 127.0.0.1 8787"))
    elif ".State.Health" in fmt: out(state.get("health", "healthy"))
    elif ".State.Running" in fmt: out("true" if item.get("running", True) else "false")
    else: raise RuntimeError(fmt)
    die(0)
if args[0] == "pull":
    if state.get("build_fail"):
        leak_then_fail()
    state["images"][args[-1]] = "https://github.com/Zolta-n/travel-manager"
    state["builds"] += 1
    die(0)
if args[0] == "run" and "--project" not in args:
    # Image readability check (python -c ...) as UID 10001.
    state["image_checks"] = state.get("image_checks", 0) + 1
    out(state.get("image_check", "OK")); die(0)
if args[0] == "run":
    from unittest.mock import patch
    if state.get("prep_fail"):
        print("REASON: E_SEED_MODE", file=sys.stderr)
        die()
    spec = importlib.util.spec_from_file_location("fixture_easy_data", project_dir / "easy_data.py")
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)
    identity = args[args.index("--project") + 1]
    digest = args[args.index("--seed-sha256") + 1]
    data = project_dir / "data"
    def chown(path, uid, gid, **kwargs):
        state["helper_chowns"].append(str(path))
        state["owners"][str(path)] = uid
    # Python tests are deliberately unprivileged. Fake ownership affects only
    # shell stat and this container simulator, never the real filesystem.
    old_lstat = Path.lstat
    old_stat = Path.stat
    def lstat(path, *a, **kw):
        info = list(old_lstat(path, *a, **kw))
        if str(path) in state.get("links", []): info[0] = 0o120777
        if str(path) in state.get("hardlinks", []): info[3] = 2
        return os.stat_result(info)
    def metadata(path, *a, **kw):
        info = list(old_stat(path, *a, **kw))
        if path == data: info[0] = 0o40700
        if path == data / "seed.json": info[0] = 0o100600
        return os.stat_result(info)
    try:
        with patch.object(helper.os, "chown", side_effect=chown), patch.object(helper, "UID", os.getuid()), patch.object(helper, "GID", os.getgid()), patch.object(Path, "lstat", lstat), patch.object(Path, "stat", metadata):
            helper.prepare(data, identity, digest, sys.stdin.buffer.read(), validate_only="--validate" in args)
        if "--validate" not in args:
            state["owners"][str(data)] = 10001
            state["owners"][str(data / "seed.json")] = 10001
            state["owners"][str(data / ".easy-install-id")] = 0
            state["modes"][str(data)] = "700"
    except Exception as error:
        print(type(error).__name__, file=sys.stderr)
        die()
    die(0)
if args[0] == "compose":
    if "--project-directory" not in args:
        raise RuntimeError(args)
    directory = args[args.index("--project-directory") + 1]
    project = args[args.index("--project-name") + 1]
    env = dict(line.split("=", 1) for line in (project_dir / ".env").read_text().splitlines() if "=" in line)
    identity = env["TRAVEL_INSTALL_ID"]
    if "config" in args:
        if state.get("config_fail"): leak_then_fail()
        die(0)
    if "up" in args:
        if state.get("up_fail"): leak_then_fail()
        state["containers"]["cid-owned"] = {"name": project + "-travel-manager-1", "id": identity, "project": project, "dir": directory, "image": "ghcr.io/zolta-n/travel-manager:latest"}
        database = project_dir / "data/travel.sqlite3"
        if not database.exists():
            database.write_bytes(b"initial managed database")
        die(0)
    if "ps" in args:
        out("cid-owned"); die(0)
raise RuntimeError((command, args))
'''


def manifest(directory):
    (directory / "PACKAGE.sha256").write_text("".join(
        hashlib.sha256((directory / name).read_bytes()).hexdigest() + "  " + name + "\n"
        for name in PACKAGE_FILES
    ))


class InstallerHarness(unittest.TestCase):
    def setUp(self):
        self.work = ROOT / (".qa-easy-" + uuid.uuid4().hex)
        self.work.mkdir(mode=0o700)
        self.project = self.work / "app with spaces"
        self.project.mkdir(mode=0o700)
        for name in PACKAGE_FILES:
            source = ROOT / name
            target = self.project / name
            target.parent.mkdir(mode=0o700, exist_ok=True)
            if name == "private/seed.json" and not source.exists():
                # The real itinerary never leaves the NAS (not in git/CI); use a stand-in.
                target.write_text(json.dumps({"version": 2, "selected": "", "trips": [], "activities": [], "documents": [], "expenses": [], "settings": {}}))
            else:
                shutil.copyfile(source, target)
            target.chmod(0o600)
        self.runner = self.work / "mock_tool.py"
        self.runner.write_text(MOCK_TOOL)
        installer = self.project / "install.sh"
        text = installer.read_text()
        functions = "\n".join(command + '() { /usr/bin/python3 "$MOCK_RUNNER" ' + command + ' "$@"; }' for command in ("docker", "chown", "chmod", "sleep", "date"))
        functions += '''
mock_compose() { /usr/bin/python3 "$MOCK_RUNNER" docker-compose "$@"; }
timeout() { shift; "$@"; }
id() { printf '0\\n'; }
stat() {
    case "$3" in /*) key=$3 ;; *) key=$PWD/$3 ;; esac
    value=$(awk -F '\\t' -v f="$2" -v p="$key" '$1==f && $2==p {v=$3} END {print v}' "$MOCK_METADATA")
    if [ -n "$value" ]; then printf '%s\\n' "$value"; else /usr/bin/stat "$@"; fi
}
mock_is_link() {
    case "$1" in /*) key=$1 ;; *) key=$PWD/$1 ;; esac
    awk -F '\\t' -v p="$key" '$1=="link" && $2==p {found=1} END {exit !found}' "$MOCK_METADATA"
}
'''
        text = text.replace('command -v docker-compose', 'command -v mock_compose')
        text = text.replace("export PATH LC_ALL=C\n", "export PATH LC_ALL=C\n" + functions)
        text = text.replace('[ -x "$DOCKER" ]', '[ "$DOCKER" = docker ]')
        text = re.sub(r'\[ ! -L ([^\]\n]+) \]', r'! mock_is_link \1', text)
        text = re.sub(r'\[ -L ([^\]\n]+) \]', r'mock_is_link \1', text)
        installer.write_text(text)
        manifest(self.project)
        self.state_path = self.work / "docker-state.json"
        self.state = {"owners": {}, "modes": {}, "links": [], "hardlinks": [], "chowns": [], "helper_chowns": [], "calls": [], "containers": {}, "images": {}, "builds": 0, "clock": 0, "sleeps": []}
        self.save()

    def tearDown(self):
        shutil.rmtree(self.work, ignore_errors=True)

    def save(self):
        self.state_path.write_text(json.dumps(self.state))
        # Refresh the same metadata projection the fake Docker/root runner uses.
        env = dict(os.environ, MOCK_STATE=str(self.state_path), MOCK_PROJECT=str(self.project), MOCK_RUNNER=str(self.runner), MOCK_METADATA=str(self.work / "metadata.tsv"))
        subprocess.run(["/usr/bin/python3", str(self.runner), "id"], env=env, check=True, stdout=subprocess.DEVNULL)

    def run_installer(self, *arguments, ok=True):
        env = dict(os.environ, MOCK_STATE=str(self.state_path), MOCK_PROJECT=str(self.project), MOCK_RUNNER=str(self.runner), MOCK_METADATA=str(self.work / "metadata.tsv"), PYTHONDONTWRITEBYTECODE="1")
        result = subprocess.run(["/bin/sh", str(self.project / "install.sh"), *arguments], env=env, text=True, capture_output=True, timeout=45)
        self.state = json.loads(self.state_path.read_text())
        if ok:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        else:
            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertNotIn("SUCCESS", result.stdout)
        env_file = self.project / ".env"
        if env_file.exists():
            secret = next((line.split("=", 1)[1].strip("'") for line in env_file.read_text().splitlines() if line.startswith("TRAVEL_PASSWORD=")), "")
            if secret:
                self.assertNotIn(secret, result.stdout + result.stderr)
                status = self.project / "private/STATUS.txt"
                if status.exists(): self.assertNotIn(secret, status.read_text())
        if str(self.project / ".install.lock") not in self.state["links"]:
            self.assertFalse((self.project / ".install.lock").exists(), "Installer must release only its own lock")
        return result

    def install(self, **kwargs):
        return self.run_installer("--host", "travel.example.net", **kwargs)


class InstallerTests(InstallerHarness):
    def test_first_install_and_rerun_preserve_secret_database_and_seed(self):
        result = self.install()
        self.assertIn("healthy locally", result.stdout)
        self.assertIn("does not verify external HTTPS", result.stdout)
        files = [".env", "private/LOGIN.txt", ".installer/id", "data/seed.json"]
        before = {name: (self.project / name).read_bytes() for name in files}
        database = self.project / "data/travel.sqlite3"
        database.write_bytes(b"user-managed-database")
        self.run_installer()
        for name, content in before.items(): self.assertEqual((self.project / name).read_bytes(), content)
        self.assertEqual(database.read_bytes(), b"user-managed-database")
        self.assertEqual(self.state["builds"], 1)
        # umask/chmod cannot persist on this workspace filesystem; these are
        # explicit command/metadata assertions, not a real POSIX permission test.
        self.assertIn("umask 077", (self.project / "install.sh").read_text())
        self.assertEqual(self.state["modes"][str(self.project / "private/STATUS.txt")], "600")
        root_runs = [c for c in self.state["calls"] if c[0] == "run" and c[c.index("--user") + 1] == "0:0"]
        self.assertIn("AI reading: no Anthropic API key", result.stdout)
        self.assertEqual(len(root_runs), 1, "Managed reruns must never invoke the root helper")
        self.assertIn("NOT VERIFIED", (self.project / "private/STATUS.txt").read_text())

    def test_plan_is_nonmutating_and_check_on_rerun_preserves_files(self):
        before = sorted(str(p.relative_to(self.project)) for p in self.project.rglob("*"))
        self.run_installer("--plan", "--host", "travel.example.net")
        self.assertEqual(before, sorted(str(p.relative_to(self.project)) for p in self.project.rglob("*")))
        self.assertFalse(self.state["builds"])
        self.install()
        protected = {n: (self.project / n).read_bytes() for n in (".env", "private/LOGIN.txt", "private/STATUS.txt")}
        self.run_installer("--check")
        self.assertEqual(protected, {n: (self.project / n).read_bytes() for n in protected})

    def test_requires_first_hostname(self):
        self.run_installer(ok=False)
        self.assertFalse((self.project / ".installer").exists())

    def test_strict_hostname_validation(self):
        invalid = ["https://travel.example.net", "evil;id", "$(id)", "a b", "a/b", "x@host", "*.example.net", "a..b", "-a.example.net", "a-.example.net", "a.example.net.", "UPPER.net", "a:0", "a:65536", "a:00443", "[::1]", "a,b", "a\nb", "a" * 64 + ".net"]
        for value in invalid:
            with self.subTest(host=value): self.run_installer("--host", value, ok=False)
        self.assertFalse(self.state["calls"])
        self.run_installer("--plan", "--host", "travel.example.net:8443")

    def test_existing_unmanaged_data_config_seed_and_credentials_refused(self):
        for name in ("data/seed.json", "data/something.sqlite", ".env", "private/LOGIN.txt", "private/STATUS.txt"):
            with self.subTest(name=name):
                target = self.project / name
                target.parent.mkdir(exist_ok=True)
                target.write_bytes(b"do-not-touch")
                target.chmod(0o600)
                self.install(ok=False)
                self.assertEqual(target.read_bytes(), b"do-not-touch")
                target.unlink()
        self.assertFalse(self.state["builds"])

    def test_existing_env_host_password_and_secure_cookie_never_silently_changed(self):
        self.install()
        env = self.project / ".env"
        good = env.read_bytes()
        self.run_installer("--host", "other.example.net", ok=False)
        self.assertEqual(env.read_bytes(), good)
        bad_values = [good.replace(b"TRAVEL_SECURE_COOKIE=true", b"TRAVEL_SECURE_COOKIE=false"), good.replace(good.splitlines()[1], b"TRAVEL_PASSWORD='Short_7!'"), good + b"EXTRA=1\n", good.replace(b"travel.example.net", b"$(id)")]
        for content in bad_values:
            env.write_bytes(content)
            self.run_installer(ok=False)
            self.assertEqual(env.read_bytes(), content)
        env.write_bytes(good)
        self.state["modes"][str(env)] = "644"; self.save()
        self.run_installer(ok=False)
        self.assertEqual(self.state["modes"][str(env)], "644")

    def test_failed_build_resumes_without_rotating_credentials(self):
        self.state["build_fail"] = True; self.save()
        self.install(ok=False)
        env = (self.project / ".env").read_bytes()
        login = (self.project / "private/LOGIN.txt").read_bytes()
        self.assertIn("FAILED", (self.project / "private/STATUS.txt").read_text())
        self.state["build_fail"] = False; self.save()
        self.install()
        self.assertEqual((self.project / ".env").read_bytes(), env)
        self.assertEqual((self.project / "private/LOGIN.txt").read_bytes(), login)

    def test_failed_preparation_and_start_can_resume(self):
        self.state["prep_fail"] = True; self.save()
        result = self.install(ok=False)
        self.assertIn("reason E_SEED_MODE", result.stderr)
        identity = (self.project / ".installer/id").read_bytes()
        self.state["prep_fail"] = False; self.state["up_fail"] = True; self.save()
        self.install(ok=False)
        seed = (self.project / "data/seed.json").read_bytes()
        self.state["up_fail"] = False; self.save()
        self.install()
        self.assertEqual((self.project / ".installer/id").read_bytes(), identity)
        self.assertEqual((self.project / "data/seed.json").read_bytes(), seed)
        self.assertEqual(self.state["builds"], 1)

    def test_virtual_links_and_actual_path_traversal_are_refused(self):
        # Virtual link metadata is required here: the workspace forbids real links.
        target = self.work / "outside"
        target.write_bytes(b"outside-unchanged")
        for name in ("app.py", ".env", "private/LOGIN.txt", "data", ".installer", ".install.lock"):
            with self.subTest(symlink=name):
                item = self.project / name
                existed = item.exists()
                if not existed: item.write_bytes(b"virtual-link")
                self.state["links"] = [str(item)]; self.save()
                self.install(ok=False)
                self.state["links"] = []; self.save()
                if not existed: item.unlink()
        item = self.project / "app.py"
        original = item.read_bytes()
        self.state["hardlinks"] = [str(item)]; self.save()
        self.install(ok=False)
        self.assertEqual(item.read_bytes(), original)
        self.state["hardlinks"] = []; self.save()
        env = dict(os.environ, MOCK_STATE=str(self.state_path), MOCK_PROJECT=str(self.project), MOCK_RUNNER=str(self.runner), MOCK_METADATA=str(self.work / "metadata.tsv"))
        path = str(self.project / ".." / self.project.name / "install.sh")
        result = subprocess.run(["/bin/sh", path, "--plan", "--host", "travel.example.net"], env=env, capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("physical", result.stderr)
        self.assertEqual(target.read_bytes(), b"outside-unchanged")

    def test_data_seed_virtual_link_guard_on_rerun(self):
        self.install()
        seed = self.project / "data/seed.json"
        original = seed.read_bytes()
        for field in ("links", "hardlinks"):
            self.state[field] = [str(seed)]; self.save()
            self.run_installer(ok=False)
            self.assertEqual(seed.read_bytes(), original)
            self.state[field] = []; self.save()

    def test_live_lock_is_never_removed_or_broken(self):
        lock = self.project / ".install.lock"
        lock.mkdir(mode=0o700)
        # This lock belongs to somebody else: run directly, not our release assertion.
        env = dict(os.environ, MOCK_STATE=str(self.state_path), MOCK_PROJECT=str(self.project), MOCK_RUNNER=str(self.runner), MOCK_METADATA=str(self.work / "metadata.tsv"))
        result = subprocess.run(["/bin/sh", str(self.project / "install.sh"), "--host", "travel.example.net"], env=env, text=True, capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(lock.is_dir())
        self.assertFalse((self.project / ".env").exists())

    def test_unrelated_port_container_is_not_touched(self):
        self.state["containers"]["v1"] = {"name": "unrelated", "id": "v1", "project": "v1", "dir": "/other", "image": "travel-manager-nas:local"}
        self.save()
        self.install(ok=False)
        self.assertEqual(self.state["containers"]["v1"]["image"], "travel-manager-nas:local")
        self.assertEqual(self.state["builds"], 0)
        self.assertFalse(any("up" in call for call in self.state["calls"]))

    def test_project_image_name_and_public_port_collisions(self):
        self.install()
        image = next(iter(self.state["images"]))
        self.state["images"][image] = "unrelated"; self.save()
        self.run_installer(ok=False)
        self.state["images"][image] = "https://github.com/Zolta-n/travel-manager"
        self.state["containers"]["cid-owned"]["ports"] = "8787/tcp 0.0.0.0 8787"; self.save()
        self.run_installer(ok=False)
        self.state["containers"]["cid-owned"]["ports"] = ""
        self.state["containers"]["cid-owned"]["service"] = "unrelated"; self.save()
        self.run_installer(ok=False)

    def test_stopped_name_collision_not_adopted(self):
        self.state["build_fail"] = True; self.save()
        self.install(ok=False)
        identity = (self.project / ".installer/id").read_text().strip()
        self.state["build_fail"] = False
        self.state["containers"]["unrelated"] = {"name": "tme" + identity + "-travel-manager-1", "id": "other", "project": "other", "dir": "/elsewhere", "image": "other:latest", "running": False}
        self.save()
        self.run_installer(ok=False)
        self.assertFalse(any("up" in call for call in self.state["calls"]))

    def test_build_flag_only_rebuilds_project_scoped_image(self):
        self.install()
        self.run_installer("--build")
        self.assertEqual(self.state["builds"], 2)
        pulls = [c for c in self.state["calls"] if c[0] == "pull"]
        self.assertEqual([c[-1] for c in pulls], ["ghcr.io/zolta-n/travel-manager:latest"] * 2)

    def test_helpers_are_offline_narrow_and_normal_compose_not_root(self):
        self.install()
        runs = [c for c in self.state["calls"] if c[0] == "run" and "--project" in c]
        self.assertEqual(len(runs), 2)
        checks = [c for c in self.state["calls"] if c[0] == "run" and "--project" not in c]
        self.assertTrue(checks)
        for command in checks:
            self.assertEqual(command[command.index("--user") + 1], "10001:10001")
            self.assertEqual(command[command.index("--network") + 1], "none")
            self.assertNotIn("--mount", command)
        for command in runs:
            self.assertEqual(command[command.index("--network") + 1], "none")
            self.assertIn("--read-only", command)
            self.assertEqual(command.count("--mount"), 1)
            self.assertEqual(command[command.index("--mount") + 1], "type=bind,src=" + str(self.project / "data") + ",dst=/data")
            self.assertNotIn("--privileged", command)
            self.assertNotIn("docker.sock", " ".join(command))
        root = runs[0]
        self.assertEqual(root[root.index("--user") + 1], "0:0")
        self.assertEqual([root[i + 1] for i, value in enumerate(root) if value == "--cap-add"], ["CHOWN", "FOWNER"])
        for call in self.state["chowns"]:
            self.assertEqual(len(call), 2)
            self.assertIn(call[1], ("private/LOGIN.txt", "private/STATUS.txt", "data"))
        normal = (self.project / "compose.yaml").read_text()
        for text in ('user: "10001:10001"', '127.0.0.1:8787:8787', 'read_only: true', 'TRAVEL_SECURE_COOKIE: "true"', '- ALL', 'no-new-privileges:true'):
            self.assertIn(text, normal)

    def test_missing_daemon_and_legacy_compose(self):
        self.state["daemon_fail"] = True; self.save()
        self.install(ok=False)
        self.assertFalse((self.project / ".env").exists())
        self.state["daemon_fail"] = False; self.state["legacy"] = True; self.save()
        self.install()

    def test_health_timeout_and_exited_app_do_not_claim_success(self):
        self.state["health"] = "starting"; self.state["clock_step"] = 20; self.save()
        result = self.install(ok=False)
        self.assertIn("120 seconds", result.stderr)
        status = (self.project / "private/STATUS.txt").read_text()
        self.assertNotIn("Local container: healthy", status)
        self.state["containers"]["cid-owned"]["running"] = False
        self.state["clock_step"] = 0; self.state["health"] = "unhealthy"; self.state["up_fail"] = True; self.save()
        self.run_installer(ok=False)
        self.assertNotIn("Local container: healthy", (self.project / "private/STATUS.txt").read_text())

    def test_integrity_or_broad_permissions_refused_without_repair(self):
        original = (self.project / "app.py").read_bytes()
        (self.project / "app.py").write_bytes(original + b"\n")
        self.install(ok=False)
        self.assertFalse(self.state["calls"])
        (self.project / "app.py").write_bytes(original)
        self.state["modes"][str(self.project)] = "770"; self.save()
        self.install(ok=False)
        self.assertEqual(self.state["modes"][str(self.project)], "770")


class DataHelperTests(unittest.TestCase):
    def setUp(self):
        self.work = ROOT / (".qa-data-" + uuid.uuid4().hex)
        self.work.mkdir(mode=0o700)
        self.identity = "a" * 24
        self.seed = b'{"version":1}'
        self.digest = hashlib.sha256(self.seed).hexdigest()

    def tearDown(self):
        shutil.rmtree(self.work)

    def prepare(self, **kwargs):
        return easy_data.prepare(self.work, self.identity, self.digest, self.seed, **kwargs)

    def test_prepare_changes_only_named_paths_and_validate_is_read_only(self):
        with patch.object(easy_data.os, "chown") as chown:
            self.prepare()
            self.assertTrue(all(Path(c.args[0]) in (self.work, self.work / "seed.json", self.work / easy_data.PENDING) for c in chown.call_args_list))
        database = self.work / "travel.sqlite3"
        database.write_bytes(b"user database")
        before = {p.name: p.read_bytes() for p in self.work.iterdir()}
        original_stat = Path.stat
        def metadata(path, *args, **kwargs):
            info = list(original_stat(path, *args, **kwargs))
            if path == self.work: info[0] = stat.S_IFDIR | 0o700
            if path == self.work / "seed.json": info[0] = stat.S_IFREG | 0o600
            return os.stat_result(info)
        with patch.object(Path, "stat", metadata), patch.object(easy_data, "UID", os.getuid()), patch.object(easy_data.os, "chown") as chown, patch.object(easy_data.os, "chmod") as chmod:
            self.prepare(validate_only=True)
            chown.assert_not_called(); chmod.assert_not_called()
        self.assertEqual({p.name: p.read_bytes() for p in self.work.iterdir()}, before)

    def test_resume_named_partial_seed_but_never_overwrite_existing_seed(self):
        (self.work / easy_data.MARKER).write_text(self.identity + "\n")
        (self.work / easy_data.PENDING).write_bytes(b"partial")
        with patch.object(easy_data.os, "chown"):
            self.prepare()
        self.assertEqual((self.work / "seed.json").read_bytes(), self.seed)
        (self.work / "seed.json").write_bytes(b"different")
        with self.assertRaises(ValueError), patch.object(easy_data.os, "chown"):
            self.prepare()
        self.assertEqual((self.work / "seed.json").read_bytes(), b"different")

    def test_unmanaged_links_and_wrong_identity_refused(self):
        marker = self.work / easy_data.MARKER
        marker.write_text("b" * 24 + "\n")
        with self.assertRaises(ValueError): self.prepare()
        marker.write_text(self.identity + "\n")
        outside = self.work.parent / (self.work.name + "-outside")
        outside.write_bytes(b"outside")
        try:
            seed = self.work / "seed.json"
            seed.write_bytes(b"virtual link target")
            original_lstat = Path.lstat
            for kind in ("symlink", "hardlink"):
                def metadata(path, *args, **kwargs):
                    info = list(original_lstat(path, *args, **kwargs))
                    if path == seed:
                        if kind == "symlink": info[0] = stat.S_IFLNK | 0o777
                        else: info[3] = 2
                    return os.stat_result(info)
                with patch.object(Path, "lstat", metadata), self.assertRaises(ValueError): self.prepare()
            seed.unlink()
            originals = self.work / "originals"
            originals.mkdir()
            def directory_link(path, *args, **kwargs):
                info = list(original_lstat(path, *args, **kwargs))
                if path == originals: info[0] = stat.S_IFLNK | 0o777
                return os.stat_result(info)
            with patch.object(Path, "lstat", directory_link), self.assertRaises(ValueError): self.prepare()
            self.assertEqual(outside.read_bytes(), b"outside")
        finally:
            outside.unlink()

    def test_unmanaged_nonempty_and_invalid_input_never_mutated(self):
        file = self.work / "travel.sqlite3"
        file.write_bytes(b"unmanaged")
        with self.assertRaises(ValueError): self.prepare()
        self.assertEqual(list(self.work.iterdir()), [file])
        with self.assertRaises(ValueError): easy_data.prepare(self.work, "../outside", self.digest, self.seed)
        with self.assertRaises(ValueError): easy_data.prepare(self.work, self.identity, "b" * 64, self.seed)


if __name__ == "__main__":
    unittest.main()


class UpgradeOptionTests(InstallerHarness):
    """Password rotation, AI key handling and reason codes added in v4."""

    def env_lines(self):
        return (self.project / ".env").read_text().splitlines()

    def test_unreadable_image_is_reported_with_reason_code(self):
        self.state["image_check"] = "E_APP_UNREADABLE"; self.save()
        result = self.install(ok=False)
        self.assertIn("E_APP_UNREADABLE", result.stderr)
        self.assertFalse(any(c[0] == "compose" and "up" in c for c in self.state["calls"]))

    def test_rotate_password_rewrites_env_and_login_consistently(self):
        self.install()
        old_env, old_login = self.env_lines(), (self.project / "private/LOGIN.txt").read_text()
        result = self.run_installer("--rotate-password")
        new_env, new_login = self.env_lines(), (self.project / "private/LOGIN.txt").read_text()
        self.assertNotEqual(old_env[1], new_env[1])
        self.assertEqual(old_env[2:], new_env[2:])
        password = new_env[1].split("=", 1)[1].strip("'")
        self.assertGreaterEqual(len(password), 48)
        self.assertIn("Password: " + password, new_login)
        self.assertNotEqual(old_login, new_login)
        self.assertIn("Password rotated", result.stdout)
        self.assertNotIn(password, result.stdout + result.stderr)
        self.assertFalse((self.project / ".env.new").exists())
        self.assertEqual(self.state["modes"][str(self.project / ".env.new")], "600")
        self.assertEqual(self.state["modes"][str(self.project / "private/LOGIN.txt.new")], "600")
        self.run_installer()  # plain rerun accepts the rotated pair

    def test_rotate_requires_existing_install_and_not_check(self):
        self.run_installer("--rotate-password", "--host", "travel.example.net", ok=False)
        self.assertFalse((self.project / ".env").exists())
        self.install()
        self.run_installer("--check", "--rotate-password", ok=False)

    def test_set_and_remove_ai_key(self):
        self.install()
        key = "sk-ant-api03-" + "Ab9_-" * 10
        key_file = self.project / "private/ANTHROPIC_API_KEY.txt"
        key_file.write_text("  " + key + "\r\n")
        result = self.run_installer("--set-ai-key")
        self.assertFalse(key_file.exists())
        lines = self.env_lines()
        self.assertEqual(len(lines), 6)
        self.assertEqual(lines[5], "ANTHROPIC_API_KEY='" + key + "'")
        self.assertNotIn(key, result.stdout + result.stderr + (self.project / "private/STATUS.txt").read_text())
        self.assertIn("AI reading: Anthropic API key configured", result.stdout)
        self.run_installer("--rotate-password")
        self.assertEqual(self.env_lines()[5], "ANTHROPIC_API_KEY='" + key + "'")
        self.run_installer()
        self.run_installer("--remove-ai-key")
        self.assertEqual(len(self.env_lines()), 5)

    def test_invalid_ai_key_and_tampered_env_line_refused(self):
        self.install()
        before = (self.project / ".env").read_bytes()
        key_file = self.project / "private/ANTHROPIC_API_KEY.txt"
        for bad in ("sk-wrong-1234567890123456789012345", "sk-ant-short", "sk-ant-" + "a" * 30 + "'; id; '"):
            key_file.write_text(bad)
            result = self.run_installer("--set-ai-key", ok=False)
            self.assertNotIn(bad, result.stdout + result.stderr)
            self.assertEqual((self.project / ".env").read_bytes(), before)
        key_file.unlink()
        self.run_installer("--set-ai-key", ok=False)
        (self.project / ".env").write_bytes(before + b"ANTHROPIC_API_KEY='$(id)'\n")
        self.run_installer(ok=False)


class RegistryAndPermissionTests(InstallerHarness):
    """v4.1: image from ghcr.io, switch-over from the locally built v4 image, File Station permission repair."""

    def test_switch_from_locally_built_v4_container(self):
        self.install()
        identity = (self.project / ".installer/id").read_text().strip()
        self.state["containers"]["cid-owned"]["image"] = "travel-manager-easy-" + identity + ":local"; self.save()
        self.run_installer("--pull")
        self.assertEqual(self.state["containers"]["cid-owned"]["image"], "ghcr.io/zolta-n/travel-manager:latest")

    def test_wrong_image_source_is_refused(self):
        self.install()
        self.state["images"]["ghcr.io/zolta-n/travel-manager:latest"] = "https://github.com/someone/else"; self.save()
        result = self.run_installer(ok=False)
        self.assertIn("expected repository", result.stderr)

    def test_writable_package_files_are_repaired_not_refused(self):
        target = str(self.project / "app.py")
        self.state["modes"][target] = "777"; self.save()
        result = self.install()
        self.assertIn("Repaired permissions of app.py", result.stdout)
        self.assertEqual(self.state["modes"][target], "600")
        self.state["modes"][str(self.project / ".env")] = "644"; self.save()
        self.run_installer(ok=False)  # private config is still never repaired automatically

    def test_check_mode_never_repairs(self):
        target = str(self.project / "app.py")
        self.state["modes"][target] = "777"; self.save()
        self.run_installer("--check", "--host", "travel.example.net", ok=False)
        self.assertEqual(self.state["modes"][target], "777")
