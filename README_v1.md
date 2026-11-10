# Travel Manager NAS backend

Start with **[START_HERE.md](START_HERE.md)** for DSM GUI-first setup. This is source plus a private first-import seed, **not a site installed on a NAS**. No actual NAS/DSM/iPhone access or verification was available. Docker is absent in the build workspace, so the image/Compose and production Flask/waitress launch must still be verified on the deployment machine.

## Runtime design

The normal image runs Python 3.12, Flask + waitress, SQLite and installed local Poppler/Tesseract tools. A small stdlib WSGI core owns routing/security so the exact same API can be exercised over real loopback HTTP when Flask/waitress are unavailable. `create_app()` is the Flask factory; `create_application()` is the dependency-free WSGI factory. The production entry point requires Flask/waitress; it never silently falls back to a development server.

Only `/` and `/index.html` are public frontend routes. There is no static-directory catch-all. Source, `.env`, seed, SQLite, attachment files and work directories are not web-accessible. `web/index.html` contains the supplied frontend; the backend never inserts the seed into it. Keep all source/seed/backups private despite the frontend route being public.

Private storage:

```text
data/
  seed.json                    # initial import only, placed here during setup
  travel.sqlite3               # metadata, immutable snapshots, hashed sessions, login attempts
  travel.sqlite3-wal/-shm       # may exist while active; include in a stopped backup
  originals/<2 hex>/<62 hex>    # generated SHA-256 addresses, never uploaded names/paths
```

Each successful PUT stores a new immutable revision and its metadata transactionally. Attachment `data` values are removed from metadata JSON and recorded as immutable file references; metadata-only documents remain supported. Original bytes are written to generated pending filenames, flushed/fsynced and atomically renamed, and directories fsynced **before** the SQLite transaction commits its references. SQLite uses WAL, `synchronous=FULL`, foreign keys and `BEGIN IMMEDIATE` for revision writes. A failed/interrupted write can leave a harmless unreferenced original, never intentionally commit a missing original. No history or original is automatically purged. Disk/power/filesystem reliability and off-NAS backups remain necessary; this is not a substitute for protected NAS storage.

GETs reconstruct data URIs after authentication, verifying SHA-256. Snapshot JSON retains attachment hashes/metadata, not raw attachment bytes. Actual filenames and document IDs never become disk paths. A metadata-only replacement/removal deletes neither the old snapshot nor its originals. Seed import occurs only if the database has no snapshot; editing the seed later does not modify saved data.

One server process/one project should own a data directory. Waitress handles concurrent requests in two threads, optimistic revisions prevent lost updates, and only one extraction runs at a time. Do not run multiple replicas on the same SQLite bind.

## API contract

All JSON responses/errors and frontend responses have `Cache-Control: no-store`. Every request validates the exact Host allowlist. If Origin or `Sec-Fetch-Site` is present, it must be same-origin; **all POST/PUT requests require a matching Origin**. Production accepts HTTPS Origins only, even though the trusted reverse proxy speaks HTTP to loopback. Forwarded host/address/scheme headers are not used. There is no CORS support.

| Method/path | Authentication / CSRF | JSON contract |
| --- | --- | --- |
| `GET /api/session` | Public | `{authenticated:false}` or `{authenticated:true,csrfToken}` |
| `POST /api/login` | Matching Origin, rate-limited | Request `{password}`; response `{authenticated:true,csrfToken}` plus HttpOnly cookie |
| `POST /api/logout` | Session + `X-CSRF-Token` + Origin | `{authenticated:false}`; token revoked and cookie cleared |
| `GET /api/state` | Session | `{state,revision}`; full schema-v2 state with reconstructed originals |
| `GET /api/revision` | Session | `{revision}` only; no original file reads, inexpensive polling |
| `PUT /api/state` | Session + `X-CSRF-Token` + Origin | Request `{state,revision}`; response `{revision}` |
| `POST /api/read` | Session + `X-CSRF-Token` + Origin | Request `{documentId}`; response `{text,method,confidence}`; optional `truncated:true,warning` |
| `GET /api/readers` | Session | Simple health dictionary: `text_email`, `pdfinfo`, `pdf_text`, `pdf_raster`, `image_ocr`, `pillow`, `ocr_language`, `offline`, `max_pages` |
| `GET /api/backup` | Session | Streamed consistent JSON with current `state`, `revision`, `format`, `exportedAt`, and `history:[{revision,createdAt,state},…]` (format v2: history documents carry `hash`; files used only by older revisions are in `originals:{sha256: data URI}`; each file appears once) |

A stale PUT returns **409** `{error,code:"revision_conflict",revision:currentRevision}` without changing current data. The client must reload/reconcile; retrying the same stale body is not a merge. Other errors are JSON `{error,code}` with 400/401/403/404/405/411/413/415/422/429/500/503 as appropriate. Read processing changes **no saved metadata or revision**: the frontend saves extracted results and accepted itinerary changes with its next state PUT.

Reader `confidence` is **0–100**, not 0–1. Methods contain `OCR` for optical extraction so the supplied frontend's confidence review threshold applies. Text/PDF extraction uses confidence 100 for transport/extraction, **not a guarantee that itinerary details are correct**. OCR reports average recognized-word confidence; hybrid PDFs report the minimum OCR page confidence. Fields remain subject to human verification.

`state` is exactly `{version:2,selected,trips,activities,documents,expenses,settings}`. The four collections require stable unique IDs (`tripId`, `activityId`, `documentId`, `expenseId`), at most 10,000 records each. Scalar row metadata is bounded; documents also permit bounded structured `auto` metadata (16 levels, 600,000 serialized characters, 120,000 per scalar string). Reader text caps at 100,000 characters so the frontend can persist extracted source text safely. Frontend validation additionally checks itinerary/business relationships. Original data must be a base64 data URI, never an HTTP URL or a local path. Server-computed byte size, MIME from the URI and SHA-256 replace client-supplied attachment claims.

The **Download server history backup** action is not a public download link: it fetches `/api/backup` on the same origin while signed in. It contains private deleted-history content as well as current originals, and has no history restore/merge UI or automatic purge. The separate **current-state backup** action creates the frontend's migratable v2/v3 snapshot of current records/originals only; that snapshot is supported by the existing restore/import UI, but does not contain server history. See START_HERE for all three backup options and the stopped-directory recovery procedure.

## Authentication/security defaults

- Required `TRAVEL_PASSWORD`: no default; 16–1024 characters, at least three character classes. Use a long unique password-manager value, not a predictable phrase. A random salt and scrypt derive the in-memory password hash; comparison is constant-time.
- Random 256-bit session tokens: only SHA-256 token hashes are persisted server-side. Sessions expire after 12 hours and are all revoked on server restart/password change. A successful re-login rotates/revokes that browser's previous token. CSRF tokens are separate random values, returned through authenticated session/login responses and compared in constant time on state writes, reading and logout.
- Cookie: `HttpOnly; SameSite=Strict; Path=/; Secure` by default, with the `__Host-` prefix in secure mode, no Domain attribute. The app has no default user account, NAS credentials, URL password or bearer token in localStorage.
- Login attempts are reserved transactionally before password hashing: five per source IP in ten minutes, fifty globally. The reverse proxy's source IP is deliberately used instead of blindly trusting forwarded addresses, so users behind that proxy share a rate bucket.
- Exact mandatory `TRAVEL_ALLOWED_HOSTS`, including non-default ports. No wildcard or Host inference. Missing/malformed/disallowed hosts fail. Mutating requests need explicit same-origin Origin and CSRF (except login, which instead has Origin/rate checks). Browsers' cross-site fetch metadata is rejected.
- Security headers: restrictive self-only CSP (inline script/style needed by the standalone frontend), no external reader/CDN script sources, nosniff, frame denial, no-referrer, same-origin resource policy, restricted permissions policy and HSTS in secure mode. No CORS headers. No untrusted forwarded headers.
- 60 MiB request limit; login 8 KiB, reader request 4 KiB; current originals 250 MiB, each 12 MiB, metadata 4 MiB. Python dependencies are exact-version pinned. Base-image and OS package security updates still require deliberate rebuilds/testing.
- Normal container: UID/GID 10001, read-only root filesystem, all capabilities dropped, no-new-privileges, process/memory limits, bounded scratch tmpfs, persistent private data bind, no Docker socket, loopback-only published port. The optional one-time GUI helper runs separately with only narrowly required ownership capabilities against a **fresh app data directory**; it is not part of the normal service.

## Offline extraction and limits

`/api/read` resolves document IDs only against the current saved state's internal original references. It never accepts a URL or a filename to open. All subprocesses use argument arrays; there is no shell, script evaluation or cloud reading service. Email HTML is converted to text with Python's stdlib parser; embedded links/images are not fetched and attachments are not recursively processed. No OCR/model downloads happen at startup or while reading.

- Text/EML: stdlib decoding/MIME parsing, at most 200 MIME parts; attachments ignored.
- PDFs: `pdfinfo` checks encryption/pages/dimensions; encrypted PDFs stop before text/OCR. At most 20 pages. `pdftotext` first; individual scanned/sparse pages in hybrid PDFs use bounded `pdftoppm` and Tesseract English OCR.
- Images: Pillow checks one frame, <=20 million pixels and <=12,000-pixel longest edge, then Tesseract `eng` TSV extraction computes word confidence.
- Each read runs in a separate worker process with ~55-second outer timeout, a 48-second working deadline, per-command timeouts, 768 MiB address-space limit, 16 MiB per-file limit, CPU/FD limits, bounded raster dimensions/output and one OCR thread. The parent kills the process group on outer timeout and removes scratch artifacts. The container's additional memory/process/tmpfs limits apply.
- Output is capped at 100,000 characters, with `truncated:true`, a visible truncation notice in the text and confidence capped at 50. Review the original for anything omitted. PDFs over 20 pages/images over limits are rejected rather than read incompletely.
- `GET /api/readers` checks executable/package presence, not a full self-test of every possible document or installed language. The shipped image explicitly installs `tesseract-ocr-eng`.

The container root is read-only and `/tmp` is a bounded compatibility tmpfs, but configured reader scratch is `/run/travel-reader` (also bounded and private), not persistent originals. Development/test scratch remains under this project, never a system temporary directory.

## Local verification with no installation

If Flask/waitress are missing, **do not install anything just to run the stdlib tests**:

```sh
cd travel_manager_nas
PYTHONDONTWRITEBYTECODE=1 python -m unittest discover -s tests -v
```

Tests generate/delete their own `.qa-<random>` directories **under this project**. They use real loopback HTTP, SQLite, transactional conflict tests, original retention, actual Poppler/Tesseract extraction when installed, and a Flask test-client smoke test when Flask is installed. Test passwords are freshly generated, not packaged credentials. Unavailable optional tooling is explicitly skipped/reported.

To run an isolated local browser test without Flask/waitress, use a new local-only data folder and an ephemeral unique password of your own. This shell example asks for it rather than shipping a working value:

```sh
cd travel_manager_nas
read -r -s -p 'Choose a unique LOCAL TEST password: ' TRAVEL_PASSWORD; echo
export TRAVEL_PASSWORD
export TRAVEL_ALLOWED_HOSTS='127.0.0.1:8787'
export TRAVEL_SECURE_COOKIE=false
export TRAVEL_DATA_DIR="$PWD/.local-test-data"
export TRAVEL_SEED_PATH="$PWD/private/seed.json"
export TRAVEL_WORK_DIR="$PWD/.local-reader-work"
PYTHONDONTWRITEBYTECODE=1 python server.py --stdlib --host 127.0.0.1 --port 8787
```

The `read -s` example needs Bash. For another shell, set the same environment variables securely. The password must still pass strength validation. Open **`http://127.0.0.1:8787` on that same trusted computer only**. The `--stdlib` launcher refuses non-loopback bindings. The false cookie setting is **not a NAS/LAN deployment option**, and local plaintext test credentials should not be reused in production. Do not publish the stdlib server through a reverse proxy, router or VPN. Stop it when finished, remove its private test data only if no longer needed, and unset the environment values. No `.env` is automatically loaded by Python; Compose reads `.env` during deployment.

For the normal production runtime, build/run with Container Manager as described in START_HERE, or (if you are already comfortable with Docker):

```sh
# Only after .env, the fresh data/seed.json and ownership have been prepared:
docker compose -f compose.yaml build
docker compose -f compose.yaml up -d
```

The first build needs internet for dependencies. Runtime reading does not. No actual Docker build, NAS installation, reverse-proxy/VPN setup or iPhone Safari check has been claimed from this workspace.

## Workspace verification result (2026-11-06)

`PYTHONDONTWRITEBYTECODE=1 python -m unittest discover -s tests -v`: **33 tests run, 32 passed, 1 explicitly skipped, 0 failures/errors** (33.017 seconds on the final run). The skipped test is the Flask test-client smoke test because Flask is absent; waitress and Docker are also absent and were not installed. The identical WSGI core passed authentication/security/state/backup tests over actual loopback HTTP. Actual installed Poppler/Tesseract passed text PDF, scanned-PDF OCR, image OCR, 21-page rejection and encrypted-PDF rejection; text and EML extraction also passed. Storage tests covered concurrent revisions, failure rollback, atomic-original interruption, deleted-original retention and consistent export during a save. Permission-helper scope/refusal tests passed with mocked ownership operations; the workspace forbids symlink creation, so the symlink guard was tested through a mocked filesystem predicate.

Python AST parsing, Compose YAML parsing/security invariants, actual-seed-only original checks and generated test-scratch cleanup also passed. Docker image build, production dependency imports/launch, DSM helper execution/ACL behavior, physical NAS deployment, reverse-proxy/VPN/certificate setup and actual iPhone Safari remain deployment verification tasks. No test credentials or QA fixtures were added to the private seed or package data.

## Source files

- `app.py`: authenticated same-origin WSGI API and Flask factory.
- `store.py`: schema validation, atomic originals, immutable SQLite revisions/backup.
- `extractor.py`, `reader_worker.py`: bounded local extraction/health/process isolation.
- `server.py`: waitress production entry point; explicit loopback-only stdlib option.
- `initialize_data.py`, `compose.initialize.yaml`: one-time narrowly scoped GUI permissions setup for new data only.
- `Dockerfile`, `compose.yaml`, `.env.example`, `.dockerignore`: deployment configuration; no embedded secrets.
- `tests/test_backend.py`: stdlib unittest suite, optional Flask client, actual offline reader tests.
- `private/seed.json`: confidential actual v2 first-import state, with the original itinerary data URI. Excluded from image build, not served.
- `web/index.html`: supplied frontend; keep API paths same-origin.
