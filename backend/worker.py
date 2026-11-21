"""Autonomous background scan engine with real-time progress publication,
concurrent-run locking, persistent cycle numbering, and strict execution outcomes.
"""
from __future__ import annotations

import json
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set

from backend.config import TrackerConfig
from backend.database import DatabaseError, TrackerDatabase
from backend.discovery import DiscoveryEngine
from backend.http_client import ResilientHTTPClient
from backend.models import (
    ClassificationStatus,
    DiscoveredCandidate,
    ExecutionOutcome,
    SecondaryEvidence,
    compute_next_scheduled_iso,
    ist_iso,
    utc_iso,
)
from backend.notifications import NotificationDispatcher, build_notification_payload
from backend.validator import TenGateValidator


class ScanAlreadyRunningError(RuntimeError):
    """Raised when a concurrent scan attempts to start while another scan holds the lock."""


class SteamTrackerWorker:
    """Executes one complete, auditable background tracking cycle."""

    def __init__(
        self,
        config: TrackerConfig,
        db: TrackerDatabase,
        http: Optional[ResilientHTTPClient] = None,
    ) -> None:
        self.config = config
        self.db = db
        self._owns_http = http is None
        self.http = http or ResilientHTTPClient(
            timeout_seconds=config.request_timeout_seconds,
            max_retries=config.max_retries,
            backoff_base_seconds=config.backoff_base_seconds,
            user_agent=config.user_agent,
        )
        self.discovery = DiscoveryEngine(config, self.http)
        self.validator = TenGateValidator(config)
        self.notifier = NotificationDispatcher(config, db, self.http)

    def close(self) -> None:
        if self._owns_http:
            self.http.close()

    def run_cycle(self) -> Dict[str, Any]:
        scan_id = f"scan-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:6]}"
        if not self.db.acquire_scan_lock(scan_id, self.config.lock_stale_after_seconds):
            raise ScanAlreadyRunningError("Another background scan is currently running.")

        started_dt = datetime.now(timezone.utc)
        started_u = utc_iso(started_dt)
        started_i = ist_iso(started_dt)
        next_sched_u = compute_next_scheduled_iso(self.config.scan_interval_minutes, started_dt)
        start_mono = time.monotonic()

        cycle_number = self.db.allocate_next_cycle_number()

        def emit(event_type: str, message: str, data: Optional[Dict[str, Any]] = None) -> None:
            payload = data or {}
            self.db.append_live_event(
                event_type=event_type,
                message=message,
                scan_id=scan_id,
                cycle_number=cycle_number,
                source=payload.get("source"),
                app_id=payload.get("app_id"),
                payload=payload,
            )

        try:
            self.db.create_scan_cycle(
                cycle_number=cycle_number,
                scan_id=scan_id,
                region=self.config.target_region,
                currency=self.config.default_currency,
                started_at_utc=started_u,
                started_at_ist=started_i,
                next_scheduled_at_utc=next_sched_u,
            )

            self.db.update_live_state(
                status=ExecutionOutcome.RUNNING.value,
                current_cycle_number=cycle_number,
                current_scan_id=scan_id,
                scan_started_at_utc=started_u,
                next_scheduled_scan_utc=next_sched_u,
                current_source="steam_search",
                current_operation="Initializing background worker",
                steam_search_requests=0,
                pages_retrieved=0,
                current_page=0,
                total_pages=None,
                candidates_discovered=0,
                unique_app_ids=0,
                appdetails_requests=0,
                candidates_waiting=0,
                secondary_requests=0,
                completed_requests=0,
                failed_requests=0,
                retried_requests=0,
                current_game_json=None,
            )
            emit("WORKER_STARTED", f"Background worker started Cycle #{cycle_number} ({scan_id})", {"region": self.config.target_region})

            # Phase 1: Discovery across Steam Store + Secondary Feeds
            def discovery_cb(et: str, msg: str, data: Dict[str, Any]) -> None:
                emit(et, msg, data)
                updates: Dict[str, Any] = {
                    "current_source": data.get("source") or "steam_search",
                    "current_operation": msg[:180],
                    "completed_requests": self.http.telemetry.completed_requests,
                    "failed_requests": self.http.telemetry.failed_requests,
                    "retried_requests": self.http.telemetry.retried_requests,
                }
                if "current_page" in data:
                    updates["current_page"] = data["current_page"]
                if "pages_retrieved" in data:
                    updates["pages_retrieved"] = data["pages_retrieved"]
                if "total_pages" in data:
                    updates["total_pages"] = data["total_pages"]
                self.db.update_live_state(**updates)

            disc = self.discovery.discover_all(emit=discovery_cb)

            # Deduplicate candidates for queue telemetry while preserving duplicate testability
            # (Steam search candidates take priority over secondary discovery candidates)
            unique_app_ids = {c.app_id for c in disc.candidates if c.app_id is not None}
            self.db.update_live_state(
                steam_search_requests=disc.steam_search_requests,
                pages_retrieved=disc.pages_retrieved,
                total_pages=disc.total_pages_exposed,
                candidates_discovered=len(disc.candidates),
                unique_app_ids=len(unique_app_ids),
                candidates_waiting=len(disc.candidates),
                secondary_requests=disc.secondary_requests,
                completed_requests=self.http.telemetry.completed_requests,
                failed_requests=self.http.telemetry.failed_requests,
                retried_requests=self.http.telemetry.retried_requests,
                current_operation=f"Discovery complete: {len(disc.candidates)} candidate(s), {len(unique_app_ids)} unique App ID(s)",
            )
            emit(
                "DISCOVERY_COMPLETED",
                f"Discovery complete: {len(disc.candidates)} candidate(s), {len(unique_app_ids)} unique App ID(s), {disc.pages_retrieved} page(s)",
                {
                    "candidates_discovered": len(disc.candidates),
                    "unique_app_ids": len(unique_app_ids),
                    "pages_retrieved": disc.pages_retrieved,
                },
            )

            # Phase 2: Individual Game Verification (10-Gate Framework)
            evaluations: List[Dict[str, Any]] = []
            seen_app_ids: Set[int] = set()
            appdetails_requests = 0
            all_errors = list(disc.errors)
            rejection_reasons: List[str] = []
            quarantine_reasons: List[str] = []
            outbox_items: List[Dict[str, Any]] = []

            total_queue = len(disc.candidates)
            for idx, cand in enumerate(disc.candidates, start=1):
                remaining = total_queue - idx
                emit(
                    "CANDIDATE_CHECK_STARTED",
                    f"Checking candidate {idx}/{total_queue}: {cand.title} (App ID: {cand.app_id or 'N/A'})",
                    {"source": cand.source, "app_id": cand.app_id, "title": cand.title},
                )

                appdetails: Optional[Dict[str, Any]] = None
                appdetails_err: Optional[str] = None
                store_signals: Optional[Dict[str, Any]] = None

                # Skip duplicate Steam API fetches if already seen in this cycle (Gate 6 will mark duplicate)
                if cand.app_id is not None and cand.app_id not in seen_app_ids:
                    appdetails_requests += 1
                    self.db.update_live_state(
                        current_source="steam_appdetails",
                        current_operation=f"Fetching Steam appdetails for App ID {cand.app_id} ({cand.title})",
                        appdetails_requests=appdetails_requests,
                        candidates_waiting=remaining,
                        current_game_json=json.dumps({
                            "title": cand.title,
                            "app_id": cand.app_id,
                            "original_price": cand.search_original_price or "Checking...",
                            "current_price": cand.search_final_price or "Checking...",
                            "discount_percent": cand.search_discount_pct,
                            "source": "steam_appdetails",
                            "gate": "Gate 1-5: Fetching Steam AppDetails",
                            "outcome": "CHECKING",
                            "reason": "",
                        }),
                    )
                    appdetails, appdetails_err = self.discovery.fetch_steam_appdetails(cand.app_id)
                    if appdetails_err:
                        all_errors.append(f"AppDetails {cand.app_id}: {appdetails_err}")

                    # Optional Steam store page check for permanent-retention wording & expiration date
                    if self.config.enable_steam_store_page_check and appdetails and not cand.is_third_party_giveaway:
                        store_signals, sp_err = self.discovery.fetch_steam_store_page_signals(cand.app_id)
                        if sp_err:
                            all_errors.append(f"StorePage {cand.app_id}: {sp_err}")

                # Gather independent secondary evidence
                sec_list: List[SecondaryEvidence] = []
                if cand.app_id is not None and cand.app_id in disc.secondary_by_appid:
                    sec_list.extend(disc.secondary_by_appid[cand.app_id])
                elif cand.title and cand.title.lower() in disc.secondary_by_title:
                    sec_list.extend(disc.secondary_by_title[cand.title.lower()])

                record = self.validator.evaluate(
                    candidate=cand,
                    appdetails=appdetails,
                    appdetails_error=appdetails_err,
                    store_page_signals=store_signals,
                    secondary_evidence=sec_list,
                    seen_app_ids_in_cycle=seen_app_ids,
                )
                rec_dict = record.to_dict()
                evaluations.append(rec_dict)
                rejection_reasons.extend(record.rejection_reasons)
                quarantine_reasons.extend(record.quarantine_reasons)

                # Update live monitor with actual verification result
                self.db.update_live_state(
                    current_source=record.primary_source,
                    current_operation=f"Validated {record.title}: {record.final_status.value}",
                    appdetails_requests=appdetails_requests,
                    candidates_waiting=remaining,
                    completed_requests=self.http.telemetry.completed_requests,
                    failed_requests=self.http.telemetry.failed_requests,
                    retried_requests=self.http.telemetry.retried_requests,
                    current_game_json=json.dumps({
                        "title": record.title,
                        "app_id": record.app_id,
                        "original_price": record.original_price_formatted,
                        "current_price": record.current_price_formatted,
                        "discount_percent": record.discount_percent,
                        "source": record.primary_source,
                        "gate": "Gate 10: Final Evidence-Based Decision",
                        "outcome": record.final_status.value,
                        "reason": record.decision_reason,
                    }),
                )

                ev_type = (
                    "GAME_VERIFIED"
                    if record.final_status == ClassificationStatus.VERIFIED
                    else ("GAME_QUARANTINED" if record.final_status == ClassificationStatus.QUARANTINED else "GAME_REJECTED")
                )
                emit(
                    ev_type,
                    f"{record.final_status.value}: {record.title} (App ID {record.app_id or 'N/A'}) — {record.decision_reason}",
                    {
                        "app_id": record.app_id,
                        "title": record.title,
                        "status": record.final_status.value,
                        "original_price": record.original_price_formatted,
                        "current_price": record.current_price_formatted,
                    },
                )

                # Enqueue notification only for VERIFIED (or QUARANTINED if explicitly opted in)
                if record.final_status == ClassificationStatus.VERIFIED or (
                    record.final_status == ClassificationStatus.QUARANTINED and self.config.notify_quarantined
                ):
                    # Dedupe key scoped to App ID + classification + active promo period (YYYY-MM)
                    # Prevents duplicate alerts across hourly scans while allowing future re-promos
                    promo_bucket = (record.expiration_at or started_u[:7]).strip()
                    dedupe_key = f"{record.final_status.value}:{record.app_id or record.title.lower()}:{promo_bucket}"
                    outbox_items.append(
                        {
                            "dedupe_key": dedupe_key,
                            "app_id": record.app_id,
                            "game_title": record.title,
                            "classification": record.final_status.value,
                            "payload": build_notification_payload(rec_dict),
                        }
                    )

            verified_count = sum(1 for e in evaluations if e["final_status"] == "VERIFIED")
            quarantined_count = sum(1 for e in evaluations if e["final_status"] == "QUARANTINED")
            rejected_count = sum(1 for e in evaluations if e["final_status"] == "REJECTED")

            # Strict execution outcome classification (never report a failed scan as SUCCESS_NO_RESULTS)
            primary_failed = disc.pages_retrieved == 0 and any("Steam search" in e for e in all_errors)
            if primary_failed and len(evaluations) == 0:
                outcome = ExecutionOutcome.FAILED
            elif all_errors:
                outcome = ExecutionOutcome.COMPLETED_WITH_ERRORS
            elif verified_count > 0:
                outcome = ExecutionOutcome.SUCCESS_WITH_RESULTS
            else:
                outcome = ExecutionOutcome.SUCCESS_NO_RESULTS

            completed_dt = datetime.now(timezone.utc)
            completed_u = utc_iso(completed_dt)
            completed_i = ist_iso(completed_dt)
            duration_s = round(time.monotonic() - start_mono, 2)

            providers = self.config.configured_notification_providers()
            notif_status = "PENDING" if (outbox_items and providers) else ("UNCONFIGURED" if outbox_items else "NONE")

            summary = {
                "cycle_number": cycle_number,
                "scan_id": scan_id,
                "status": outcome.value,
                "started_at_utc": started_u,
                "started_at_ist": started_i,
                "completed_at_utc": completed_u,
                "completed_at_ist": completed_i,
                "duration_seconds": duration_s,
                "sources_checked": disc.sources_checked,
                "steam_catalogue_size": disc.steam_catalogue_size,
                "filtered_promo_total": disc.filtered_promo_total,
                "pages_retrieved": disc.pages_retrieved,
                "total_pages_exposed": disc.total_pages_exposed,
                "candidates_discovered": len(disc.candidates),
                "unique_app_ids": len(unique_app_ids),
                "games_checked": len(evaluations),
                "verified_count": verified_count,
                "quarantined_count": quarantined_count,
                "rejected_count": rejected_count,
                "steam_search_requests": disc.steam_search_requests,
                "appdetails_requests": appdetails_requests,
                "secondary_requests": disc.secondary_requests,
                "completed_requests": self.http.telemetry.completed_requests,
                "failed_requests": self.http.telemetry.failed_requests,
                "retried_requests": self.http.telemetry.retried_requests,
                "api_response_times": self.http.telemetry.average_latencies(),
                "errors": all_errors,
                "rejection_reasons": list(dict.fromkeys(rejection_reasons)),
                "quarantine_reasons": list(dict.fromkeys(quarantine_reasons)),
                "notification_status": notif_status,
                "publication_status": "PUBLISHED",
                "sync_status": "READY",
                "data_freshness": "FRESH" if outcome != ExecutionOutcome.FAILED else "STALE",
                "coverage_disclosure": disc.coverage_disclosure,
                "next_scheduled_at_utc": next_sched_u,
            }

            self.db.finalize_scan_cycle(
                cycle_number=cycle_number,
                scan_id=scan_id,
                summary=summary,
                evaluations=evaluations,
                outbox_items=outbox_items,
                providers=providers,
                notification_max_retries=self.config.notification_max_retries,
            )
            emit("HISTORICAL_RECORD_SAVED", f"Saved Cycle #{cycle_number} to permanent audit ledger ({outcome.value})", {"cycle_number": cycle_number})

            # Phase 3: Dispatch transactional outbox notifications independently per provider
            notif_stats = self.notifier.dispatch_pending(emit_cb=emit)
            if notif_stats["attempted"] > 0:
                if notif_stats["failed"] == 0:
                    final_notif_status = f"DELIVERED ({notif_stats['delivered']})"
                elif notif_stats["delivered"] > 0:
                    final_notif_status = f"PARTIAL ({notif_stats['delivered']} ok, {notif_stats['failed']} failed)"
                else:
                    final_notif_status = f"FAILED ({notif_stats['failed']} failed)"
                self.db.update_cycle_notification_status(cycle_number, final_notif_status)
                summary["notification_status"] = final_notif_status

            # Update final live state -> IDLE (or FAILED if scan failed)
            final_live_status = ExecutionOutcome.FAILED.value if outcome == ExecutionOutcome.FAILED else ExecutionOutcome.IDLE.value
            live_updates: Dict[str, Any] = {
                "status": final_live_status,
                "last_completed_scan_utc": completed_u,
                "next_scheduled_scan_utc": next_sched_u,
                "current_operation": f"Cycle #{cycle_number} finished: {outcome.value}",
                "candidates_waiting": 0,
                "completed_requests": self.http.telemetry.completed_requests,
                "failed_requests": self.http.telemetry.failed_requests,
                "retried_requests": self.http.telemetry.retried_requests,
            }
            if outcome in (
                ExecutionOutcome.SUCCESS_WITH_RESULTS,
                ExecutionOutcome.SUCCESS_NO_RESULTS,
                ExecutionOutcome.COMPLETED_WITH_ERRORS,
            ):
                live_updates["last_successful_scan_utc"] = completed_u
            self.db.update_live_state(**live_updates)

            emit(
                "SCAN_COMPLETED",
                f"Cycle #{cycle_number} completed ({outcome.value}): {verified_count} verified, {quarantined_count} quarantined, {rejected_count} rejected in {duration_s}s",
                {"cycle_number": cycle_number, "outcome": outcome.value, "duration_seconds": duration_s},
            )
            return summary

        except DatabaseError:
            # Propagate DB write failures cleanly without masking them
            raise
        except Exception as exc:
            completed_u = utc_iso()
            completed_i = ist_iso()
            duration_s = round(time.monotonic() - start_mono, 2)
            fail_summary = {
                "cycle_number": cycle_number,
                "scan_id": scan_id,
                "status": ExecutionOutcome.FAILED.value,
                "started_at_utc": started_u,
                "started_at_ist": started_i,
                "completed_at_utc": completed_u,
                "completed_at_ist": completed_i,
                "duration_seconds": duration_s,
                "errors": [f"Unhandled worker exception: {exc}"],
                "next_scheduled_at_utc": next_sched_u,
                "data_freshness": "STALE",
            }
            try:
                self.db.finalize_scan_cycle(cycle_number, scan_id, fail_summary, [], [], [])
                self.db.update_live_state(
                    status=ExecutionOutcome.FAILED.value,
                    last_completed_scan_utc=completed_u,
                    current_operation=f"Cycle #{cycle_number} FAILED: {exc}",
                )
                emit("SCAN_FAILED", f"Cycle #{cycle_number} failed: {exc}", {"error": str(exc)})
            except Exception:
                pass
            return fail_summary
        finally:
            try:
                self.db.release_scan_lock(scan_id)
            except Exception:
                pass
