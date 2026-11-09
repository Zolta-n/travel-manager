# Installer details and safe recovery

## What is automated

One manually launched DSM task performs package/path preflight, Docker/Compose/daemon checks, project-specific image build/reuse, strong random credential and `.env` creation, initial seed/folder preparation, Compose start and a bounded local health wait. No host Python, SSH, downloaded installer, shell evaluation, Docker socket mount or privileged container is needed. Python helpers run from the built app image. The initial build fetches only the base image and dependencies declared by Dockerfile/requirements; this is not an offline installation.

It does **not** install Container Manager, create a shared folder, configure DSM/networking, add a certificate, change a reverse proxy, alter router/firewall/DNS/VPN/QuickConnect, expose8787 publicly or configure your iPhone. Supply the actual lowercase DNS authority (optionally a non-default numeric port) using `--host`; this edition does not accept IPv6 authorities. No scheme, path, userinfo, wildcard, comma-separated list, whitespace or shell syntax is accepted. QuickConnect is not inferred as a custom host.

## Root trust boundary

The DSM task must be authorized by an administrator. Uploaded code is executable as root: obtain this complete package through a trusted channel and use an admin-only, non-public folder owned by the account that will privately retrieve credentials. Package markers and SHA-256 checks detect wrong folders, changed/missing files and corruption; **they are not a signature or defense against a malicious administrator replacing the package and manifest together**.

The installer verifies all runtime manifest paths against a fixed allowlist, requires regular single-link files, rejects symlinked app/config/state/data paths, and rejects nonphysical paths, commas, colons and newlines in the app path. Spaces are supported. Package directories and all ancestors must be owned by root or the trusted uploader and must not be group/world writable. Private state/config must already have the expected owner and0600/0700 modes; unsafe files are refused, not automatically repaired. Root writes are scoped to the selected app folder and ordinary Docker operations. No whole-volume/share permission change occurs. Standard Docker build operations of course use Docker's own storage.

**DSM ACL limitation:** POSIX checks are conservative, not an ACL evaluator. A legitimate Synology ACL-managed shared folder may report broad POSIX bits and be refused. Conversely the script cannot independently certify every inherited DSM ACL. The authorized administrator must review actual ACLs and choose a genuinely private path. Do not bypass checks or chmod/chown a whole share/volume. Universal compatibility with DSM extraction ownership, ACL inheritance or File Station access has not been verified.

Only an existing trusted package owner/root may write this tree while root installation runs. Shell checks reduce accidental links and use exclusive creation/private modes; they are not an adversarial race-proof filesystem sandbox against that trusted administrator. The Python helper uses no-follow/exclusive opens where practical. Runtime data is never put into the Docker build context: `.dockerignore` allowlists only app source, dependencies and `web/index.html`.

## Identity, data and permissions

`.installer/id` is a random stable identity, root-only0600 inside0700 state. Both the Compose project and image tag derive from it, so the installer never uses/rebuilds the v1 global image. It checks image labels, both Compose naming conventions, project/service/working-directory/config-file/image ownership, Docker port publications and local TCP listeners before startup. Unrelated containers, named volumes and images are not adopted or removed. A v1 instance using8787 causes refusal; moving/stopping it is a separate administrator decision, not part of installation. There are no `down -v`, pruning, forced lock breaking, backup overwrites or migration commands.

Fresh `data` must be empty. A narrowly scoped host chown to0:0 is allowed only for that empty app-local directory if uploaded by its nonroot owner. The root helper mounts **only `./data`**, uses `--network none`, read-only image, no-new-privileges, cap-drop ALL and only CHOWN/FOWNER. It creates the exact installation marker and validated seed, then assigns only the directory/seed/named pending seed to10001. It never recursively chmods/chowns database files or originals. The nonsecret marker is root-owned0644 inside the private0700 data directory. A partial seed copy can resume only at the installer-reserved pending filename, never over an existing seed.

**Managed reruns skip the root helper once `data` is UID10001.** Validation runs as10001 with no capabilities/network and checks the marker, seed digest, known app entries and link guards without writing data. This matters: root with only CHOWN/FOWNER cannot traverse UID10001's0700 directory or read its0600 seed; the installer does not add DAC_OVERRIDE to get around that. The mandatory nonroot validation also follows a fresh root handoff before Compose starts. Changed/missing seed, unknown entries or unsafe links refuse rather than repair/reseed a live database. Existing SQLite/history/originals stay intact.

The normal service stays10001:10001, cap-drop ALL, read-only root filesystem, bounded tmpfs/resources/logs, no-new-privileges, restart policy and **127.0.0.1:8787 only**. Secure cookies are hardcoded true in Compose; false/weak/unsupported existing `.env` is refused.

## Secrets and private retrieval

The password is generated from256 random bits plus character-class prefixes using `/dev/urandom`; it is never an argument to Docker or printed. `.env` is created0600 as root and parsed as data, never sourced/evaluated. Reruns keep the same host/password and refuse host mismatch or malformed/weak config, instead of overwriting it. Existing unmanaged data, credentials/config/status are not adopted. Generated `private/LOGIN.txt` and nonsecret `private/STATUS.txt` alone are assigned0600 to the original package owner's numeric UID. No broad group/Everyone permission is granted.

Use that same DSM account for File Station. If the folder was extracted as root, or DSM ACLs prevent access, File Station private retrieval is **not guaranteed**. Establish an administrator-approved private retrieval path before relying on the install; do not print the password through a task, email, console log or environment dump. Docker administrators inherently can inspect container environment secrets; this password is not protection against NAS administrators.

Docker build/Compose output is suppressed because even error output can contain interpolated environment secrets. The script stores no raw command log. Task messages/status contain no password. Errors direct the administrator to the Container Manager **Containers** list and private local logs/status; CLI Compose registration as an editable GUI Project is not promised. Do not share environment dumps, backups or seed data.

## Check, rerun and failures

Append `--check` or `--plan` to the same absolute command for a non-mutating report. It performs path/package/config/daemon/Compose/port checks; it does not create state/config, run the helper, start containers, prove an image can build or validate HTTPS/OCR on the NAS. It can report missing Docker/Compose/timeout utilities or an unready daemon. Unsupported system utilities fail safely rather than installing NAS packages.

Normally rerun the **same disabled DSM task manually**. Successful runs reuse the image. Add `--build` only to deliberately rebuild that installation's own image. Build/preparation/start failures leave config/password and managed data for retry; no automatic backup replacement or rollback of other apps occurs. A power interruption during the initial identity/config-file write may leave incomplete private state; malformed partial config safely refuses and requires administrator inspection rather than silently rotating credentials.

An atomic `.install.lock` directory prevents concurrent installers. Normal exits/signals release only the lock they acquired. After a power loss or SIGKILL a stale lock may remain: verify in DSM that no task/install process is running, then have the administrator remove **only the empty `.install.lock` directory in this app folder**. Never clear an active/unknown lock; never delete `.installer`, `.env`, credentials or data to force a rerun.

Health polling has a120-second budget with bounded Docker inspections (small scheduling/command overhead can apply). Failure/timeout is nonzero, retains managed data and does not claim success. Local success means the Dockerfile HTTP health endpoint passed; it does **not** establish external HTTPS, trusted certificate, VPN routing, OCR correctness on the NAS or iPhone readiness. `private/STATUS.txt` explicitly keeps remote access NOT VERIFIED. Container Manager may report a container still starting after timeout; inspect privately and rerun after resolving the cause.

The packaged seed includes the initial conversation itinerary only. Import the user's latest complete v2/v3 JSON backup through the authenticated app to bring over later local rental documents/manual entries/originals. No v1 database is automatically adopted and no later offline upload is invented.

## v4 additions

### Options

| Option | What it does |
|---|---|
| `--build` | Rebuilds this installation's own image (needed after copying in new program files). |
| `--rotate-password` | Generates a new random password, rewrites `.env` and `private/LOGIN.txt` (each written to a `.new` file first, then renamed), and restarts the app. All sessions end. |
| `--set-ai-key` | Reads `private/ANTHROPIC_API_KEY.txt` (only the key, `sk-ant-…`, max 512 bytes, regular file owned by root or the folder owner), stores it as line 6 of the root-only `.env`, deletes the text file and restarts the app. The key is never printed or written to `STATUS.txt`. |
| `--remove-ai-key` | Removes line 6 from `.env`; the app falls back to the local readers. |

These options require an existing installation and cannot be combined with `--check`. `.env` may now have 5 lines (no AI key) or 6 lines (`ANTHROPIC_API_KEY='sk-ant-…'`); anything else is still refused.

### Synology ACL handling

Files extracted through File Station into an ACL-managed share come out as `-rwx------+` (owner only).

- **Image:** the Dockerfile now sets `/app` to root-owned `0755` directories / `0644` files after copying, so host modes never matter. After every build the installer runs a check as UID 10001 inside the image (no network, no mounts) and refuses with `E_APP_UNREADABLE` if any program file is unreadable.
- **Private files the installer creates** (`.installer/`, `.installer/id`, `.env`, `private/LOGIN.txt`, `private/STATUS.txt`): it runs `synoacltool -del` when that tool exists, then sets `0700`/`0600` explicitly. `umask 077` alone is not enough on ACL shares. Files that already exist are still checked, never silently repaired.
- The port check no longer requires `/proc/net/tcp6`, so NAS units with IPv6 disabled are supported.

### Reason codes

Failures print a fixed, non-secret code. Raw Docker or helper output is still never shown.

| Code | Meaning | What to do |
|---|---|---|
| `E_APP_UNREADABLE` | UID 10001 cannot read program files in the image (ACL/permission problem). | Rerun with `--build` using this package's Dockerfile. |
| `E_IMAGE_CHECK_FAILED` | The image check container could not run. | Check Container Manager is running; rerun. |
| `E_SEED_MODE` | `data/seed.json` is not owned by UID 10001 with mode 0600. | Fix ownership/mode of that single file (Container Manager terminal or support); do not chmod the share. |
| `E_DATA_MODE` | `data/` is not owned by UID 10001 with mode 0700. | Same, for the `data` folder only. |
| `E_SEED_DIFFERS` | `data/seed.json` differs from `private/seed.json`. | Restore the original `private/seed.json` from the package you first installed with. |
| `E_SEED_MISSING` | Seed missing while a database exists. | Inspect privately; never reseed a live database. |
| `E_SEED_INTEGRITY` / `E_SEED_INVALID` | `private/seed.json` is damaged or not JSON. | Re-extract the package. |
| `E_SEED_PENDING` | A half-written seed from an interrupted first install. | Inspect `data/` privately. |
| `E_DATA_IDENTITY` | `data/` belongs to another installation. | Use the matching app folder. |
| `E_DATA_UNMANAGED` | `data/` has files but no installation marker. | Use an empty `data/` for a new install. |
| `E_DATA_UNKNOWN_ENTRY` | Unexpected files inside `data/`. | Move them out after checking what they are. |
| `E_DATA_LINK` / `E_DATA_NOT_DIR` | A link or hard link inside `data/`. | Replace with a normal file/folder. |
| `E_PERMISSION` | The helper was denied access (often ACLs on `data/`). | Remove inherited ACLs from `data/` only. |
| `E_MISSING` / `E_IO` / `E_ARGUMENTS` / `E_UNKNOWN` | Missing file, disk/I/O error, or an unrecognised failure. | Check disk space and Container Manager logs; rerun. |
| `E_PRIVATE_MODE` | The installer could not set `0600`/`0700` on its own private file. | Check that root can write the app folder. |

### AI reading at runtime

`ANTHROPIC_API_KEY` is passed to the container through Compose. Reads run as background jobs (one at a time) so long documents never hit Cloudflare's 100-second request limit; the browser polls for the result. The key format is checked at startup; a malformed key disables AI reading instead of failing the app. Outbound access to `api.anthropic.com` uses the container's normal Docker network (no new port is opened; the app stays bound to 127.0.0.1:8787).

## Validation boundaries

See VERIFICATION.md. Source/backend behavior and mocked orchestration are checked here, not a real Synology install. Docker build, dropped-capability kernel behavior, Synology package paths/ACLs, GUI registration/retrieval, reverse proxy/certificate/VPN and iPhone use still require authorized validation on the target NAS.
