# Travel Manager NAS — easy installer edition

Start with **[START_HERE.md](START_HERE.md)**: upload/extract, run one disabled DSM root task manually, read private credentials/status. No host Python or additional NAS packages are installed by the script.

**v4 (Nov 2026):** optional AI document reading with Claude (`ai_reader.py`), NAS-side validation of every date / time / IANA time zone, multi-leg bookings split into one item per leg, an itinerary check (overlaps, tight connections, nights without accommodation), a rewritten single-layer frontend script, `--rotate-password` / `--set-ai-key` / `--remove-ai-key`, Synology ACL fixes and installer reason codes. Data format unchanged (state version 2). Normal service remains non-root, read-only, capability-dropped, secure-cookie enabled and localhost-only.

- `install.sh`: rerunnable POSIX shell installer; `--check`/`--plan` is non-mutating; `--build` explicitly rebuilds only this installation's image.
- `ai_reader.py`: Claude request/response schema, deterministic date/time-zone checks, background read jobs.
- `easy_data.py`: image-contained preparation/validation helper; no Docker socket or network access.
- `INSTALLER_DETAILS.md`: exact trust boundary, refusal/recovery behavior and remaining manual prerequisites.
- `VERIFICATION.md`: verification results and real-world gaps.
- `tests/test_easy_installer.py`: isolated Docker/root/metadata mocks; no real system changes.
- `ADVANCED_SETUP_v1.md`, `README_v1.md`, `ACCEPTANCE_CHECKLIST.md`, `compose.initialize.yaml`: preserved historical v1 references, **not** instructions to run two projects for this installer. Follow START_HERE instead.

This is a new install using the packaged itinerary seed, not an automatic migration or a copy of any additional rental document. Keep this folder/private seed/backups out of public web roots. v1 remains unchanged.
