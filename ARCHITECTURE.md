# Architecture & Technical Research Summary

## 1. System Topology
```
[Cloud Scheduler: Built-in 60m Thread / Cloudflare Cron / GitHub Actions]
                               │
                               ▼
              [Python Worker: backend/worker.py]
       ├── Primary Discovery: Steam Store Filtered Search (paginated, cc=in)
       ├── Primary Verify:    Steam api/appdetails (cc=in) + Store Page HTML
       ├── Secondary Verify:  CheapShark API (storeID=1) + GamerPower API
       ├── 10-Gate Validator: VERIFIED / QUARANTINED / REJECTED
       └── Outbox Dispatcher: ntfy.sh + Telegram Bot API (independent per provider)
                               │
                               ▼
             [Durable WAL SQLite: data/steam_tracker.db]
       (5 datasets: Cycles, Games Inventory, Live Events, Notifications, Sync)
                               │
                               ▼
       [FastAPI REST + SSE Server: backend/server.py + ReportLab PDFs]
                               │
                               ▼
         [PWA Frontend: frontend/index.html + sw.js + IndexedDB]
            (Windows Chrome Desktop & Android Add to Home Screen)
```

## 2. Verified Data Sources & Limitations
- **Steam Store Filtered Search**: `https://store.steampowered.com/search/results/?query=&maxprice=free&specials=1&category1=998&cc=in&l=english&infinite=1&start=<offset>&count=50`. Uses `category1=998` (full games only) and paginates until `start >= total_count` ([Steam Search Filter Reference](https://www.reddit.com/r/Steam/comments/1psfqbi/tip_2_never_miss_a_free_game_100_off_the_browser/)).
- **Steam AppDetails**: `https://store.steampowered.com/api/appdetails?appids=<id>&cc=in&l=english` ([Steamworks Web API](https://partner.steamgames.com/doc/webapi_overview), [SteamDB Store Prices](https://steamdb.info/blog/store-prices-api/)). Verifies `type == "game"`, `price_overview.currency == "INR"`, `initial > 0`, `final == 0`, and `discount_percent == 100`.
- **CheapShark API**: `GET https://www.cheapshark.com/api/1.0/deals?storeID=1&upperPrice=0&onSale=1` ([CheapShark API Docs](https://apidocs.cheapshark.com/)). Prices are always in USD; uses `X-Total-Page-Count` header for pagination and respects HTTP 429 `Retry-After`.
- **GamerPower API**: `GET https://www.gamerpower.com/api/giveaways?platform=steam&type=game` ([GamerPower API Docs](https://www.gamerpower.com/api-read)). Free, no auth, fair use. Frequently lists third-party Steam key giveaways (e.g., Fanatical, Humble); our validator detects non-Steam URLs and places them strictly into **Category B (Quarantined)**.

## 3. Cloud Hosting & Free-Tier Reality Check (2026)
- **GitHub Actions** ([Limits](https://docs.github.com/en/actions/reference/limits), [Free Tier Breakdown](https://cicdcalculator.com/github-actions-free-tier)): Public repos are free and unmetered; private repos get 2,000 Linux minutes/month. Cron schedules can experience 5–20+ minute delays and can be unreliable on free private repos.
- **Cloudflare Workers Cron Triggers** ([Limits](https://developers.cloudflare.com/workers/platform/limits/), [D1 Free Tier Enforcement](https://developers.cloudflare.com/changelog/post/2026-09-01-d1-free-tier-limit-enforcement/)): Free tier provides 5 reliable Cron Triggers (`deploy/cloudflare-cron-worker.js`). D1 free tier enforces daily row read/write limits (Paid Workers plan starts at $5/mo).
- **Render** ([Free Tier Docs](https://render.com/docs/free), [Pricing](https://render.com/pricing)): Free web services spin down after 15 minutes of inactivity and lack persistent disks. Always-on web service with persistent disk starts at ~$7/mo (`render.yaml`).
- **Recommended Low-Cost 24/7 Setup**: A small Linux VPS ($4–$6/mo) or Render Starter ($7/mo) running `docker compose up -d` with HTTPS (Caddy/Cloudflare Tunnel/Render TLS), optionally backed by the free Cloudflare Cron Trigger as a redundant hourly heartbeat.

## 4. Notification Provider Policies
- **ntfy.sh** ([Publish Docs](https://docs.ntfy.sh/publish/), [Privacy & Retention](https://docs.ntfy.sh/privacy/)): Free tier allows 250 messages/day and caches messages for **12 hours** for offline Android devices via Firebase Cloud Messaging.
- **Telegram Bot API** ([Bots FAQ](https://core.telegram.org/bots/faq)): Free, ~1 message/sec per chat, and Telegram cloud retains chat history indefinitely so your phone receives missed alerts even after days offline.
