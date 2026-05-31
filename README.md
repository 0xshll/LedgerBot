LedgerPilot — deploy notes for Tranger Cloud (Pro)

Recommended approach
--------------------
Use Tranger Cloud as a background "worker" (long-running process) with your existing `Dockerfile`.
With a Pro plan you can attach persistent storage and run Docker containers continuously.

Essential settings
------------------
- Source: GitHub repository `0xshll/LedgerBot`
- Branch: `main`
- Build: Use existing `Dockerfile` at repo root
- Runtime: "Background" / "Worker" (not HTTP/web) so Telegram polling can run uninterrupted
- Environment variables (set in Tranger secrets/env):
  - `TELEGRAM_BOT_TOKEN` — your bot token
  - `TELEGRAM_ALLOWED_USER_ID` — (optional) restrict access to your Telegram user id

Persistent storage
------------------
The app stores the SQLite DB at `/app/data/accounts.sqlite3` (see `Dockerfile` which sets `DATABASE_PATH=/app/data/accounts.sqlite3`).
- Create a persistent volume in Tranger and mount it at `/app/data` so account data survives restarts and redeploys.

Deployment checklist (Tranger UI)
--------------------------------
1. Dashboard → Deploy → Connect GitHub → select `0xshll/LedgerBot` → `main`
2. Ensure the platform selects Dockerfile build
3. Choose runtime type: Background / Worker
4. Add secrets: `TELEGRAM_BOT_TOKEN` (paste value in secret)
5. Create a persistent volume (size as you prefer) and mount to `/app/data`
6. Start deployment

Verify and troubleshooting
-------------------------
- Watch build logs for `LedgerPilot starting` and `Bot started. Listening for Telegram updates.`
- If Tranger reports the container stopped or "Failed to fetch logs":
  - Confirm the service was deployed as a worker/background process (not web)
  - Confirm the `TELEGRAM_BOT_TOKEN` secret is set
  - Check that the mounted volume exists and the container has permissions to write to `/app/data`

Optional: make the app web-friendly
-----------------------------------
If you prefer to deploy as an HTTP service (some platforms require it), I can add a tiny health endpoint and an optional webhook handler so the bot can run via Telegram webhooks instead of polling. This requires:
- Adding a small web server (FastAPI/Flask) to `script.py` or a new `web.py`
- A public HTTPS URL configured in the platform

If you want that change, say "add health endpoint" and I'll implement and test it locally.
