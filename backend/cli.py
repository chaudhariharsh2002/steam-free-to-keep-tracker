"""Command-line interface for Steam Free-to-Keep Tracker.

Usage:
  python -m backend.cli run-scan          # Execute one complete background scan cycle
  python -m backend.cli serve             # Run FastAPI web server (add --with-scheduler for 24/7 built-in hourly loop)
  python -m backend.cli export-backup     # Export full JSON historical backup to disk
  python -m backend.cli dispatch-outbox   # Retry any pending/failed notification deliveries
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import uvicorn

from backend.config import TrackerConfig
from backend.database import TrackerDatabase
from backend.http_client import ResilientHTTPClient
from backend.notifications import NotificationDispatcher
from backend.server import create_app, start_in_process_scheduler
from backend.static_publisher import publish_static_site
from backend.worker import ScanAlreadyRunningError, SteamTrackerWorker


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Steam Free-to-Keep Tracker CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("run-scan", help="Run a single autonomous hourly scan cycle")

    p_serve = sub.add_parser("serve", help="Start the FastAPI dashboard & API server")
    p_serve.add_argument("--host", default=None)
    p_serve.add_argument("--port", type=int, default=None)
    p_serve.add_argument(
        "--with-scheduler",
        action="store_true",
        help="Run built-in 60-minute background scheduler thread alongside the web server",
    )

    p_bak = sub.add_parser("export-backup", help="Export full historical JSON backup")
    p_bak.add_argument("--out", default="data/backups/steam-ftk-backup.json")

    sub.add_parser("dispatch-outbox", help="Retry pending/failed notification outbox deliveries")

    p_pub = sub.add_parser("publish-static", help="Build static HTTPS PWA + JSON + PDFs for GitHub Pages")
    p_pub.add_argument("--out", default="site")

    args = parser.parse_args(argv)
    config = TrackerConfig.from_env()
    config.ensure_directories()
    db = TrackerDatabase(config.db_path)

    if args.command == "run-scan":
        worker = SteamTrackerWorker(config, db)
        try:
            summary = worker.run_cycle()
            print(json.dumps(summary, indent=2))
            return 0 if summary["status"] != "FAILED" else 2
        except ScanAlreadyRunningError as exc:
            print(f"SKIPPED: {exc}", file=sys.stderr)
            return 0
        finally:
            worker.close()

    if args.command == "serve":
        app = create_app(config=config, db=db)
        if args.with_scheduler:
            start_in_process_scheduler(config, db)
        uvicorn.run(
            app,
            host=args.host or config.host,
            port=args.port or config.port,
            log_level="info",
        )
        return 0

    if args.command == "export-backup":
        backup = db.export_full_backup()
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(backup, indent=2), encoding="utf-8")
        print(f"Backup written to {out_path}")
        return 0

    if args.command == "dispatch-outbox":
        http = ResilientHTTPClient(
            timeout_seconds=config.request_timeout_seconds,
            max_retries=config.max_retries,
            backoff_base_seconds=config.backoff_base_seconds,
        )
        try:
            disp = NotificationDispatcher(config, db, http)
            stats = disp.dispatch_pending()
            print(json.dumps(stats, indent=2))
            return 0
        finally:
            http.close()

    if args.command == "publish-static":
        res = publish_static_site(config, db, args.out)
        print(json.dumps(res, indent=2))
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
