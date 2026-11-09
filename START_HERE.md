# Travel Manager — start here

Private travel itinerary app for a Synology NAS. Everything below uses the DSM web interface (File Station, Task Scheduler, Container Manager). No SSH needed.

The app folder in all examples is the real one:

```
/volume1/travelapp/Travel_Manager_NAS_Easy_Setup_v2
```

If you install somewhere else, replace that path everywhere.

---

## A. Upgrading an existing installation (v2 → v4)

Your password, host name and data are kept. The data format is unchanged, so no migration runs.

1. **Back up first.** In the app, open *Excel & backup* → **Current-state backup**. Keep the file private.
2. **Copy the new files in.** In File Station, upload the ZIP to `/volume1/travelapp/` and choose **Extract → Extract here**, allowing it to overwrite. The ZIP contains the folder `Travel_Manager_NAS_Easy_Setup_v2/` with program files only — it never contains `data/`, `.env`, `.installer/` or `private/LOGIN.txt`, so those stay as they are.
3. **(Optional) Add your Anthropic API key** for AI document reading — see section C. You can do this later.
4. **Rebuild.** Control Panel → Task Scheduler → select your Travel Manager task → **Edit** → *Task Settings*. Set the command to:

   ```sh
   /bin/sh /volume1/travelapp/Travel_Manager_NAS_Easy_Setup_v2/install.sh --build
   ```

   (Add ` --set-ai-key` at the end if you did step 3.) Click **OK**, then select the task and click **Run**.
5. **Check the result.** Open `private/STATUS.txt` in File Station. It should say `Completed local installation`. Then sign in at `https://travel.businessintels.com` as before.
6. **Set the task back** to the plain `--build`-free command for future reruns:

   ```sh
   /bin/sh /volume1/travelapp/Travel_Manager_NAS_Easy_Setup_v2/install.sh
   ```

If the task fails, `private/STATUS.txt` and the task's email/output show a reason code such as `E_SEED_MODE`. Look it up in [INSTALLER_DETAILS.md](INSTALLER_DETAILS.md#reason-codes).

---

## B. First installation (new NAS or new folder)

**Before you start:** Container Manager must be installed. HTTPS to the app (Cloudflare Tunnel or a reverse proxy to `http://127.0.0.1:8787`) is a separate step, not done by this installer.

1. In File Station, create a private shared folder (here `travelapp`), upload the ZIP there and **Extract here**. Do not use a Web Station public folder.
2. Control Panel → **Task Scheduler → Create → Scheduled Task → User-defined script**.
   - *General*: user **root**, untick **Enabled** (it only runs when you click Run).
   - *Task Settings* → command (use your real host name):

     ```sh
     /bin/sh /volume1/travelapp/Travel_Manager_NAS_Easy_Setup_v2/install.sh --host travel.businessintels.com
     ```
3. Select the task → **Run**. The first build downloads packages and can take several minutes.
4. In File Station, as the account that owns the folder, open `private/LOGIN.txt` (your password) and `private/STATUS.txt`.
5. Edit the task again and remove `--host …` so later reruns use the stored settings.

Optional dry run: add `--check` to the command. It changes nothing.

---

## C. AI document reading (Anthropic Claude)

With a key, the app sends each uploaded original (PDF, photo, email or pasted text) to the Anthropic API. Claude reads it and returns every booking segment — each flight leg separately, hotel stays, car rentals, meals, meetings — with local dates, times and IANA time zones. The NAS then re-checks every date, time and zone (daylight-saving gaps, “+1 day” arrivals, printed UTC offsets, impossible durations), matches the trip and only adds items automatically when everything is clear. Everything else goes to the review list with a reason.

Without a key (or when AI reading is switched off, or the API cannot be reached), the local NAS readers are used as before.

**Privacy:** with AI reading on, document contents leave the NAS and go to Anthropic. You can switch it off any time in *Documents → Read documents with Claude AI*.

**Network:** the NAS needs outbound HTTPS to `api.anthropic.com`. Mainland China is not a supported region for the Anthropic API and connections from there often fail. If reading reports “could not reach api.anthropic.com”, the local reader is used automatically.

To add the key:

1. Create an API key at console.anthropic.com (Settings → API keys). It starts with `sk-ant-`.
2. On your computer, create a plain text file named exactly `ANTHROPIC_API_KEY.txt` that contains **only the key**. Upload it with File Station into the app's `private/` folder.
3. Run the task once with `--set-ai-key` at the end of the command (it can be combined with `--build`). The installer moves the key into the root-only `.env`, **deletes the text file**, and restarts the app. The key is never printed.
4. Set the task command back to normal.

To check that the NAS can reach Claude: Container Manager → **Container** → the Travel Manager container → **Terminal** → **Create** → run

```sh
python /app/ai_reader.py --selftest
```

It reads a built-in sample booking (no personal data) and prints the two flight legs with their time zones, or the reason it failed (for example “could not reach api.anthropic.com”).

To remove the key: run once with `--remove-ai-key`. To replace it: repeat steps 2–3 with the new key.

Each read costs a few cents of API usage on your Anthropic account (model `claude-opus-5-5`).

---

## D. Changing the password

Run the task once with `--rotate-password` at the end of the command. A new random password is written to `.env` and `private/LOGIN.txt`, the app restarts, and every device must sign in again with the new password from `private/LOGIN.txt`. Set the task command back to normal afterwards.

---

More detail: [INSTALLER_DETAILS.md](INSTALLER_DETAILS.md). What was tested: [VERIFICATION.md](VERIFICATION.md).
