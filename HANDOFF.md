# Travel Manager — handoff for refactor/upgrade

## What this is
Self-hosted travel itinerary app (Flask + waitress, SQLite, server-side PDF/image OCR via poppler + tesseract), packaged as `Travel_Manager_NAS_Easy_Setup_v2`. Single-user, password login, no username. Frontend is one file: `web/index.html`.

## Where it runs (live, working since 2026-11-08)
- **NAS:** Synology DS225+ (x86-64, DSM 7.2, 2 GB RAM), LAN IP 192.168.50.70, hostname Synology_Zoli.
- **App folder:** `/volume1/travelapp/Travel_Manager_NAS_Easy_Setup_v2/` (shared folder `travelapp`, owner Zoli_admin UID 1026).
- **Container:** Compose project `tme85b2b3fe40b6318511d1469e`, image `travel-manager-easy-85b2b3fe40b6318511d1469e:local`, runs as UID 10001, read-only rootfs, bound to **127.0.0.1:8787 only**.
- **Public access:** Cloudflare Tunnel (cloudflared container, project `cloudflare` in `/volume1/docker/cloud flare`, now `network_mode: host`) → published application route `travel.businessintels.com` → `http://localhost:8787`. Owner is behind double NAT in China; no port forwarding is possible, the tunnel is the only route in.
- **Install method:** `install.sh` run once as root from DSM Task Scheduler (disabled task, run manually). No SSH used.
- Other containers on the NAS: feed-platform, reporting, watchtower, cloudflare. Port 8000 is used by the reporting app.

## Problems hit during install — fix these in the code
1. **Synology ACL inheritance breaks the image.** Files extracted via File Station into an ACL'd shared folder come out `-rwx------+` (owner only). Docker `COPY` preserved that, so in the image `/app/*.py` were `0700 root` and UID 10001 could not read its own code → "Managed data validation refused". Fixed manually with `synoacltool -del` + `chmod 644`.
   → **Fix in Dockerfile:** `COPY --chmod=0644` (and `0755` for dirs) or an explicit `chmod` step, so host permissions never matter.
2. **Installer's own private files inherit ACL too.** `.installer/id`, `.env`, `private/LOGIN.txt` and `private/STATUS.txt` were created under the inherited ACL and showed mode 700, so every rerun refused with "Existing private files must already have mode 0600". Fixed manually with `synoacltool -del` + `chmod 600`.
   → **Fix in install.sh:** after creating each private file, run `synoacltool -del` when present and `chmod 600` explicitly (`umask 077` is not enough on ACL shares). Also `chmod 600` LOGIN.txt; currently it is only chowned.
3. **Error messages too vague.** "Managed data validation refused" hid a simple permission error. Add a non-secret reason code (e.g. `E_APP_UNREADABLE`, `E_SEED_MODE`) to the output without leaking data.
4. **Docs mismatch:** START_HERE uses the example path `travel_manager_nas_easy/`, but the ZIP extracts to `Travel_Manager_NAS_Easy_Setup_v2/`.
5. **No way to change the password** other than editing root-only `.env` plus LOGIN.txt by hand. The installer requires at least 48 characters, charset `[A-Za-z0-9!_-]` and 3 character classes; the app itself only requires 16. Consider an `install.sh --rotate-password` option.

## Constraints for the refactor
- **Don't break the live data.** `data/` (owner 10001, mode 700) holds `.easy-install-id`, `seed.json`, and later `travel.sqlite3` plus `originals/`. Any schema change needs a migration. The installer refuses to adopt or repair data, by design.
- Keep the security posture: non-root, read-only rootfs, cap-drop ALL, loopback-only port, secure cookies, `TRAVEL_ALLOWED_HOSTS=travel.businessintels.com`.
- An upgrade must be deployable the same way: copy the new files into the app folder, then rerun the install task with `--build`. The password, host and data must survive. Keep `PACKAGE.sha256` in sync with the files it lists (the installer verifies it).
- Note that `install.sh` must stay POSIX `sh` (DSM BusyBox/ash compatible).
- `private/seed.json` contains the owner's real itinerary. Keep it out of any public repo.

## Owner
Zoltan. Non-developer for this project: prefers DSM GUI steps (Task Scheduler, File Station, Container Manager) over SSH, with exact copy-paste commands.


## v4 refactor (2026-11-08) — what changed
- **AI reading:** `ai_reader.py` sends the original (PDF / photo / email text) to Claude (`claude-opus-5-5`, structured JSON output, server-side refusal fallback) and gets one segment per booking leg. Every date, time and IANA zone is then re-validated with `zoneinfo` (DST gaps/folds, printed UTC offsets, end-before-start, implausible durations, inferred years). Only fully clean segments with a clear trip match are auto-added; the rest go to review with reasons. Reads run as background jobs (`POST /api/analyze`, `GET /api/analyze/<job>`) because Cloudflare cuts requests at 100 s. Local OCR/rules remain as the fallback.
- **Frontend:** the three monkey-patched script layers in `web/index.html` were merged into one sectioned script. Dead offline-HTML code was removed. New: day-grouped itinerary with zone labels and durations, itinerary check, review queue with per-item add/dismiss, "Create trip from this", AI on/off and home time-zone settings.
- **Backend:** `app.py` routing is a table; `Store.metadata()` added; `extractor.py` has one shared worker runner plus `prepare_ai_image` (EXIF rotation, size bound) in the sandboxed worker.
- **Handoff items 1–5 fixed:** Dockerfile chmod step + image readability check; `synoacltool -del` + explicit chmod on every private file the installer creates (LOGIN.txt now chmod 600); `E_*` reason codes; START_HERE uses the real folder name; `--rotate-password`. Also `--set-ai-key` / `--remove-ai-key`, IPv6-optional port check.
- **Data:** state format unchanged (version 2); new settings keys `aiReading`, `homeZone` are plain scalars; `documents[].auto` holds the reading result. No migration needed.
- `.env` may now have a 6th line `ANTHROPIC_API_KEY='sk-ant-…'`. PACKAGE.sha256 lists 16 files (added `ai_reader.py`).


## v4.1 (2026-11-09) — sync, sessions, GitHub deployment
- Reported problems: edits from the iPhone not reaching other devices, repeated sign-ins, a PDF stuck on "Reading…".
- Causes found: sessions deleted on every restart and only 12 h long; one shared sign-in limit for all devices (all traffic arrives from the tunnel); a save conflict stopped a device from saving; unsaved edits lived only in the tab; "Reading…" was saved to the server and AI results were lost when the tab closed.
- Fixes: see `VERIFICATION.md` (v4.1) and `CLAUDE.md`. Code now lives in GitHub `Zolta-n/travel-manager`; deployment via GitHub Actions → ghcr.io → Watchtower (`UPDATE_WORKFLOW.md`).
- Cloudflare zone `businessintels.com`: ECH turned off via API (the owner's office network broke ECH connections to all subdomains). HTTP/3 was turned off during diagnosis.
