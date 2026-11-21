# Steam Free-to-Keep Tracker

An autonomous, 24/7 production-ready application that monitors the [Steam Store](https://store.steampowered.com/) every 60 minutes for **previously paid full games temporarily discounted 100% to Free-to-Keep** in India (`IN` / `INR` / `IST`).

## What This Application Does
- **24/7 Cloud Background Tracking**: Runs independently of your Windows laptop, Android phone, or browser via Docker/VPS built-in hourly scheduler, [Cloudflare Workers Cron Triggers](https://developers.cloudflare.com/workers/configuration/cron-triggers/), or [GitHub Actions](https://docs.github.com/en/actions/reference/limits).
- **Multi-Source Discovery with Full Pagination**: Queries Steam Store filtered search (`category1=998&specials=1&maxprice=free&cc=in&infinite=1`), verifies each candidate via Steam `api/appdetails` and Steam Store product pages, and cross-checks [CheapShark API](https://apidocs.cheapshark.com/) and [GamerPower API](https://www.gamerpower.com/api-read).
- **10-Gate Evidence-Based Classification**: Strictly separates **Verified Direct Steam Free-to-Keep** from **Quarantined** (third-party key giveaways, missing secondary corroboration, conflicting sources) and **Rejected** (permanent Free-to-Play, free weekends, trials, demos, betas, playtests, DLC, soundtracks, expired offers).
- **Genuine Live Background Monitor**: Streams real worker progress, request counts, active game validation gates, and chronological console events over Server-Sent Events (SSE) with polling fallback — zero fake animations or synthetic counters.
- **Online + Offline PWA Dashboard**: Installable on Windows Chrome and Android (`Add to Home Screen`) with Service Worker app-shell caching, IndexedDB history storage, automatic reconnection catch-up sync, and an automatic Catch-Up Popup.
- **Permanent Audit Ledger & PDF Reports**: Persists every hourly cycle and every historical game forever in WAL-mode SQLite, with an in-app PDF Pop-Up Viewer and downloadable individual cycle + full historical audit PDFs.
- **Independent Phone Notifications**: Transactional outbox with per-provider delivery tracking for [ntfy.sh](https://docs.ntfy.sh/publish/) and [Telegram Bot API](https://core.telegram.org/bots/faq).

## Quick Start (Local Test on Windows/Linux)
```bash
python -m venv .venv
.venv\Scripts\activate      # Windows
# source .venv/bin/activate # Linux/macOS
pip install -r requirements.txt
copy .env.example .env      # Edit .env with your optional ntfy/Telegram keys
python tests/run_suite.py
python -m backend.cli serve --with-scheduler
```
Open `http://127.0.0.1:8000` in Chrome.

## Documentation
- [ARCHITECTURE.md](ARCHITECTURE.md) — System design, data sources, database schema, and cloud free-tier analysis.
- [docs/INSTALLATION.md](docs/INSTALLATION.md) — Beginner-friendly setup for Windows 10, Android PWA, ntfy.sh, and Telegram.
- [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) — 24/7 cloud hosting options (Docker/VPS, Render, Cloudflare Cron, GitHub Actions).
- [docs/MAINTENANCE.md](docs/MAINTENANCE.md) — Backups, schema migrations, troubleshooting, and log monitoring.
