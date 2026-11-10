# Start here: your private Travel Manager on Synology

**This is a deployable project, not an already installed website.** No connection to your NAS was made. Your NAS is thought to be a DS225+, but its model, available RAM, DSM version and Container Manager support have not been verified. Actual iPhone Safari has not been tested. Initial setup needs a DSM administrator and a trusted HTTPS address or VPN that you configure. No NAS administrator credentials or working app password are supplied.

Once set up, normal use is in a browser: sign in, add travel documents, review the extracted details and let changes save to the NAS. The app password is separate from your DSM password. Do not reuse either password.

## 1. Put the project in one new, private folder

1. In DSM **Package Center**, install/open **Container Manager** if your NAS supports it. DSM labels can differ between releases.
2. In **File Station**, create a NEW folder just for this app in a private shared folder. An **example only** is `docker/travel-manager` on your own volume. Do not use the shared folder itself, a whole volume, a Web Station folder or a publicly shared folder as the project/data directory.
3. Upload/unzip the complete project there. You should see `compose.yaml`, `Dockerfile`, `web/index.html`, `private/seed.json` and the Python files. Keep the `private` folder private: its seed contains your real travel records and original attachment.
4. Create an empty subfolder named `data` if it is absent. With File Station, **copy** `private/seed.json` to `data/seed.json`. The new `data` folder must contain only that one file before the one-time permissions step. Do not copy QA documents or old databases into it.
5. Restrict the project folder and `.env` to the administrator(s) who should manage the app. Do not enable File Station sharing links. The app serves only the frontend and authenticated APIs; it never serves source, seed or data folders.

The seed is used **only when the SQLite database has no saved revision**. Later starts never replace saved NAS data with the seed. The original seed remains a private initial import, not a live database.

## 2. Set two required values — there is no default password

Copy `.env.example` to a file named **`.env`**, next to `compose.yaml`. You can use DSM Text Editor or prepare this file locally and upload it. File Station may need “show hidden files” enabled. Do not use a rich-text editor.

Set:

```dotenv
TRAVEL_PASSWORD='PASTE_YOUR_OWN_UNIQUE_PASSWORD_HERE'
TRAVEL_ALLOWED_HOSTS=YOUR_REAL_HTTPS_HOSTNAME_HERE
TRAVEL_SECURE_COOKIE=true
```

**Both uppercase values above are placeholders, not deployment credentials or an actual hostname.** Replace them. Use a password manager to generate a unique password of at least 16 characters, using at least three of lowercase, uppercase, digits and symbols. Do not use the literal placeholder. Single quotes preserve `$` and `#` in Compose's `.env` format; if your chosen password contains a single quote, generate another strong password or follow Docker Compose quoting rules.

`TRAVEL_ALLOWED_HOSTS` is the exact hostname you will actually type in the HTTPS browser address, without `https://` or a path. If HTTPS uses a non-default port, include `:port`. You may comma-separate multiple exact authorities; wildcards are not supported. This is an allowlist, not a DNS registration service. Your hostname, DNS and trusted certificate must already be set up by you.

Keep `TRAVEL_SECURE_COOKIE=true` for NAS, LAN and remote use. Direct plain HTTP cannot sign in with this secure setting. **Do not change it to false to work around NAS certificate or proxy problems.** The false setting is exclusively for isolated loopback development on a trusted computer, described in `README.md`.

Never paste `.env` into a chat, support ticket, public repository or screenshot. It is deliberately excluded from the image build.

## 3. Prepare only the NEW app data folder (GUI-first)

The running app uses numeric UID/GID **10001:10001**, not DSM admin/root. A newly uploaded folder may not initially allow that user to write. A supplied one-time helper handles this without changing a whole volume or granting Everyone access.

1. In **Container Manager → Project → Create**, use a temporary project name such as `travel-manager-prepare` and select this project's folder as its path.
2. Choose the option to upload/select a Compose file, and use **`compose.initialize.yaml`**. If your DSM dialog instead asks you to paste YAML into `docker-compose.yml`, paste that file's exact contents. Do not replace `compose.yaml` with the helper contents.
3. Build and run this helper project. First build needs internet access to download the Python image, pinned Python dependencies and Linux OCR/PDF packages. No travel document is sent to a reading service. Your seed, data and `.env` are excluded from the Docker build context.
4. In its container log, look for **“Only the new app data folder and its seed.json were prepared…”** and an **exit code of 0**. “Exited” is expected: this helper is not a web server.
5. Remove the helper project/container when finished, **without deleting the project folder or data**. Then create the normal project below. If DSM offers a “delete project files/data” checkbox, leave it off.

The helper briefly runs as container root with only the CHOWN/FOWNER capabilities, no network, and only the new `./data` directory mounted. It refuses a folder with anything other than `seed.json`, refuses a symlink and never recursively changes ownership. It makes just the new data folder and that seed readable/writable by UID 10001. It does **not** change a shared folder, volume, DSM account or Docker socket. Never point this helper at an existing database or another folder. The normal server never runs as root and has all capabilities dropped.

If your NAS permissions/ACLs still deny access, stop and have its administrator check **only this new app data directory**. Do not recursively change permissions on `/volume1`, `/volume2`, `docker`, a home folder or any shared-folder root. Do not grant Everyone access to your documents.

## 4. Create and start the normal project

1. In **Container Manager → Project → Create**, name the normal project (for example `travel-manager`) and select the same project folder.
2. Upload/select **`compose.yaml`**, or paste its exact contents into the GUI's Compose editor. DSM may call the project file `docker-compose.yml`; that is fine. Keep the `.env` in the project path.
3. Build and start the project. The already-built image can be reused/cached. If your DSM offers separate Build and Start actions, use them in that order. Do not select host networking, privileged mode or a Docker socket mount.
4. Check that the normal container stays **running** and becomes **healthy**. If it exits, check the log: missing/weak password, a missing allowlist, wrong folder ownership or missing `data/seed.json` are common first-start causes. No automatic public runtime installation is attempted.

The supplied port mapping is **`127.0.0.1:8787:8787`**. This is the NAS's own loopback address, not your phone's address. It deliberately does not expose the backend to your entire LAN or internet.

## 5. Provide HTTPS and your own remote-access route

The simplest intended arrangement is:

**Browser/iPhone → your trusted HTTPS hostname → DSM reverse proxy → NAS `127.0.0.1:8787`.**

On suitable DSM releases, reverse proxy settings are under **Control Panel → Login Portal → Advanced → Reverse Proxy** (older versions use a different location):

- Source protocol: **HTTPS**. Source hostname: **your actual chosen hostname**, not a value invented by this project. Source port: your configured HTTPS port, usually 443.
- Destination protocol: **HTTP**. Destination hostname: **`127.0.0.1`**. Destination port: **`8787`**.
- Preserve the original **Host** header. If the proxy rewrites it, use its request custom-header option to set **Host** to your actual external hostname (including a non-default port if applicable). That exact authority must appear in `TRAVEL_ALLOWED_HOSTS`.
- Use a valid, trusted certificate assigned to that source hostname in DSM's certificate settings. The backend intentionally ignores `X-Forwarded-Host`, `X-Forwarded-For` and `X-Forwarded-Proto`; adding them will not fix a mismatched Host.

DNS resolution, certificates, any router/firewall rules and a VPN are **your setup tasks**. A private VPN plus the HTTPS hostname is the recommended remote-access route. This project does not configure Synology QuickConnect, DDNS, your router or any VPN, and QuickConnect is not assumed to automatically publish this custom app. **Do not forward port 8787 or DSM administration ports to the public internet.** Even with a VPN, keep HTTPS and secure cookies enabled.

Open **your own HTTPS address** on a trusted computer first, then on your iPhone connected to the permitted network/VPN. Sign in with the app password from your `.env`. Optionally use Safari's “Add to Home Screen.” You must verify certificate trust, uploads, reading and saving on your actual phone; no real iPhone test has been performed here.

### Optional LAN binding — advanced, not the default

If a separate, trusted reverse-proxy machine truly needs backend access over the LAN, explicitly replace the port mapping with `YOUR_NAS_LAN_IP:8787:8787` (replace that placeholder with your NAS's actual LAN address). Restrict the DSM firewall to **only that proxy's IP**, keep secure cookies true, and use HTTPS at the browser-facing proxy. Do not bind `0.0.0.0` casually, expose 8787 through a router, or disable cookies' Secure flag for direct LAN HTTP. If you do not need a separate proxy machine, leave the supplied loopback mapping unchanged.

## 6. First-use check

- Sign in. Your original v2 records and original itinerary image should appear on the first database only.
- Add one non-sensitive test confirmation, wait for “Saved on NAS,” then reload the page. It should still be there.
- Try “Read locally” on a readable text/PDF/image, review what was extracted, and verify dates, confirmation codes, locations and prices against the original. English OCR is fallible, not a booking authority.
- Sign out. Private API data should no longer be accessible. The public app shell is intentionally not a private-data backup.
- From another device, confirm that conflicting saves ask you to reload/reconcile rather than silently overwrite newer data.

If a login fails repeatedly, wait 10 minutes. The server limits sign-ins, and reverse-proxy users share the proxy's IP rate bucket. If you need to change the password, edit only `.env` and recreate/restart the normal project so its environment is refreshed. Restarting revokes existing sessions.

## Backups and safe updates

**Deleting a document or trip is not a privacy purge.** Every successful save retains a revision and its original files. There is deliberately no automatic purge, version-restore UI or permanent-delete UI. History consumes disk beyond the current-state 40 MiB limit; monitor NAS free space. A browser's current-state workbook export is not a substitute for a full NAS backup.

Three distinct backup choices:

1. **Current-state backup (browser):** the frontend's current-state backup action downloads a migratable v2/v3 travel-state snapshot with **current** records and their originals. Its existing restore/import UI can bring that current state back. It does **not** include previously deleted records, server revision history or server sessions. Save this before a bulk import or change.
2. **Download server history backup:** this separate frontend action fetches the authenticated `/api/backup` endpoint. It exports the current full state **and every historical revision**; each original file is included once (format v2: history states refer to files by `hash`, files used only by older revisions are in `originals`), not sessions/passwords. This is **not** the current-state backup button and has **no automatic history-restore UI**. Keep the downloaded JSON private; it can grow large. The database is held at a consistent revision boundary while the export streams.
3. **Stopped-container folder backup:** in Container Manager stop the normal project, wait for its server to exit, then copy the **entire `data` directory** somewhere private with File Station/your NAS backup tool. Include `travel.sqlite3`, any `-wal`/`-shm` sidecars and the entire `originals` tree. Do not copy only the SQLite file from a running container. Restart after the copy completes.

Keep a separate private copy of your project configuration and `.env` (in a password-manager/encrypted backup), not just the data. The seed is not a current backup. Never delete the database to “refresh” the app: that would cause a fresh initial import.

To update: take a backup first, stop the project, replace the source/frontend files without overwriting `data` or `.env`, build the new image, then start the same project/data binding. Never start two copies against the same data directory. To recover a full directory backup, stop the app and have its administrator restore the **whole matching data directory** with UID/GID 10001 permissions. The browser can restore/import its current-state backup, but a full server-history restore or history merge from `/api/backup` JSON is not automated by this release. Keep the history JSON for offline recovery/review and the stopped-directory backup for full server restoration.

## Reader and storage boundaries

- Files: **12 MiB each**; originals in the current state: **250 MiB total** (larger saves upload in batches). History is retained separately and can exceed that total.
- Whole-state HTTP request: 60 MiB; metadata: 4 MiB; at most 10,000 rows per collection.
- Offline readers: Python text/EML parsing; Poppler PDF text; Poppler rasterization plus Tesseract **English** OCR for scanned/hybrid PDFs; Tesseract English OCR for images. Linux reader packages are installed at image build, not fetched from a CDN at runtime.
- Maximum 20 PDF pages; encrypted PDFs stop and require a trusted local unlocked copy. Images: 20 million pixels / 12,000-pixel longest edge, one frame only. Each local read is bounded to approximately 55 seconds, one at a time, with memory/file limits.
- Text is capped at 100,000 characters; a truncation notice is included in the extracted text and confidence is reduced. Review the original if the notice appears.
- Unsupported formats remain stored intact; automatic reading is not promised. Word/Excel files are not automatically parsed by this backend.
- The entire package, seed, backups and server directory contain private material. Do not upload them to public web hosting or public image registries.

For exact APIs, local verification and technical security details, see `README.md`.
