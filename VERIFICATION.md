# Travel Manager NAS — verification and limits

## v4 verification (2026-11-08)

- **Unit and integration tests:** `python -m unittest discover -s tests` → 77 tests, all passed, 3 skipped (Tesseract OCR not installed in the development sandbox). New: `tests/test_ai.py` (16 tests: overnight cross-zone flights and UTC instants, missing "+1 day", DST gap and repeated hour, invalid zones with printed-offset fallback, offset mismatch, inferred years, trip-match sanity, de-duplication, costs, request schema, key validation, refusal handling, end-to-end background job through the real WSGI app, EXIF-rotated phone photo preparation) and `UpgradeOptionTests` (password rotation, AI key set/remove/invalid/tampered, `E_APP_UNREADABLE` and `E_SEED_MODE` reason codes).
- **Installer:** `sh -n install.sh` (dash) passed. Installer tests still use Docker/root mocks.
- **Real Docker image build:** built with Docker 29 from this Dockerfile with every `.py` file at host mode `0700` and `web/index.html` at `0600` (reproducing the Synology ACL problem). In the image all program files were `root 0644`; the installer's UID-10001 readability check printed `OK`; the backend and AI tests passed inside the image on Python 3.12 with the pinned `requirements.txt`. The container started read-only, as UID 10001 with all capabilities dropped, and served `/api/session` and the page. *Limit:* the sandbox's network policy blocked `deb.debian.org`, so that test build skipped the `poppler-utils`/`tesseract` apt step; the production Dockerfile still installs them unchanged.
- **Browser end-to-end (Playwright/Chromium):** real server, Claude replaced by canned answers. Upload of a two-leg e-ticket PDF, a hotel confirmation and a booking for a date with no trip: both legs auto-added with correct zones and a 14 h 25 min duration, the receipt total became a cost, the uncertain hotel went to review, "Create trip from this" built the new trip and added the stay, the itinerary check flagged nights without accommodation, re-reading the ticket created no duplicates, saves reached the server, no browser console errors.
- **Not verified here:** a live call to the Anthropic API (no key was available in the build environment), reachability of `api.anthropic.com` from the NAS in China, the DSM GUI flow on the DS225+, `synoacltool` behaviour, iPhone Safari.

## Earlier verification (v2 installer, kept for history)

## New installer verification (2026-11-06)

- `/bin/sh -n install.sh`: passed.
- Isolated installer/helper suite: 23 cases in the final packaging run (418.324 seconds). 22 passed; one test fixture encountered a workspace file-recreation error while preparing virtual hardlink metadata, before exercising production behavior. The fixture was corrected to reuse an existing harmless file without unlink/recreation. That corrected case then passed independently in 14.657 seconds. The cases have passed across these runs; no subsequent clean full-suite run is claimed. Production installer code was not changed for this fixture correction.
- Coverage includes first install, stable identity/image, rerun without root-helper invocation, data/secret retention, failed build/preparation/start recovery, non-mutating check, strict host/config checks, offline narrow helper commands, ownership/image/name/port collisions, lock handling, bounded health failure, source integrity, virtual symlink/hardlink guards, path traversal and suppressed secret-bearing subprocess errors.
- These are **Docker/root/subprocess and filesystem-metadata mocks**, not Docker integration tests. This workspace forbids creating symlinks/hardlinks and does not persist chmod semantics. Link/mode cases explicitly inject metadata doubles; they do NOT establish real kernel link/permission/capability enforcement. The test-only copied script injects command doubles; the production installer contains no test-mode/root-check bypass.
- The unchanged backend suite was independently rerun on the easy clone:33 cases,32 passed,1 skipped because Flask was absent. Original HTTP/storage, native PDF/image/scanned-PDF OCR checks were retained.
- Eight baseline files were byte-compared with v1 and are unchanged: `app.py`, `store.py`, `extractor.py`, `reader_worker.py`, `server.py`, `initialize_data.py`, `web/index.html`, `private/seed.json`. v1 itself was not edited.
- Runtime `PACKAGE.sha256` was regenerated after the final production `.dockerignore` adjustment. The separate archive-wide FILES.sha256 is for packaging, not the installer's runtime allowlist.

**Not verified:** any Docker image build/run, actual dropped-capability helper behavior, Synology DSM/Container Manager package paths or ACLs, private File Station retrieval, GUI Project registration, reverse proxy/HTTPS/certificate/VPN, iPhone or physical NAS. No live NAS installation was attempted. `--check` cannot prove the build will work. Local health is never remote-ready status.

Use START_HERE.md for the new three-operation setup and INSTALLER_DETAILS.md for exact refusal/recovery behavior. The following is preserved historical v1 evidence, not a claim of new NAS/device verification.

## Historical v1: implemented and exercised in an isolated development environment

- Password sign-in, session cookies, CSRF-protected writes, sign-out and clearing private records from the visible client after sign-out.
- NAS-backed records with automatically saved revisions. Two signed-in browser pages see the same saved trips and originals.
- Concurrent-device edits produce a revision conflict rather than silently overwriting. The losing device keeps its unsaved work and offers backup/reload.
- Forced reload is blocked during saving/document reading. New edits during a refresh prevent stale state from being installed.
- Uploaded PDF confirmation → local NAS text extraction → matching existing trip → automatic rental-car itinerary entry. Dates, local times, reference and original document link were checked.
- Uploaded PNG confirmation → real local Tesseract English OCR → booking recognition and source linkage. The same reservation was linked without duplicating the existing itinerary entry.
- Full legacy-format JSON migration retains manually entered notes and original attachments. Unprocessed supported files are processed after import.
- Excel workbook export, full current-state JSON backup, selected-trip printable PDF rendering and a narrow mobile viewport.
- Editable trip/itinerary/document/cost workflows retained from the preceding version. Explicit review queues retained for ambiguous/unsupported extraction and trip matching.
- Public HTML starts with empty travel collections; the private seed/originals are not embedded in the public app shell.
- Frontend HTML validation and JavaScript syntax checks passed. Desktop and narrow-screen layouts were visually inspected.

Reading and matching use deterministic extraction rules plus local PDF/OCR tools, not a general AI agent. Test confirmations were synthetic and clearly marked, and are not included in the initial user database. The initial database contains the user-provided flight itinerary and original source plus the previously labeled empty private-trip example. No missing travel costs were inferred.

## Review and regression work

An independent code review found reload/active-save ordering and replaced-document reading races. These were corrected; the final behavior preserves unsaved work, discards superseded extraction, and respects Stop reading. Polling first checks only the revision, avoiding repeated download of unchanged original documents. A separate security-oriented source review reported no high-confidence vulnerabilities in its reviewed scope; this is not a security certification or penetration test.

Backend automated tests are included in `tests/`: 33 cases ran, 32 passed and 1 was skipped because the production Flask wrapper was unavailable. Actual HTTP, PDF, encrypted-PDF rejection, scanned-PDF OCR and image OCR were exercised. See README for the local command and production dependency requirements. The server can be exercised on isolated loopback with its development adapter; production uses the supplied container/dependencies instead.

## Configured but NOT verified on your equipment

- Actual DS225+ model, installed DSM, Container Manager availability, RAM, filesystem ACLs and storage capacity.
- The actual Docker image build and production Flask/Waitress runtime: Docker and those production Python libraries were not available in the development environment. The core server and native PDF/OCR programs were tested through the isolated development adapter.
- DSM reverse proxy, DNS, trusted HTTPS certificate, VPN, firewall and remote connectivity.
- Physical iPhone/Safari file/photo uploads, Home Screen behavior, long-document memory use and print/share PDF workflow. Chromium mobile-size emulation is not an iPhone device test.
- Your additional rental confirmation, which was uploaded inside your downloaded tool rather than supplied to this conversation. Import your latest full backup to carry it across and test that exact layout.

## Unsupported / deliberately out of scope

- Automatic public deployment, router/VPN setup or access to your NAS from this conversation.
- Automatic Outlook mailbox access, MSG decoding, live ticket/booking verification, payments or cancellations.
- Universal arbitrary-language/layout recognition. English OCR is supported; unclear, multi-leg or protected sources may require review or an authorized unprotected export.
- Multiple users/roles, application MFA, separate business/private access permissions, secure erasure/purge of historical originals, or one-click restoration of the all-history archive.
- Offline edit synchronization. If disconnected, unsaved edits remain in that open page, not safely synchronized in the background.
- Encryption of downloaded backups by this application. Apply NAS/storage and backup protection separately.

## Safe completion boundary

The installable application package is built and locally tested; it has **not been installed on your NAS**. Complete START_HERE.md and ACCEPTANCE_CHECKLIST.md before trusting it with additional sensitive documents or using it while travelling. Keep earlier versions and backups until migration is verified.
