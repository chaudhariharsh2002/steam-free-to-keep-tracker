# 24/7 Cloud Deployment Guide

To keep tracking every 60 minutes even when your Windows laptop and Android phone are switched off, deploy the backend to a cloud host.

## Option 1: Docker Compose on Any Linux VPS (Best Value & Full Control)
Works on any $4–$6/month Ubuntu VPS (Hetzner, DigitalOcean, Oracle Cloud, etc.).
1. Copy the project folder to your server and create `.env` from `.env.example`.
2. Run:
   ```bash
   docker compose up -d --build
   ```
3. Put Caddy or Nginx in front of port `8000` for free automatic HTTPS (required for Android PWA Service Worker installation).
4. Because `Dockerfile` launches `python -m backend.cli serve --with-scheduler`, the 60-minute autonomous scan runs 24/7 inside the container and persists all history to `./data/steam_tracker.db`.

## Option 2: Render Blueprint (Zero-Server Management)
1. Push this repository to GitHub (keep `.env` excluded via `.gitignore`).
2. In [Render](https://render.com/), click **New -> Blueprint** and select your repo (`render.yaml` is pre-configured for the Singapore region with a 1 GB persistent disk).
3. Fill in your secret environment variables (`NTFY_TOPIC`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`).
4. Render provides automatic HTTPS for instant Android PWA installation.
   - *Pricing note:* Render's Free tier spins down after 15 minutes idle and has no persistent disk; use Starter (~$7/mo) for true 24/7 always-on persistence.

## Option 3: Redundant External Hourly Trigger via Cloudflare Workers (Free Tier)
Cloudflare Workers Free includes 5 Cron Triggers (`deploy/cloudflare-cron-worker.js`):
```bash
cd deploy
npx wrangler deploy
npx wrangler secret put TRACKER_API_URL
npx wrangler secret put TRIGGER_API_KEY
```
Every hour on the hour, Cloudflare sends an authenticated `POST /api/v1/scans/trigger` request. If a scan is already running, the server's concurrent-run lock safely returns HTTP 409 without duplicating work.

## Option 4: GitHub Actions Scheduled Workflow
`.github/workflows/hourly-scan.yml` runs every hour (`cron: "5 * * * *"`). Add repository secrets (`TRACKER_API_URL` + `TRIGGER_API_KEY` for remote trigger mode, OR `NTFY_TOPIC` / `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` for standalone runner mode).
