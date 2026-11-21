"""Durable SQLite database layer with schema versioning, WAL mode, atomic commits,
concurrent-run locking, persistent cycle numbering, and 5 distinct datasets:
1. Hourly scan history (scan_cycles + cycle_game_evaluations)
2. Individual game history (games_inventory)
3. Live progress events (live_worker_state + live_progress_events)
4. Notification delivery history (notification_outbox + notification_deliveries)
5. Application synchronization history (sync_history)
"""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

from backend.models import ExecutionOutcome, utc_iso, ist_iso


CURRENT_SCHEMA_VERSION = 1


MIGRATIONS: Dict[int, str] = {
    1: """
    CREATE TABLE IF NOT EXISTS schema_migrations (
        version INTEGER PRIMARY KEY,
        applied_at_utc TEXT NOT NULL,
        description TEXT NOT NULL
    );

    -- Persistent system counters & distributed/process-safe execution lock
    CREATE TABLE IF NOT EXISTS system_state (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL,
        updated_at_utc TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS worker_lock (
        lock_name TEXT PRIMARY KEY,
        owner_scan_id TEXT,
        locked_at_utc TEXT,
        expires_at_utc TEXT
    );

    -- Dataset 1: Permanent Hourly Audit Ledger
    CREATE TABLE IF NOT EXISTS scan_cycles (
        cycle_number INTEGER PRIMARY KEY,
        scan_id TEXT UNIQUE NOT NULL,
        status TEXT NOT NULL,
        started_at_utc TEXT NOT NULL,
        started_at_ist TEXT NOT NULL,
        completed_at_utc TEXT,
        completed_at_ist TEXT,
        duration_seconds REAL DEFAULT 0.0,
        region TEXT NOT NULL,
        currency TEXT NOT NULL,
        sources_checked_json TEXT NOT NULL DEFAULT '[]',
        steam_catalogue_size INTEGER,
        filtered_promo_total INTEGER,
        pages_retrieved INTEGER NOT NULL DEFAULT 0,
        total_pages_exposed INTEGER,
        candidates_discovered INTEGER NOT NULL DEFAULT 0,
        unique_app_ids INTEGER NOT NULL DEFAULT 0,
        games_checked INTEGER NOT NULL DEFAULT 0,
        verified_count INTEGER NOT NULL DEFAULT 0,
        quarantined_count INTEGER NOT NULL DEFAULT 0,
        rejected_count INTEGER NOT NULL DEFAULT 0,
        steam_search_requests INTEGER NOT NULL DEFAULT 0,
        appdetails_requests INTEGER NOT NULL DEFAULT 0,
        secondary_requests INTEGER NOT NULL DEFAULT 0,
        completed_requests INTEGER NOT NULL DEFAULT 0,
        failed_requests INTEGER NOT NULL DEFAULT 0,
        retried_requests INTEGER NOT NULL DEFAULT 0,
        api_response_times_json TEXT NOT NULL DEFAULT '{}',
        errors_json TEXT NOT NULL DEFAULT '[]',
        rejection_reasons_json TEXT NOT NULL DEFAULT '[]',
        quarantine_reasons_json TEXT NOT NULL DEFAULT '[]',
        notification_status TEXT NOT NULL DEFAULT 'NONE',
        publication_status TEXT NOT NULL DEFAULT 'PUBLISHED',
        sync_status TEXT NOT NULL DEFAULT 'READY',
        data_freshness TEXT NOT NULL DEFAULT 'FRESH',
        coverage_disclosure TEXT NOT NULL DEFAULT '',
        next_scheduled_at_utc TEXT
    );

    CREATE INDEX IF NOT EXISTS idx_scan_cycles_started ON scan_cycles(started_at_utc DESC);
    CREATE INDEX IF NOT EXISTS idx_scan_cycles_status ON scan_cycles(status);

    -- Per-cycle individual game validation outcomes
    CREATE TABLE IF NOT EXISTS cycle_game_evaluations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        cycle_number INTEGER NOT NULL,
        scan_id TEXT NOT NULL,
        app_id INTEGER,
        title TEXT NOT NULL,
        final_status TEXT NOT NULL,
        original_price_minor INTEGER,
        current_price_minor INTEGER,
        original_price_formatted TEXT NOT NULL,
        current_price_formatted TEXT NOT NULL,
        discount_percent INTEGER,
        currency TEXT NOT NULL,
        region TEXT NOT NULL,
        product_type TEXT NOT NULL,
        claim_url TEXT NOT NULL,
        expiration_at TEXT,
        primary_source TEXT NOT NULL,
        secondary_sources_json TEXT NOT NULL DEFAULT '[]',
        gates_json TEXT NOT NULL DEFAULT '[]',
        decision_reason TEXT NOT NULL,
        rejection_reasons_json TEXT NOT NULL DEFAULT '[]',
        quarantine_reasons_json TEXT NOT NULL DEFAULT '[]',
        checked_at_utc TEXT NOT NULL,
        checked_at_ist TEXT NOT NULL,
        FOREIGN KEY (cycle_number) REFERENCES scan_cycles(cycle_number) ON DELETE CASCADE
    );

    CREATE INDEX IF NOT EXISTS idx_eval_cycle ON cycle_game_evaluations(cycle_number);
    CREATE INDEX IF NOT EXISTS idx_eval_status ON cycle_game_evaluations(final_status);
    CREATE INDEX IF NOT EXISTS idx_eval_appid ON cycle_game_evaluations(app_id);

    -- Dataset 2: Complete Historical & Active Game Inventory (never auto-purged)
    CREATE TABLE IF NOT EXISTS games_inventory (
        game_key TEXT PRIMARY KEY, -- "appid:<id>" or "title:<slug>" if invalid appid
        app_id INTEGER,
        title TEXT NOT NULL,
        current_status TEXT NOT NULL, -- VERIFIED / QUARANTINED / REJECTED / EXPIRED
        is_active_promo INTEGER NOT NULL DEFAULT 0,
        original_price_minor INTEGER,
        promotional_price_minor INTEGER,
        original_price_formatted TEXT NOT NULL,
        promotional_price_formatted TEXT NOT NULL,
        discount_percent INTEGER,
        currency TEXT NOT NULL,
        region TEXT NOT NULL,
        product_type TEXT NOT NULL,
        claim_url TEXT NOT NULL,
        first_discovered_utc TEXT NOT NULL,
        first_discovered_ist TEXT NOT NULL,
        last_verified_utc TEXT NOT NULL,
        last_verified_ist TEXT NOT NULL,
        expiration_at TEXT,
        sources_json TEXT NOT NULL DEFAULT '[]',
        latest_validation_json TEXT NOT NULL DEFAULT '[]',
        decision_reason TEXT NOT NULL DEFAULT '',
        scan_ids_json TEXT NOT NULL DEFAULT '[]',
        manual_claimed INTEGER NOT NULL DEFAULT 0,
        manual_claimed_at_utc TEXT
    );

    CREATE INDEX IF NOT EXISTS idx_games_active ON games_inventory(is_active_promo, current_status);
    CREATE INDEX IF NOT EXISTS idx_games_appid ON games_inventory(app_id);

    -- Dataset 3: Real-Time Background Worker State & Chronological Live Console
    CREATE TABLE IF NOT EXISTS live_worker_state (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        status TEXT NOT NULL,
        current_cycle_number INTEGER NOT NULL DEFAULT 0,
        current_scan_id TEXT,
        scan_started_at_utc TEXT,
        last_completed_scan_utc TEXT,
        last_successful_scan_utc TEXT,
        next_scheduled_scan_utc TEXT,
        current_source TEXT,
        current_operation TEXT,
        steam_search_requests INTEGER NOT NULL DEFAULT 0,
        pages_retrieved INTEGER NOT NULL DEFAULT 0,
        current_page INTEGER NOT NULL DEFAULT 0,
        total_pages INTEGER,
        candidates_discovered INTEGER NOT NULL DEFAULT 0,
        unique_app_ids INTEGER NOT NULL DEFAULT 0,
        appdetails_requests INTEGER NOT NULL DEFAULT 0,
        candidates_waiting INTEGER NOT NULL DEFAULT 0,
        secondary_requests INTEGER NOT NULL DEFAULT 0,
        completed_requests INTEGER NOT NULL DEFAULT 0,
        failed_requests INTEGER NOT NULL DEFAULT 0,
        retried_requests INTEGER NOT NULL DEFAULT 0,
        current_game_json TEXT,
        updated_at_utc TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS live_progress_events (
        event_id INTEGER PRIMARY KEY AUTOINCREMENT,
        scan_id TEXT,
        cycle_number INTEGER,
        event_type TEXT NOT NULL,
        message TEXT NOT NULL,
        source TEXT,
        app_id INTEGER,
        payload_json TEXT NOT NULL DEFAULT '{}',
        created_at_utc TEXT NOT NULL,
        created_at_ist TEXT NOT NULL
    );

    CREATE INDEX IF NOT EXISTS idx_live_events_scan ON live_progress_events(scan_id, event_id DESC);
    CREATE INDEX IF NOT EXISTS idx_live_events_id ON live_progress_events(event_id DESC);

    -- Dataset 4: Transactional Notification Outbox & Independent Per-Provider Delivery History
    CREATE TABLE IF NOT EXISTS notification_outbox (
        outbox_id INTEGER PRIMARY KEY AUTOINCREMENT,
        dedupe_key TEXT UNIQUE NOT NULL, -- prevents duplicate alerts across repeated scans
        scan_id TEXT NOT NULL,
        cycle_number INTEGER NOT NULL,
        app_id INTEGER,
        game_title TEXT NOT NULL,
        classification TEXT NOT NULL, -- VERIFIED (or QUARANTINED if explicitly opted in)
        payload_json TEXT NOT NULL,
        created_at_utc TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS notification_deliveries (
        delivery_id INTEGER PRIMARY KEY AUTOINCREMENT,
        outbox_id INTEGER NOT NULL,
        provider TEXT NOT NULL, -- "ntfy" | "telegram"
        status TEXT NOT NULL, -- PENDING | DELIVERED | FAILED | PERMANENTLY_FAILED
        attempt_count INTEGER NOT NULL DEFAULT 0,
        max_attempts INTEGER NOT NULL DEFAULT 5,
        last_http_status INTEGER,
        last_error TEXT,
        next_retry_at_utc TEXT,
        delivered_at_utc TEXT,
        updated_at_utc TEXT NOT NULL,
        UNIQUE(outbox_id, provider),
        FOREIGN KEY (outbox_id) REFERENCES notification_outbox(outbox_id) ON DELETE CASCADE
    );

    CREATE INDEX IF NOT EXISTS idx_notif_status ON notification_deliveries(status, next_retry_at_utc);

    -- Dataset 5: Application Synchronization History
    CREATE TABLE IF NOT EXISTS sync_history (
        sync_id INTEGER PRIMARY KEY AUTOINCREMENT,
        client_id TEXT NOT NULL,
        last_client_cycle INTEGER NOT NULL DEFAULT 0,
        server_latest_cycle INTEGER NOT NULL DEFAULT 0,
        cycles_returned INTEGER NOT NULL DEFAULT 0,
        games_returned INTEGER NOT NULL DEFAULT 0,
        synced_at_utc TEXT NOT NULL,
        synced_at_ist TEXT NOT NULL
    );
    """
}


class DatabaseError(RuntimeError):
    """Raised on unrecoverable database persistence errors."""


class TrackerDatabase:
    """Thread-safe, WAL-enabled SQLite database with atomic transactions and migrations."""

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._local_lock = threading.RLock()
        self.initialize()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=FULL;")
        conn.execute("PRAGMA foreign_keys=ON;")
        return conn

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._local_lock:
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE;")
                yield conn
                if conn.in_transaction:
                    conn.execute("COMMIT;")
            except Exception as exc:
                try:
                    if conn.in_transaction:
                        conn.execute("ROLLBACK;")
                except Exception:
                    pass
                raise DatabaseError(f"Database transaction failed: {exc}") from exc
            finally:
                conn.close()

    @contextmanager
    def readonly(self) -> Iterator[sqlite3.Connection]:
        conn = self._connect()
        try:
            yield conn
        finally:
            conn.close()

    def initialize(self) -> None:
        with self.transaction() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version INTEGER PRIMARY KEY,
                    applied_at_utc TEXT NOT NULL,
                    description TEXT NOT NULL
                );
                """
            )
            row = conn.execute("SELECT MAX(version) AS v FROM schema_migrations").fetchone()
            current_v = row["v"] if row and row["v"] is not None else 0
            for ver in sorted(MIGRATIONS.keys()):
                if ver > current_v:
                    conn.executescript(MIGRATIONS[ver])
                    conn.execute(
                        "INSERT INTO schema_migrations (version, applied_at_utc, description) VALUES (?, ?, ?)",
                        (ver, utc_iso(), f"Schema migration v{ver}"),
                    )

            # Ensure persistent cycle counter exists and never resets
            now = utc_iso()
            conn.execute(
                "INSERT OR IGNORE INTO system_state (key, value, updated_at_utc) VALUES ('last_cycle_number', '0', ?)",
                (now,),
            )
            conn.execute(
                "INSERT OR IGNORE INTO worker_lock (lock_name, owner_scan_id, locked_at_utc, expires_at_utc) VALUES ('hourly_scan', NULL, NULL, NULL)"
            )
            conn.execute(
                """
                INSERT OR IGNORE INTO live_worker_state (
                    id, status, current_cycle_number, updated_at_utc
                ) VALUES (1, ?, 0, ?)
                """,
                (ExecutionOutcome.AWAITING_FIRST_SCAN.value, now),
            )

    def get_schema_version(self) -> int:
        with self.readonly() as conn:
            row = conn.execute("SELECT MAX(version) AS v FROM schema_migrations").fetchone()
            return int(row["v"]) if row and row["v"] is not None else 0

    # --- Concurrent-run protection ---
    def acquire_scan_lock(self, scan_id: str, stale_after_seconds: int = 1800) -> bool:
        now = datetime.now(timezone.utc)
        now_iso = utc_iso(now)
        expires_iso = utc_iso(datetime.fromtimestamp(now.timestamp() + stale_after_seconds, tz=timezone.utc))
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT owner_scan_id, expires_at_utc FROM worker_lock WHERE lock_name = 'hourly_scan'"
            ).fetchone()
            if row and row["owner_scan_id"]:
                exp = row["expires_at_utc"]
                if exp and exp > now_iso:
                    return False
            conn.execute(
                """
                UPDATE worker_lock
                SET owner_scan_id = ?, locked_at_utc = ?, expires_at_utc = ?
                WHERE lock_name = 'hourly_scan'
                """,
                (scan_id, now_iso, expires_iso),
            )
            return True

    def release_scan_lock(self, scan_id: str) -> None:
        with self.transaction() as conn:
            conn.execute(
                """
                UPDATE worker_lock
                SET owner_scan_id = NULL, locked_at_utc = NULL, expires_at_utc = NULL
                WHERE lock_name = 'hourly_scan' AND owner_scan_id = ?
                """,
                (scan_id,),
            )

    # --- Persistent cycle numbering (never resets) ---
    def allocate_next_cycle_number(self) -> int:
        with self.transaction() as conn:
            row = conn.execute("SELECT value FROM system_state WHERE key = 'last_cycle_number'").fetchone()
            current = int(row["value"]) if row else 0
            # Also check scan_cycles max in case of restore
            max_row = conn.execute("SELECT COALESCE(MAX(cycle_number), 0) AS m FROM scan_cycles").fetchone()
            nxt = max(current, int(max_row["m"])) + 1
            conn.execute(
                "INSERT OR REPLACE INTO system_state (key, value, updated_at_utc) VALUES ('last_cycle_number', ?, ?)",
                (str(nxt), utc_iso()),
            )
            return nxt

    def get_last_cycle_number(self) -> int:
        with self.readonly() as conn:
            row = conn.execute("SELECT value FROM system_state WHERE key = 'last_cycle_number'").fetchone()
            return int(row["value"]) if row else 0

    # --- Live Worker State & Chronological Console Events ---
    def update_live_state(self, **fields: Any) -> None:
        if not fields:
            return
        fields["updated_at_utc"] = utc_iso()
        cols = ", ".join(f"{k} = ?" for k in fields.keys())
        vals = list(fields.values())
        with self.transaction() as conn:
            conn.execute(f"UPDATE live_worker_state SET {cols} WHERE id = 1", vals)

    def get_live_state(self) -> Dict[str, Any]:
        with self.readonly() as conn:
            row = conn.execute("SELECT * FROM live_worker_state WHERE id = 1").fetchone()
            if not row:
                return {"status": ExecutionOutcome.AWAITING_FIRST_SCAN.value, "current_cycle_number": 0}
            data = dict(row)
            if data.get("current_game_json"):
                try:
                    data["current_game"] = json.loads(data["current_game_json"])
                except Exception:
                    data["current_game"] = None
            else:
                data["current_game"] = None
            data.pop("current_game_json", None)
            return data

    def append_live_event(
        self,
        event_type: str,
        message: str,
        scan_id: Optional[str] = None,
        cycle_number: Optional[int] = None,
        source: Optional[str] = None,
        app_id: Optional[int] = None,
        payload: Optional[Dict[str, Any]] = None,
    ) -> int:
        now_u = utc_iso()
        now_i = ist_iso()
        with self.transaction() as conn:
            cur = conn.execute(
                """
                INSERT INTO live_progress_events (
                    scan_id, cycle_number, event_type, message, source, app_id,
                    payload_json, created_at_utc, created_at_ist
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    scan_id,
                    cycle_number,
                    event_type,
                    message,
                    source,
                    app_id,
                    json.dumps(payload or {}),
                    now_u,
                    now_i,
                ),
            )
            return int(cur.lastrowid)

    def get_live_events(self, after_event_id: int = 0, limit: int = 100, scan_id: Optional[str] = None) -> List[Dict[str, Any]]:
        with self.readonly() as conn:
            if scan_id:
                rows = conn.execute(
                    """
                    SELECT * FROM live_progress_events
                    WHERE scan_id = ? AND event_id > ?
                    ORDER BY event_id ASC LIMIT ?
                    """,
                    (scan_id, after_event_id, limit),
                ).fetchall()
            elif after_event_id > 0:
                rows = conn.execute(
                    """
                    SELECT * FROM live_progress_events
                    WHERE event_id > ?
                    ORDER BY event_id ASC LIMIT ?
                    """,
                    (after_event_id, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT * FROM (
                        SELECT * FROM live_progress_events
                        ORDER BY event_id DESC LIMIT ?
                    ) ORDER BY event_id ASC
                    """,
                    (limit,),
                ).fetchall()
            result = []
            for r in rows:
                d = dict(r)
                d["payload"] = json.loads(d.pop("payload_json", "{}") or "{}")
                result.append(d)
            return result

    # --- Scan Cycle Persistence ---
    def create_scan_cycle(
        self,
        cycle_number: int,
        scan_id: str,
        region: str,
        currency: str,
        started_at_utc: str,
        started_at_ist: str,
        next_scheduled_at_utc: str,
    ) -> None:
        with self.transaction() as conn:
            conn.execute(
                """
                INSERT INTO scan_cycles (
                    cycle_number, scan_id, status, started_at_utc, started_at_ist,
                    region, currency, next_scheduled_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    cycle_number,
                    scan_id,
                    ExecutionOutcome.RUNNING.value,
                    started_at_utc,
                    started_at_ist,
                    region,
                    currency,
                    next_scheduled_at_utc,
                ),
            )

    def finalize_scan_cycle(
        self,
        cycle_number: int,
        scan_id: str,
        summary: Dict[str, Any],
        evaluations: List[Dict[str, Any]],
        outbox_items: List[Dict[str, Any]],
        providers: List[str],
        notification_max_retries: int = 5,
    ) -> List[int]:
        """Atomically persist scan ledger, individual evaluations, historical game inventory,
        and transactional notification outbox rows."""
        created_outbox_ids: List[int] = []
        now_u = utc_iso()
        now_i = ist_iso()

        with self.transaction() as conn:
            conn.execute(
                """
                UPDATE scan_cycles SET
                    status = ?, completed_at_utc = ?, completed_at_ist = ?,
                    duration_seconds = ?, sources_checked_json = ?,
                    steam_catalogue_size = ?, filtered_promo_total = ?,
                    pages_retrieved = ?, total_pages_exposed = ?,
                    candidates_discovered = ?, unique_app_ids = ?,
                    games_checked = ?, verified_count = ?, quarantined_count = ?, rejected_count = ?,
                    steam_search_requests = ?, appdetails_requests = ?, secondary_requests = ?,
                    completed_requests = ?, failed_requests = ?, retried_requests = ?,
                    api_response_times_json = ?, errors_json = ?,
                    rejection_reasons_json = ?, quarantine_reasons_json = ?,
                    notification_status = ?, publication_status = ?, sync_status = ?,
                    data_freshness = ?, coverage_disclosure = ?, next_scheduled_at_utc = ?
                WHERE cycle_number = ? AND scan_id = ?
                """,
                (
                    summary["status"],
                    summary["completed_at_utc"],
                    summary["completed_at_ist"],
                    summary["duration_seconds"],
                    json.dumps(summary.get("sources_checked", [])),
                    summary.get("steam_catalogue_size"),
                    summary.get("filtered_promo_total"),
                    summary.get("pages_retrieved", 0),
                    summary.get("total_pages_exposed"),
                    summary.get("candidates_discovered", 0),
                    summary.get("unique_app_ids", 0),
                    summary.get("games_checked", 0),
                    summary.get("verified_count", 0),
                    summary.get("quarantined_count", 0),
                    summary.get("rejected_count", 0),
                    summary.get("steam_search_requests", 0),
                    summary.get("appdetails_requests", 0),
                    summary.get("secondary_requests", 0),
                    summary.get("completed_requests", 0),
                    summary.get("failed_requests", 0),
                    summary.get("retried_requests", 0),
                    json.dumps(summary.get("api_response_times", {})),
                    json.dumps(summary.get("errors", [])),
                    json.dumps(summary.get("rejection_reasons", [])),
                    json.dumps(summary.get("quarantine_reasons", [])),
                    summary.get("notification_status", "NONE"),
                    summary.get("publication_status", "PUBLISHED"),
                    summary.get("sync_status", "READY"),
                    summary.get("data_freshness", "FRESH"),
                    summary.get("coverage_disclosure", ""),
                    summary.get("next_scheduled_at_utc"),
                    cycle_number,
                    scan_id,
                ),
            )

            verified_active_keys: set[str] = set()

            for ev in evaluations:
                conn.execute(
                    """
                    INSERT INTO cycle_game_evaluations (
                        cycle_number, scan_id, app_id, title, final_status,
                        original_price_minor, current_price_minor,
                        original_price_formatted, current_price_formatted,
                        discount_percent, currency, region, product_type, claim_url,
                        expiration_at, primary_source, secondary_sources_json,
                        gates_json, decision_reason, rejection_reasons_json,
                        quarantine_reasons_json, checked_at_utc, checked_at_ist
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        cycle_number,
                        scan_id,
                        ev.get("app_id"),
                        ev["title"],
                        ev["final_status"],
                        ev.get("original_price_minor"),
                        ev.get("current_price_minor"),
                        ev["original_price_formatted"],
                        ev["current_price_formatted"],
                        ev.get("discount_percent"),
                        ev["currency"],
                        ev["region"],
                        ev["product_type"],
                        ev["claim_url"],
                        ev.get("expiration_at"),
                        ev["primary_source"],
                        json.dumps(ev.get("secondary_sources", [])),
                        json.dumps(ev.get("gates", [])),
                        ev["decision_reason"],
                        json.dumps(ev.get("rejection_reasons", [])),
                        json.dumps(ev.get("quarantine_reasons", [])),
                        ev["checked_at_utc"],
                        ev["checked_at_ist"],
                    ),
                )

                # Upsert into permanent historical game inventory (never delete old records)
                game_key = f"appid:{ev['app_id']}" if ev.get("app_id") else f"title:{ev['title'].strip().lower()}"
                is_active = 1 if ev["final_status"] == "VERIFIED" else 0
                if is_active:
                    verified_active_keys.add(game_key)

                existing = conn.execute(
                    "SELECT * FROM games_inventory WHERE game_key = ?", (game_key,)
                ).fetchone()

                sources_set = {ev["primary_source"]}
                for s in ev.get("secondary_sources", []):
                    if s.get("source_name") and s.get("matched"):
                        sources_set.add(s["source_name"])

                if existing:
                    prev_sources = set(json.loads(existing["sources_json"] or "[]"))
                    prev_scans = list(json.loads(existing["scan_ids_json"] or "[]"))
                    if scan_id not in prev_scans:
                        prev_scans.append(scan_id)
                    merged_sources = sorted(prev_sources | sources_set)
                    conn.execute(
                        """
                        UPDATE games_inventory SET
                            title = ?, current_status = ?, is_active_promo = ?,
                            original_price_minor = COALESCE(?, original_price_minor),
                            promotional_price_minor = ?,
                            original_price_formatted = CASE WHEN ? != 'Unknown' THEN ? ELSE original_price_formatted END,
                            promotional_price_formatted = ?,
                            discount_percent = ?, currency = ?, region = ?, product_type = ?,
                            claim_url = ?, last_verified_utc = ?, last_verified_ist = ?,
                            expiration_at = COALESCE(?, expiration_at),
                            sources_json = ?, latest_validation_json = ?, decision_reason = ?,
                            scan_ids_json = ?
                        WHERE game_key = ?
                        """,
                        (
                            ev["title"],
                            ev["final_status"],
                            is_active,
                            ev.get("original_price_minor"),
                            ev.get("current_price_minor"),
                            ev["original_price_formatted"],
                            ev["original_price_formatted"],
                            ev["current_price_formatted"],
                            ev.get("discount_percent"),
                            ev["currency"],
                            ev["region"],
                            ev["product_type"],
                            ev["claim_url"],
                            ev["checked_at_utc"],
                            ev["checked_at_ist"],
                            ev.get("expiration_at"),
                            json.dumps(merged_sources),
                            json.dumps(ev.get("gates", [])),
                            ev["decision_reason"],
                            json.dumps(prev_scans),
                            game_key,
                        ),
                    )
                else:
                    conn.execute(
                        """
                        INSERT INTO games_inventory (
                            game_key, app_id, title, current_status, is_active_promo,
                            original_price_minor, promotional_price_minor,
                            original_price_formatted, promotional_price_formatted,
                            discount_percent, currency, region, product_type, claim_url,
                            first_discovered_utc, first_discovered_ist,
                            last_verified_utc, last_verified_ist,
                            expiration_at, sources_json, latest_validation_json,
                            decision_reason, scan_ids_json
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            game_key,
                            ev.get("app_id"),
                            ev["title"],
                            ev["final_status"],
                            is_active,
                            ev.get("original_price_minor"),
                            ev.get("current_price_minor"),
                            ev["original_price_formatted"],
                            ev["current_price_formatted"],
                            ev.get("discount_percent"),
                            ev["currency"],
                            ev["region"],
                            ev["product_type"],
                            ev["claim_url"],
                            ev["checked_at_utc"],
                            ev["checked_at_ist"],
                            ev["checked_at_utc"],
                            ev["checked_at_ist"],
                            ev.get("expiration_at"),
                            json.dumps(sorted(sources_set)),
                            json.dumps(ev.get("gates", [])),
                            ev["decision_reason"],
                            json.dumps([scan_id]),
                        ),
                    )

            # Mark previously active VERIFIED games as EXPIRED if the scan succeeded cleanly
            # and they are no longer present in active verified promotions (never delete them!)
            if summary["status"] in (
                ExecutionOutcome.SUCCESS_WITH_RESULTS.value,
                ExecutionOutcome.SUCCESS_NO_RESULTS.value,
            ):
                active_rows = conn.execute(
                    "SELECT game_key FROM games_inventory WHERE is_active_promo = 1"
                ).fetchall()
                for r in active_rows:
                    if r["game_key"] not in verified_active_keys:
                        conn.execute(
                            """
                            UPDATE games_inventory
                            SET is_active_promo = 0, current_status = 'EXPIRED'
                            WHERE game_key = ?
                            """,
                            (r["game_key"],),
                        )

            # Transactional Notification Outbox (deduplicated per promo window)
            for item in outbox_items:
                cur = conn.execute(
                    """
                    INSERT OR IGNORE INTO notification_outbox (
                        dedupe_key, scan_id, cycle_number, app_id, game_title,
                        classification, payload_json, created_at_utc
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        item["dedupe_key"],
                        scan_id,
                        cycle_number,
                        item.get("app_id"),
                        item["game_title"],
                        item["classification"],
                        json.dumps(item["payload"]),
                        now_u,
                    ),
                )
                if cur.rowcount > 0:
                    oid = int(cur.lastrowid)
                    created_outbox_ids.append(oid)
                    for prov in providers:
                        conn.execute(
                            """
                            INSERT OR IGNORE INTO notification_deliveries (
                                outbox_id, provider, status, attempt_count, max_attempts,
                                next_retry_at_utc, updated_at_utc
                            ) VALUES (?, ?, 'PENDING', 0, ?, ?, ?)
                            """,
                            (oid, prov, notification_max_retries, now_u, now_u),
                        )

        return created_outbox_ids

    # --- Query Helpers ---
    def _decode_cycle(self, row: sqlite3.Row) -> Dict[str, Any]:
        d = dict(row)
        for key, out_key in [
            ("sources_checked_json", "sources_checked"),
            ("api_response_times_json", "api_response_times"),
            ("errors_json", "errors"),
            ("rejection_reasons_json", "rejection_reasons"),
            ("quarantine_reasons_json", "quarantine_reasons"),
        ]:
            raw = d.pop(key, None)
            try:
                d[out_key] = json.loads(raw) if raw else ([] if key != "api_response_times_json" else {})
            except Exception:
                d[out_key] = [] if key != "api_response_times_json" else {}
        return d

    def _decode_eval(self, row: sqlite3.Row) -> Dict[str, Any]:
        d = dict(row)
        for key, out_key in [
            ("secondary_sources_json", "secondary_sources"),
            ("gates_json", "gates"),
            ("rejection_reasons_json", "rejection_reasons"),
            ("quarantine_reasons_json", "quarantine_reasons"),
        ]:
            raw = d.pop(key, None)
            try:
                d[out_key] = json.loads(raw) if raw else []
            except Exception:
                d[out_key] = []
        return d

    def _decode_game(self, row: sqlite3.Row) -> Dict[str, Any]:
        d = dict(row)
        d["is_active_promo"] = bool(d["is_active_promo"])
        d["manual_claimed"] = bool(d["manual_claimed"])
        for key, out_key in [
            ("sources_json", "sources"),
            ("latest_validation_json", "latest_validation"),
            ("scan_ids_json", "scan_ids"),
        ]:
            raw = d.pop(key, None)
            try:
                d[out_key] = json.loads(raw) if raw else []
            except Exception:
                d[out_key] = []
        return d

    def list_cycles(
        self,
        limit: int = 100,
        offset: int = 0,
        after_cycle: Optional[int] = None,
        status: Optional[str] = None,
        date_prefix: Optional[str] = None,
    ) -> Tuple[List[Dict[str, Any]], int]:
        where: List[str] = []
        params: List[Any] = []
        if after_cycle is not None:
            where.append("cycle_number > ?")
            params.append(after_cycle)
        if status:
            where.append("status = ?")
            params.append(status)
        if date_prefix:
            where.append("started_at_ist LIKE ?")
            params.append(f"{date_prefix}%")
        clause = f"WHERE {' AND '.join(where)}" if where else ""
        with self.readonly() as conn:
            total = conn.execute(f"SELECT COUNT(*) AS c FROM scan_cycles {clause}", params).fetchone()["c"]
            rows = conn.execute(
                f"SELECT * FROM scan_cycles {clause} ORDER BY cycle_number DESC LIMIT ? OFFSET ?",
                params + [limit, offset],
            ).fetchall()
            return [self._decode_cycle(r) for r in rows], int(total)

    def get_cycle_detail(self, cycle_number: int) -> Optional[Dict[str, Any]]:
        with self.readonly() as conn:
            row = conn.execute("SELECT * FROM scan_cycles WHERE cycle_number = ?", (cycle_number,)).fetchone()
            if not row:
                return None
            cycle = self._decode_cycle(row)
            ev_rows = conn.execute(
                "SELECT * FROM cycle_game_evaluations WHERE cycle_number = ? ORDER BY id ASC",
                (cycle_number,),
            ).fetchall()
            cycle["evaluations"] = [self._decode_eval(r) for r in ev_rows]
            return cycle

    def list_games(
        self,
        active_only: bool = False,
        status: Optional[str] = None,
        search: Optional[str] = None,
        limit: int = 500,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        where: List[str] = []
        params: List[Any] = []
        if active_only:
            where.append("is_active_promo = 1 AND current_status = 'VERIFIED'")
        if status:
            where.append("current_status = ?")
            params.append(status)
        if search:
            where.append("(LOWER(title) LIKE ? OR CAST(app_id AS TEXT) LIKE ?)")
            q = f"%{search.strip().lower()}%"
            params.extend([q, q])
        clause = f"WHERE {' AND '.join(where)}" if where else ""
        with self.readonly() as conn:
            rows = conn.execute(
                f"SELECT * FROM games_inventory {clause} ORDER BY last_verified_utc DESC LIMIT ? OFFSET ?",
                params + [limit, offset],
            ).fetchall()
            return [self._decode_game(r) for r in rows]

    def set_manual_claimed(self, game_key: str, claimed: bool) -> Optional[Dict[str, Any]]:
        now = utc_iso() if claimed else None
        with self.transaction() as conn:
            cur = conn.execute(
                "UPDATE games_inventory SET manual_claimed = ?, manual_claimed_at_utc = ? WHERE game_key = ?",
                (1 if claimed else 0, now, game_key),
            )
            if cur.rowcount == 0:
                return None
            row = conn.execute("SELECT * FROM games_inventory WHERE game_key = ?", (game_key,)).fetchone()
            return self._decode_game(row) if row else None

    def get_aggregate_stats(self) -> Dict[str, Any]:
        with self.readonly() as conn:
            c = conn.execute(
                """
                SELECT
                    COUNT(*) AS total_cycles,
                    COALESCE(SUM(pages_retrieved), 0) AS total_pages_retrieved,
                    COALESCE(SUM(candidates_discovered), 0) AS total_candidates_discovered,
                    COALESCE(SUM(games_checked), 0) AS total_games_checked,
                    COALESCE(SUM(verified_count), 0) AS total_verified_occurrences,
                    COALESCE(SUM(quarantined_count), 0) AS total_quarantined_occurrences,
                    COALESCE(SUM(rejected_count), 0) AS total_rejected_occurrences,
                    COALESCE(SUM(failed_requests), 0) AS total_errors
                FROM scan_cycles
                WHERE status != 'RUNNING'
                """
            ).fetchone()
            g_active = conn.execute(
                "SELECT COUNT(*) AS c FROM games_inventory WHERE is_active_promo = 1 AND current_status = 'VERIFIED'"
            ).fetchone()["c"]
            g_hist = conn.execute("SELECT COUNT(*) AS c FROM games_inventory").fetchone()["c"]
            g_ver = conn.execute(
                "SELECT COUNT(*) AS c FROM games_inventory WHERE current_status IN ('VERIFIED', 'EXPIRED')"
            ).fetchone()["c"]
            g_quar = conn.execute(
                "SELECT COUNT(*) AS c FROM games_inventory WHERE current_status = 'QUARANTINED'"
            ).fetchone()["c"]
            g_rej = conn.execute(
                "SELECT COUNT(*) AS c FROM games_inventory WHERE current_status = 'REJECTED'"
            ).fetchone()["c"]
            n_del = conn.execute(
                "SELECT COUNT(*) AS c FROM notification_deliveries WHERE status = 'DELIVERED'"
            ).fetchone()["c"]
            n_fail = conn.execute(
                "SELECT COUNT(*) AS c FROM notification_deliveries WHERE status IN ('FAILED', 'PERMANENTLY_FAILED')"
            ).fetchone()["c"]
            return {
                "total_completed_cycles": int(c["total_cycles"]),
                "total_pages_retrieved": int(c["total_pages_retrieved"]),
                "total_candidates_discovered": int(c["total_candidates_discovered"]),
                "total_games_checked": int(c["total_games_checked"]),
                "active_verified_opportunities": int(g_active),
                "total_verified_opportunities": int(g_ver),
                "total_quarantined_opportunities": int(g_quar),
                "total_rejected_candidates": int(g_rej),
                "total_historical_games": int(g_hist),
                "total_notifications_delivered": int(n_del),
                "total_notifications_failed": int(n_fail),
                "total_errors": int(c["total_errors"]),
            }

    # --- Notification Delivery Queries & Updates ---
    def fetch_pending_deliveries(self, limit: int = 50) -> List[Dict[str, Any]]:
        now = utc_iso()
        with self.readonly() as conn:
            rows = conn.execute(
                """
                SELECT d.*, o.scan_id, o.cycle_number, o.app_id, o.game_title,
                       o.classification, o.payload_json, o.dedupe_key
                FROM notification_deliveries d
                JOIN notification_outbox o ON d.outbox_id = o.outbox_id
                WHERE d.status IN ('PENDING', 'FAILED')
                  AND d.attempt_count < d.max_attempts
                  AND (d.next_retry_at_utc IS NULL OR d.next_retry_at_utc <= ?)
                ORDER BY d.delivery_id ASC LIMIT ?
                """,
                (now, limit),
            ).fetchall()
            out = []
            for r in rows:
                d = dict(r)
                d["payload"] = json.loads(d.pop("payload_json", "{}") or "{}")
                out.append(d)
            return out

    def update_delivery_result(
        self,
        delivery_id: int,
        delivered: bool,
        http_status: Optional[int],
        error: Optional[str],
        next_retry_at_utc: Optional[str],
    ) -> None:
        now = utc_iso()
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT attempt_count, max_attempts FROM notification_deliveries WHERE delivery_id = ?",
                (delivery_id,),
            ).fetchone()
            if not row:
                return
            attempts = int(row["attempt_count"]) + 1
            max_att = int(row["max_attempts"])
            if delivered:
                status = "DELIVERED"
                delivered_at = now
                next_retry = None
            elif attempts >= max_att:
                status = "PERMANENTLY_FAILED"
                delivered_at = None
                next_retry = None
            else:
                status = "FAILED"
                delivered_at = None
                next_retry = next_retry_at_utc
            conn.execute(
                """
                UPDATE notification_deliveries SET
                    status = ?, attempt_count = ?, last_http_status = ?,
                    last_error = ?, next_retry_at_utc = ?, delivered_at_utc = ?,
                    updated_at_utc = ?
                WHERE delivery_id = ?
                """,
                (status, attempts, http_status, error, next_retry, delivered_at, now, delivery_id),
            )

    def list_notifications(self, limit: int = 100) -> List[Dict[str, Any]]:
        with self.readonly() as conn:
            rows = conn.execute(
                """
                SELECT d.delivery_id, d.outbox_id, d.provider, d.status, d.attempt_count,
                       d.max_attempts, d.last_http_status, d.last_error, d.delivered_at_utc,
                       d.updated_at_utc, o.scan_id, o.cycle_number, o.app_id, o.game_title,
                       o.classification, o.payload_json, o.created_at_utc
                FROM notification_deliveries d
                JOIN notification_outbox o ON d.outbox_id = o.outbox_id
                ORDER BY d.delivery_id DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
            res = []
            for r in rows:
                d = dict(r)
                d["payload"] = json.loads(d.pop("payload_json", "{}") or "{}")
                res.append(d)
            return res

    def update_cycle_notification_status(self, cycle_number: int, status: str) -> None:
        with self.transaction() as conn:
            conn.execute(
                "UPDATE scan_cycles SET notification_status = ? WHERE cycle_number = ?",
                (status, cycle_number),
            )

    # --- Synchronization Tracking ---
    def record_sync(
        self,
        client_id: str,
        last_client_cycle: int,
        server_latest_cycle: int,
        cycles_returned: int,
        games_returned: int,
    ) -> None:
        with self.transaction() as conn:
            conn.execute(
                """
                INSERT INTO sync_history (
                    client_id, last_client_cycle, server_latest_cycle,
                    cycles_returned, games_returned, synced_at_utc, synced_at_ist
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    client_id,
                    last_client_cycle,
                    server_latest_cycle,
                    cycles_returned,
                    games_returned,
                    utc_iso(),
                    ist_iso(),
                ),
            )

    def export_full_backup(self) -> Dict[str, Any]:
        cycles, _ = self.list_cycles(limit=100000, offset=0)
        detailed_cycles = [self.get_cycle_detail(c["cycle_number"]) for c in cycles]
        return {
            "backup_version": 1,
            "schema_version": self.get_schema_version(),
            "exported_at_utc": utc_iso(),
            "exported_at_ist": ist_iso(),
            "last_cycle_number": self.get_last_cycle_number(),
            "stats": self.get_aggregate_stats(),
            "live_state": self.get_live_state(),
            "games_inventory": self.list_games(limit=100000),
            "scan_cycles": detailed_cycles,
            "notifications": self.list_notifications(limit=10000),
        }
