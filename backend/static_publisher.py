"""Static HTTPS PWA + JSON API + PDF publisher for $0/month GitHub Pages hosting.

Exports the complete PWA frontend, static JSON API snapshots, chronological worker console
events, individual cycle PDFs, full historical PDF, and JSON/CSV backups into a single
directory ready for GitHub Pages deployment.
"""
from __future__ import annotations

import csv
import io
import json
import shutil
from pathlib import Path
from typing import Any, Dict

from backend.config import TrackerConfig
from backend.database import TrackerDatabase
from backend.models import ist_iso, utc_iso
from backend.pdf_reports import generate_cycle_pdf_bytes, generate_historical_pdf_bytes


def publish_static_site(
    config: TrackerConfig,
    db: TrackerDatabase,
    out_dir: str,
    frontend_dir: str | None = None,
) -> Dict[str, Any]:
    out = Path(out_dir)
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True, exist_ok=True)

    fe_src = Path(frontend_dir) if frontend_dir else (Path(__file__).resolve().parent.parent / "frontend")

    # 1. Copy PWA frontend with relative asset paths so GitHub Pages subpath (/repo/) works cleanly
    static_out = out / "static"
    static_out.mkdir(parents=True, exist_ok=True)

    for rel in ("styles.css", "app.js"):
        if (fe_src / rel).exists():
            shutil.copy2(fe_src / rel, static_out / rel)
    if (fe_src / "icons").exists():
        shutil.copytree(fe_src / "icons", static_out / "icons", dirs_exist_ok=True)

    # Rewrite root-anchored paths in index.html, manifest.webmanifest, and sw.js to relative paths
    if (fe_src / "index.html").exists():
        idx_html = (fe_src / "index.html").read_text(encoding="utf-8")
        idx_html = (
            idx_html.replace('href="/manifest.webmanifest"', 'href="./manifest.webmanifest"')
            .replace('href="/static/', 'href="./static/')
            .replace('src="/static/', 'src="./static/')
        )
        (out / "index.html").write_text(idx_html, encoding="utf-8")

    if (fe_src / "manifest.webmanifest").exists():
        man = json.loads((fe_src / "manifest.webmanifest").read_text(encoding="utf-8"))
        man["start_url"] = "./"
        man["scope"] = "./"
        for ic in man.get("icons", []):
            if str(ic.get("src", "")).startswith("/static/"):
                ic["src"] = "." + ic["src"]
        (out / "manifest.webmanifest").write_text(json.dumps(man, indent=2), encoding="utf-8")

    if (fe_src / "sw.js").exists():
        sw = (fe_src / "sw.js").read_text(encoding="utf-8")
        sw = (
            sw.replace('"/"', '"./"')
            .replace('"/manifest.webmanifest"', '"./manifest.webmanifest"')
            .replace('"/static/', '"./static/')
        )
        (out / "sw.js").write_text(sw, encoding="utf-8")

    # 2. Export static JSON API endpoints
    api_dir = out / "api" / "v1"
    (api_dir / "live").mkdir(parents=True, exist_ok=True)
    (api_dir / "reports" / "cycle").mkdir(parents=True, exist_ok=True)
    (api_dir / "export" / "cycle").mkdir(parents=True, exist_ok=True)

    cycles, _ = db.list_cycles(limit=10000, offset=0)
    detailed_cycles = [db.get_cycle_detail(c["cycle_number"]) for c in cycles]
    all_games = db.list_games(limit=10000)
    active_games = [g for g in all_games if g["is_active_promo"] and g["current_status"] == "VERIFIED"]
    notifications = db.list_notifications(limit=500)
    live_state = db.get_live_state()
    stats = db.get_aggregate_stats()
    events = db.get_live_events(after_event_id=0, limit=250)

    sync_payload = {
        "hosting_mode": "static_github_pages",
        "synced_at_utc": utc_iso(),
        "synced_at_ist": ist_iso(),
        "since_cycle": 0,
        "server_latest_cycle": db.get_last_cycle_number(),
        "missed_cycles_count": len(detailed_cycles),
        "missed_cycles": detailed_cycles,
        "active_verified_games": active_games,
        "historical_games": all_games,
        "notifications": notifications,
        "live_state": live_state,
        "stats": stats,
        "settings": {**config.public_summary(), "hosting_mode": "GitHub Pages + GitHub Actions ($0/mo)"},
    }
    (api_dir / "sync.json").write_text(json.dumps(sync_payload, indent=2), encoding="utf-8")

    (api_dir / "live" / "events.json").write_text(
        json.dumps({"events": events, "last_event_id": events[-1]["event_id"] if events else 0}, indent=2),
        encoding="utf-8",
    )

    (api_dir / "health.json").write_text(
        json.dumps(
            {
                "status": "ok",
                "hosting_mode": "static_github_pages",
                "published_at_utc": utc_iso(),
                "published_at_ist": ist_iso(),
                "schema_version": db.get_schema_version(),
                "last_cycle_number": db.get_last_cycle_number(),
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    # 3. Generate individual cycle PDFs & per-cycle JSON backups
    pdfs_written = 0
    for cyc in detailed_cycles:
        if not cyc:
            continue
        cnum = cyc["cycle_number"]
        pdf_bytes = generate_cycle_pdf_bytes(cyc)
        (api_dir / "reports" / "cycle" / f"{cnum}.pdf").write_bytes(pdf_bytes)
        (api_dir / "export" / "cycle" / f"{cnum}.json").write_text(json.dumps(cyc, indent=2), encoding="utf-8")
        pdfs_written += 1

    # 4. Generate full historical audit PDF
    hist_pdf = generate_historical_pdf_bytes(cycles, all_games, stats)
    (api_dir / "reports" / "historical.pdf").write_bytes(hist_pdf)

    # 5. Full JSON backup & CSV exports
    (api_dir / "export" / "full-backup.json").write_text(
        json.dumps(db.export_full_backup(), indent=2), encoding="utf-8"
    )

    games_buf = io.StringIO()
    gw = csv.writer(games_buf)
    gw.writerow([
        "game_key", "app_id", "title", "current_status", "is_active_promo",
        "original_price", "promotional_price", "discount_percent", "currency",
        "region", "product_type", "first_discovered_ist", "last_verified_ist",
        "expiration_at", "manual_claimed", "claim_url", "decision_reason",
    ])
    for g in all_games:
        gw.writerow([
            g["game_key"], g["app_id"], g["title"], g["current_status"], g["is_active_promo"],
            g["original_price_formatted"], g["promotional_price_formatted"], g["discount_percent"],
            g["currency"], g["region"], g["product_type"], g["first_discovered_ist"],
            g["last_verified_ist"], g["expiration_at"] or "", g["manual_claimed"],
            g["claim_url"], g["decision_reason"],
        ])
    (api_dir / "export" / "games.csv").write_text(games_buf.getvalue(), encoding="utf-8")

    cyc_buf = io.StringIO()
    cw = csv.writer(cyc_buf)
    cw.writerow([
        "cycle_number", "scan_id", "status", "started_at_ist", "completed_at_ist",
        "duration_seconds", "pages_retrieved", "candidates_discovered", "games_checked",
        "verified_count", "quarantined_count", "rejected_count", "failed_requests",
        "retried_requests", "notification_status",
    ])
    for c in cycles:
        cw.writerow([
            c["cycle_number"], c["scan_id"], c["status"], c["started_at_ist"],
            c["completed_at_ist"] or "", c["duration_seconds"], c["pages_retrieved"],
            c["candidates_discovered"], c["games_checked"], c["verified_count"],
            c["quarantined_count"], c["rejected_count"], c["failed_requests"],
            c["retried_requests"], c["notification_status"],
        ])
    (api_dir / "export" / "cycles.csv").write_text(cyc_buf.getvalue(), encoding="utf-8")

    # Prevent GitHub Pages Jekyll from ignoring files
    (out / ".nojekyll").write_text("", encoding="utf-8")

    return {
        "out_dir": str(out.resolve()),
        "cycles_published": len(detailed_cycles),
        "games_published": len(all_games),
        "cycle_pdfs_written": pdfs_written,
        "historical_pdf_bytes": len(hist_pdf),
    }
