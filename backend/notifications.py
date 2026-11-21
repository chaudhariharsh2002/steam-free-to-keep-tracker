"""Independent background phone notification dispatcher with transactional outbox,
per-provider delivery tracking (ntfy.sh & Telegram Bot API), exponential backoff retries,
and duplicate suppression.

Provider policies (verified):
- ntfy.sh free tier: 250 messages/day, caches messages for 12 hours by default for offline Android
  delivery via Firebase Cloud Messaging (https://docs.ntfy.sh/privacy/).
- Telegram Bot API: free, ~1 msg/sec per chat, cloud-retained history for offline devices
  (https://core.telegram.org/bots/faq).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from backend.config import TrackerConfig
from backend.database import TrackerDatabase
from backend.http_client import ResilientHTTPClient, ResilientHTTPError
from backend.models import format_ist_human, utc_iso


def build_notification_payload(ev: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "title": ev["title"],
        "app_id": ev.get("app_id"),
        "original_price": ev["original_price_formatted"],
        "current_price": ev["current_price_formatted"],
        "discount_percent": ev.get("discount_percent"),
        "claim_url": ev["claim_url"],
        "discovered_at_utc": ev["checked_at_utc"],
        "discovered_at_ist": format_ist_human(ev["checked_at_utc"]),
        "verification_status": ev["final_status"],
        "expiration_at": ev.get("expiration_at") or "Not disclosed by Steam",
        "region": ev["region"],
    }


def format_notification_body(payload: Dict[str, Any]) -> str:
    status_label = (
        "VERIFIED FREE-TO-KEEP"
        if payload.get("verification_status") == "VERIFIED"
        else f"QUARANTINED ({payload.get('verification_status')})"
    )
    lines = [
        f"Status: {status_label}",
        f"Game: {payload.get('title')}",
        f"App ID: {payload.get('app_id') or 'N/A'}",
        f"Original Price: {payload.get('original_price')}",
        f"Current Price: {payload.get('current_price')} ({payload.get('discount_percent') or 0}% OFF)",
        f"Region: {payload.get('region', 'IN')}",
        f"Expires: {payload.get('expiration_at', 'Unknown')}",
        f"Discovered: {payload.get('discovered_at_ist', payload.get('discovered_at_utc'))}",
        f"Claim on Steam: {payload.get('claim_url')}",
    ]
    return "\n".join(lines)


class NotificationDispatcher:
    """Processes pending outbox deliveries independently per provider."""

    def __init__(
        self,
        config: TrackerConfig,
        db: TrackerDatabase,
        http: ResilientHTTPClient,
    ) -> None:
        self.config = config
        self.db = db
        self.http = http

    def dispatch_pending(self, emit_cb=None) -> Dict[str, int]:
        pending = self.db.fetch_pending_deliveries(limit=100)
        stats = {"attempted": 0, "delivered": 0, "failed": 0, "skipped_unconfigured": 0}

        for row in pending:
            provider = row["provider"]
            delivery_id = row["delivery_id"]
            payload = row["payload"]
            attempt_num = int(row["attempt_count"]) + 1
            stats["attempted"] += 1

            ok, http_status, err = self._send_single(provider, payload)
            if ok:
                stats["delivered"] += 1
                self.db.update_delivery_result(
                    delivery_id=delivery_id,
                    delivered=True,
                    http_status=http_status,
                    error=None,
                    next_retry_at_utc=None,
                )
                if emit_cb:
                    emit_cb(
                        "NOTIFICATION_DISPATCHED",
                        f"Delivered {provider} alert for {payload.get('title')} (App ID {payload.get('app_id')})",
                        {"provider": provider, "app_id": payload.get("app_id"), "status": "DELIVERED"},
                    )
            else:
                stats["failed"] += 1
                # Exponential retry backoff: 2m, 4m, 8m, 16m...
                backoff_minutes = min(60, 2 ** attempt_num)
                next_retry = utc_iso(datetime.now(timezone.utc) + timedelta(minutes=backoff_minutes))
                self.db.update_delivery_result(
                    delivery_id=delivery_id,
                    delivered=False,
                    http_status=http_status,
                    error=err,
                    next_retry_at_utc=next_retry,
                )
                if emit_cb:
                    emit_cb(
                        "NOTIFICATION_FAILED",
                        f"Failed {provider} alert for {payload.get('title')}: {err}",
                        {"provider": provider, "app_id": payload.get("app_id"), "status": "FAILED"},
                    )

        return stats

    def _send_single(self, provider: str, payload: Dict[str, Any]) -> tuple[bool, Optional[int], Optional[str]]:
        if provider == "ntfy":
            return self._send_ntfy(payload)
        if provider == "telegram":
            return self._send_telegram(payload)
        return False, None, f"Unsupported notification provider: {provider}"

    def _send_ntfy(self, payload: Dict[str, Any]) -> tuple[bool, Optional[int], Optional[str]]:
        if not self.config.ntfy_topic:
            return False, None, "ntfy topic not configured (NTFY_TOPIC missing)"
        url = f"{self.config.ntfy_server_url}/{self.config.ntfy_topic}"
        title_prefix = (
            "FREE TO KEEP" if payload.get("verification_status") == "VERIFIED" else "QUARANTINED"
        )
        headers: Dict[str, str] = {
            "Title": f"{title_prefix}: {payload.get('title', 'Steam Game')}",
            "Priority": "high" if payload.get("verification_status") == "VERIFIED" else "default",
            "Tags": "video_game,tada" if payload.get("verification_status") == "VERIFIED" else "warning",
            "Click": str(payload.get("claim_url") or "https://store.steampowered.com/"),
            "Actions": f"view, Open on Steam, {payload.get('claim_url') or 'https://store.steampowered.com/'}",
        }
        if self.config.ntfy_token:
            headers["Authorization"] = f"Bearer {self.config.ntfy_token}"
        body = format_notification_body(payload).encode("utf-8")
        try:
            resp = self.http.request("POST", url, source="ntfy", headers=headers, content=body)
            return True, resp.status_code, None
        except ResilientHTTPError as exc:
            return False, exc.status_code, str(exc)

    def _send_telegram(self, payload: Dict[str, Any]) -> tuple[bool, Optional[int], Optional[str]]:
        if not self.config.telegram_bot_token or not self.config.telegram_chat_id:
            return False, None, "Telegram bot token or chat ID not configured"
        url = f"https://api.telegram.org/bot{self.config.telegram_bot_token}/sendMessage"
        body = {
            "chat_id": self.config.telegram_chat_id,
            "text": format_notification_body(payload),
            "disable_web_page_preview": False,
        }
        try:
            resp = self.http.request("POST", url, source="telegram", json_body=body)
            return True, resp.status_code, None
        except ResilientHTTPError as exc:
            return False, exc.status_code, str(exc)
