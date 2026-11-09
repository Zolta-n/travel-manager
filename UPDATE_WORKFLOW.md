# Updating Travel Manager

**NAS:** Synology DS225+ · **App:** `https://travel.businessintels.com` (Cloudflare Tunnel → `127.0.0.1:8787`) · **Repo:** `Zolta-n/travel-manager` (private)

## Normal flow

```
branch + pull request → GitHub Actions: tests (pull request)
merge to main → tests again → image ghcr.io/zolta-n/travel-manager:latest (+ :<commit>)
→ Watchtower on the NAS (polls every 5 min) → container restarted on the new image
```

- Nothing is published when a test fails.
- Only `main` deploys. Work happens on branches; merging the pull request is the approval.
- Sessions, data, password and API key survive the restart (they live in `data/` and `.env` on the NAS).

## Checking a deployment

1. GitHub → **Actions**: the run for the merge shows two green jobs, *test* and *publish*.
2. Within ~5 minutes: Container Manager → Travel Manager container → **Log** shows `Started; N signed-in device session(s) kept`.
3. Open the app; the footer shows the version.

## Rolling back

Every image is also tagged with its commit. To go back: on GitHub, revert the bad commit (or the merge) and merge the revert — the previous code is rebuilt and installed the same way.

## When the install task is needed

Only for installer or compose changes (rare; the pull request says so):
1. Download the **Travel_Manager_NAS_Easy_Setup_v2** artifact from the Actions run.
2. Extract it in File Station into `/volume1/travelapp/` with **Overwrite existing files**.
3. Run the DSM task with `install.sh --pull`, then set the command back to plain `install.sh`.

## Logs

Container Manager → container → **Log**. One line per save, conflict, sign-in failure, AI reading and restart. Never contains passwords, keys or document contents.

## Local development

```bash
pip install -r requirements.txt
python -m unittest discover -s tests
```

The real itinerary (`private/seed.json`) is never committed; tests use a stand-in.
