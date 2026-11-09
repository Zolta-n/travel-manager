# Travel Manager — notes for Claude sessions

Private travel itinerary app for one owner (Zoltan), running on a Synology DS225+ behind a Cloudflare Tunnel at `https://travel.businessintels.com`. Read `HANDOFF.md` for the NAS details and history, `UPDATE_WORKFLOW.md` for deployment.

## Hard rules
- **Merging to `main` deploys to the live NAS** (GitHub Actions → ghcr.io → Watchtower). Work on a branch; open a pull request; never push to `main` directly.
- **Never commit** `private/seed.json`, `.env`, `data/`, keys or itinerary content. `.gitignore` covers them; check `git status` before committing.
- **State format is version 2.** Any change to stored data needs a migration and must keep existing NAS data loadable. `documents[].auto` is free-form reading metadata.
- Keep the security posture: non-root (UID 10001), read-only root fs, `cap_drop: ALL`, port bound to `127.0.0.1` only, secure cookies, CSRF on writes, no secrets in logs.
- `install.sh` must stay POSIX `sh` (DSM BusyBox/ash). Its 16-file allowlist must match `tools/build_nas_package.py`; run that script after changing any package file so `PACKAGE.sha256` is current (CI checks it).
- The owner prefers DSM GUI steps (File Station, Task Scheduler, Container Manager) with exact copy-paste commands; no SSH.

## Layout
- `app.py` — WSGI app, route table, sessions (30-day sliding, survive restarts), per-device sign-in limits (CF-Connecting-IP behind the local tunnel), request log.
- `store.py` — SQLite revisions + content-addressed originals; `ai_results` keeps finished AI readings; `meta` holds the password fingerprint.
- `ai_reader.py` — Claude extraction (structured output) + deterministic date/zone checks; background jobs.
- `extractor.py`, `reader_worker.py` — sandboxed local PDF/OCR/email reading and photo preparation.
- `web/index.html` — single-file frontend; one sectioned script. Sync: lightweight state (`?originals=0`), three-way merge on conflict, unsaved edits kept in IndexedDB.
- `install.sh`, `easy_data.py`, `compose.yaml` — NAS installer (pulls `ghcr.io/zolta-n/travel-manager:latest`).

## Tests
`python -m unittest discover -s tests` (installer tests use Docker/root mocks). For UI changes, also drive the page with Playwright against a local server with a fake Claude (see `tests/test_ai.py` for the fake client pattern).
