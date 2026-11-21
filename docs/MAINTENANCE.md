# Maintenance, Backups & Troubleshooting

## 1. Backups & Disaster Recovery
- **From the Dashboard**: Go to **10. Settings -> Download Full JSON Backup**, or export CSV tables from **Historical Game Inventory** and **Hourly Scan Ledger**.
- **From CLI**:
  ```bash
  python -m backend.cli export-backup --out data/backups/backup-$(date +%F).json
  ```
- **SQLite Database**: Copy `data/steam_tracker.db` (WAL mode ensures consistent reads). You can inspect any exported JSON backup completely offline using `standalone/steam-tracker-standalone-fallback.html`.

## 2. Database Schema Versioning
- Schema migrations are tracked in the `schema_migrations` table (`CURRENT_SCHEMA_VERSION = 1`).
- Upgrading the backend automatically applies any newer migrations inside an atomic transaction without deleting historical scan cycles or game inventory rows.

## 3. Retrying Failed Phone Notifications
If Telegram or ntfy.sh had a temporary outage, failed deliveries are queued in `notification_deliveries` with exponential backoff and retried automatically on the next hourly cycle. You can also retry immediately:
```bash
python -m backend.cli dispatch-outbox
```

## 4. Common Troubleshooting
- **Scan shows `FAILED` instead of `SUCCESS_NO_RESULTS`**: By design, network or Steam HTTP 5xx/429 outages are never disguised as zero-result successes. Open the cycle's **PDF Report** or **Hourly Scan Ledger** to view exact error messages and retry counts.
- **Game appears in `Quarantined` instead of `Verified`**: Inspect the game's **10-Gate Proof** modal. Usually either (a) it is a third-party key giveaway from GamerPower, or (b) CheapShark/GamerPower has not yet indexed the new Steam 100%-off discount (often takes up to 1 hour to corroborate).
- **Stale lock after power loss**: If the server crashes mid-scan, the concurrent-run lock automatically expires after `LOCK_STALE_AFTER_SECONDS` (default 30 minutes) so the next hourly cycle recovers cleanly.
