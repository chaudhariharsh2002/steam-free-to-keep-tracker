"""FastAPI server providing REST APIs, Server-Sent Events (SSE) live monitor stream,
synchronization endpoints, server-side PDF reports, JSON/CSV/Backup exports,
and static PWA frontend hosting.
"""
from __future__ import annotations

import asyncio
import csv
import io
import json
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from backend.config import TrackerConfig
from backend.database import TrackerDatabase
from backend.models import utc_iso, ist_iso
from backend.pdf_reports import generate_cycle_pdf_bytes, generate_historical_pdf_bytes
from backend.worker import ScanAlreadyRunningError, SteamTrackerWorker


class ClaimedUpdateBody(BaseModel):
    claimed: bool


def create_app(
    config: Optional[TrackerConfig] = None,
    db: Optional[TrackerDatabase] = None,
    frontend_dir: Optional[str] = None,
) -> FastAPI:
    cfg = config or TrackerConfig.from_env()
    cfg.ensure_directories()
    database = db or TrackerDatabase(cfg.db_path)

    app = FastAPI(
        title="Steam Free-to-Keep Tracker API",
        version="1.0.0",
        description="24/7 Steam Free-to-Keep monitoring, 10-gate validation, live SSE telemetry, and audit ledger API.",
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=cfg.cors_origins,
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.state.config = cfg
    app.state.db = database

    # --- Health & Config ---
    @app.get("/api/v1/health")
    def health() -> Dict[str, Any]:
        return {
            "status": "ok",
            "server_time_utc": utc_iso(),
            "server_time_ist": ist_iso(),
            "schema_version": database.get_schema_version(),
            "last_cycle_number": database.get_last_cycle_number(),
        }

    @app.get("/api/v1/settings")
    def get_settings() -> Dict[str, Any]:
        return cfg.public_summary()

    # --- Live Worker Monitor (Polling + Real-Time SSE) ---
    @app.get("/api/v1/live/status")
    def live_status() -> Dict[str, Any]:
        return {
            "server_time_utc": utc_iso(),
            "server_time_ist": ist_iso(),
            "worker": database.get_live_state(),
            "stats": database.get_aggregate_stats(),
        }

    @app.get("/api/v1/live/events")
    def live_events(
        after_event_id: int = Query(default=0, ge=0),
        limit: int = Query(default=100, ge=1, le=500),
        scan_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        events = database.get_live_events(after_event_id=after_event_id, limit=limit, scan_id=scan_id)
        last_id = events[-1]["event_id"] if events else after_event_id
        return {"events": events, "last_event_id": last_id}

    @app.get("/api/v1/live/stream")
    async def live_stream(after_event_id: int = Query(default=0, ge=0)):
        """Genuine Server-Sent Events (SSE) stream pushing real worker state & console events."""
        async def event_generator():
            last_id = after_event_id
            # Immediate snapshot on connect
            snapshot = {
                "worker": database.get_live_state(),
                "stats": database.get_aggregate_stats(),
                "server_time_utc": utc_iso(),
            }
            yield f"event: status\ndata: {json.dumps(snapshot)}\n\n"

            for _ in range(180):  # Graceful 6-minute stream window; EventSource auto-reconnects
                new_events = database.get_live_events(after_event_id=last_id, limit=50)
                for ev in new_events:
                    last_id = ev["event_id"]
                    yield f"id: {last_id}\nevent: console\ndata: {json.dumps(ev)}\n\n"
                status_payload = {
                    "worker": database.get_live_state(),
                    "stats": database.get_aggregate_stats(),
                    "server_time_utc": utc_iso(),
                }
                yield f"event: status\ndata: {json.dumps(status_payload)}\n\n"
                await asyncio.sleep(2.0)

        return StreamingResponse(
            event_generator(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # --- Synchronization Endpoint (Online Startup + Reconnection Catch-Up) ---
    @app.get("/api/v1/sync")
    def sync_client(
        since_cycle: int = Query(default=0, ge=0),
        client_id: str = Query(default="browser"),
    ) -> Dict[str, Any]:
        missed_cycles, _ = database.list_cycles(limit=1000, offset=0, after_cycle=since_cycle)
        detailed_missed = [database.get_cycle_detail(c["cycle_number"]) for c in missed_cycles]
        all_games = database.list_games(limit=5000)
        active_games = [g for g in all_games if g["is_active_promo"] and g["current_status"] == "VERIFIED"]
        notifications = database.list_notifications(limit=200)
        live = database.get_live_state()
        stats = database.get_aggregate_stats()
        server_latest = database.get_last_cycle_number()

        database.record_sync(
            client_id=client_id[:64],
            last_client_cycle=since_cycle,
            server_latest_cycle=server_latest,
            cycles_returned=len(detailed_missed),
            games_returned=len(all_games),
        )

        return {
            "synced_at_utc": utc_iso(),
            "synced_at_ist": ist_iso(),
            "since_cycle": since_cycle,
            "server_latest_cycle": server_latest,
            "missed_cycles_count": len(detailed_missed),
            "missed_cycles": detailed_missed,
            "active_verified_games": active_games,
            "historical_games": all_games,
            "notifications": notifications,
            "live_state": live,
            "stats": stats,
            "settings": cfg.public_summary(),
        }

    # --- Games Inventory ---
    @app.get("/api/v1/games")
    def list_games(
        active_only: bool = False,
        status: Optional[str] = None,
        search: Optional[str] = None,
        limit: int = Query(default=500, ge=1, le=5000),
        offset: int = Query(default=0, ge=0),
    ) -> Dict[str, Any]:
        items = database.list_games(active_only=active_only, status=status, search=search, limit=limit, offset=offset)
        return {"count": len(items), "games": items}

    @app.patch("/api/v1/games/{game_key:path}/claimed")
    def toggle_claimed(game_key: str, body: ClaimedUpdateBody) -> Dict[str, Any]:
        updated = database.set_manual_claimed(game_key, body.claimed)
        if not updated:
            raise HTTPException(status_code=404, detail="Game record not found")
        return {"game": updated}

    # --- Permanent Hourly Scan Ledger ---
    @app.get("/api/v1/cycles")
    def list_cycles(
        limit: int = Query(default=100, ge=1, le=1000),
        offset: int = Query(default=0, ge=0),
        status: Optional[str] = None,
        date: Optional[str] = None,
    ) -> Dict[str, Any]:
        items, total = database.list_cycles(limit=limit, offset=offset, status=status, date_prefix=date)
        return {"total": total, "count": len(items), "cycles": items}

    @app.get("/api/v1/cycles/{cycle_number}")
    def get_cycle(cycle_number: int) -> Dict[str, Any]:
        detail = database.get_cycle_detail(cycle_number)
        if not detail:
            raise HTTPException(status_code=404, detail=f"Cycle #{cycle_number} not found")
        return detail

    # --- Notifications History ---
    @app.get("/api/v1/notifications")
    def list_notifications(limit: int = Query(default=100, ge=1, le=1000)) -> Dict[str, Any]:
        return {"notifications": database.list_notifications(limit=limit)}

    # --- Individual & Historical PDF Reports ---
    @app.get("/api/v1/reports/cycle/{cycle_number}.pdf")
    def download_cycle_pdf(cycle_number: int, download: bool = False):
        detail = database.get_cycle_detail(cycle_number)
        if not detail:
            raise HTTPException(status_code=404, detail=f"Cycle #{cycle_number} not found")
        pdf_bytes = generate_cycle_pdf_bytes(detail)
        disp = "attachment" if download else "inline"
        filename = f"steam-ftk-cycle-{cycle_number}-{detail['scan_id']}.pdf"
        return Response(
            content=pdf_bytes,
            media_type="application/pdf",
            headers={"Content-Disposition": f'{disp}; filename="{filename}"'},
        )

    @app.get("/api/v1/reports/historical.pdf")
    def download_historical_pdf(download: bool = False):
        cycles, _ = database.list_cycles(limit=5000, offset=0)
        games = database.list_games(limit=5000)
        stats = database.get_aggregate_stats()
        pdf_bytes = generate_historical_pdf_bytes(cycles, games, stats)
        disp = "attachment" if download else "inline"
        return Response(
            content=pdf_bytes,
            media_type="application/pdf",
            headers={"Content-Disposition": f'{disp}; filename="steam-ftk-historical-audit.pdf"'},
        )

    # --- Data Export & Full Backup ---
    @app.get("/api/v1/export/json")
    def export_json():
        backup = database.export_full_backup()
        return JSONResponse(
            content=backup,
            headers={"Content-Disposition": 'attachment; filename="steam-ftk-full-backup.json"'},
        )

    @app.get("/api/v1/export/cycle/{cycle_number}.json")
    def export_cycle_json(cycle_number: int):
        detail = database.get_cycle_detail(cycle_number)
        if not detail:
            raise HTTPException(status_code=404, detail=f"Cycle #{cycle_number} not found")
        return JSONResponse(
            content=detail,
            headers={"Content-Disposition": f'attachment; filename="steam-ftk-cycle-{cycle_number}.json"'},
        )

    @app.get("/api/v1/export/csv")
    def export_csv(dataset: str = Query(default="games", pattern="^(games|cycles)$")):
        buf = io.StringIO()
        writer = csv.writer(buf)
        if dataset == "games":
            writer.writerow([
                "game_key", "app_id", "title", "current_status", "is_active_promo",
                "original_price", "promotional_price", "discount_percent", "currency",
                "region", "product_type", "first_discovered_ist", "last_verified_ist",
                "expiration_at", "manual_claimed", "claim_url", "decision_reason",
            ])
            for g in database.list_games(limit=100000):
                writer.writerow([
                    g["game_key"], g["app_id"], g["title"], g["current_status"], g["is_active_promo"],
                    g["original_price_formatted"], g["promotional_price_formatted"], g["discount_percent"],
                    g["currency"], g["region"], g["product_type"], g["first_discovered_ist"],
                    g["last_verified_ist"], g["expiration_at"] or "", g["manual_claimed"],
                    g["claim_url"], g["decision_reason"],
                ])
            fname = "steam-ftk-games-history.csv"
        else:
            writer.writerow([
                "cycle_number", "scan_id", "status", "started_at_ist", "completed_at_ist",
                "duration_seconds", "pages_retrieved", "candidates_discovered", "games_checked",
                "verified_count", "quarantined_count", "rejected_count", "failed_requests",
                "retried_requests", "notification_status",
            ])
            cycles, _ = database.list_cycles(limit=100000, offset=0)
            for c in cycles:
                writer.writerow([
                    c["cycle_number"], c["scan_id"], c["status"], c["started_at_ist"],
                    c["completed_at_ist"] or "", c["duration_seconds"], c["pages_retrieved"],
                    c["candidates_discovered"], c["games_checked"], c["verified_count"],
                    c["quarantined_count"], c["rejected_count"], c["failed_requests"],
                    c["retried_requests"], c["notification_status"],
                ])
            fname = "steam-ftk-scan-ledger.csv"

        return Response(
            content=buf.getvalue(),
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{fname}"'},
        )

    # --- Authenticated Background Scan Trigger (for Cron / GitHub Actions / Cloudflare Worker) ---
    @app.post("/api/v1/scans/trigger")
    def trigger_scan(
        background_tasks: BackgroundTasks,
        authorization: Optional[str] = Header(default=None),
        sync_wait: bool = Query(default=False),
    ) -> Dict[str, Any]:
        if cfg.trigger_api_key:
            expected = f"Bearer {cfg.trigger_api_key}"
            if authorization != expected:
                raise HTTPException(status_code=401, detail="Invalid or missing Bearer token")

        def _execute():
            worker = SteamTrackerWorker(cfg, database)
            try:
                worker.run_cycle()
            except ScanAlreadyRunningError:
                pass
            finally:
                worker.close()

        if sync_wait:
            worker = SteamTrackerWorker(cfg, database)
            try:
                summary = worker.run_cycle()
                return {"triggered": True, "summary": summary}
            except ScanAlreadyRunningError as exc:
                raise HTTPException(status_code=409, detail=str(exc))
            finally:
                worker.close()

        background_tasks.add_task(_execute)
        return {"triggered": True, "mode": "background", "server_time_utc": utc_iso()}

    # --- Static PWA Frontend Serving ---
    fe_path = Path(frontend_dir) if frontend_dir else (Path(__file__).resolve().parent.parent / "frontend")
    if fe_path.exists():
        @app.get("/", include_in_schema=False)
        def serve_index():
            return FileResponse(fe_path / "index.html")

        @app.get("/manifest.webmanifest", include_in_schema=False)
        def serve_manifest():
            return FileResponse(fe_path / "manifest.webmanifest", media_type="application/manifest+json")

        @app.get("/sw.js", include_in_schema=False)
        def serve_sw():
            return FileResponse(
                fe_path / "sw.js",
                media_type="application/javascript",
                headers={"Service-Worker-Allowed": "/"},
            )

        app.mount("/static", StaticFiles(directory=str(fe_path)), name="static")

    return app


def start_in_process_scheduler(config: TrackerConfig, db: TrackerDatabase) -> threading.Event:
    """Runs an independent background scheduler thread inside always-on Docker/VPS deployments."""
    stop_event = threading.Event()

    def _loop():
        interval_sec = config.scan_interval_minutes * 60
        while not stop_event.is_set():
            worker = SteamTrackerWorker(config, db)
            try:
                worker.run_cycle()
            except ScanAlreadyRunningError:
                pass
            except Exception:
                pass
            finally:
                worker.close()
            stop_event.wait(interval_sec)

    t = threading.Thread(target=_loop, name="steam-ftk-hourly-scheduler", daemon=True)
    t.start()
    return stop_event
