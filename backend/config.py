"""Configuration management for Steam Free-to-Keep Tracker.

All secrets (Telegram tokens, ntfy auth tokens, trigger API keys) are loaded strictly
from environment variables and never hardcoded or exposed to frontend assets.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import List


@dataclass(frozen=True)
class TrackerConfig:
    # Target storefront & locale defaults (India / INR / IST)
    target_region: str = "IN"
    steam_country_code: str = "in"
    steam_language: str = "english"
    default_currency: str = "INR"
    timezone: str = "Asia/Kolkata"
    scan_interval_minutes: int = 60

    # Storage & persistence
    db_path: str = "data/steam_tracker.db"
    pdf_cache_dir: str = "data/reports"
    backup_dir: str = "data/backups"

    # HTTP & resilience
    request_timeout_seconds: float = 20.0
    max_retries: int = 3
    backoff_base_seconds: float = 1.5
    max_search_pages: int = 50
    search_page_size: int = 50
    user_agent: str = (
        "SteamFreeToKeepTracker/1.0 (+https://github.com/steam-ftk-tracker; respectful-crawler)"
    )

    # Secondary source toggles
    enable_cheapshark: bool = True
    enable_gamerpower: bool = True
    enable_steam_store_page_check: bool = True

    # Notifications (secrets strictly from env)
    ntfy_server_url: str = "https://ntfy.sh"
    ntfy_topic: str = ""
    ntfy_token: str = ""
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    notify_quarantined: bool = False
    notification_max_retries: int = 5

    # API & Security
    host: str = "0.0.0.0"
    port: int = 8000
    trigger_api_key: str = ""
    cors_origins: List[str] = field(default_factory=lambda: ["*"])
    lock_stale_after_seconds: int = 1800  # 30 minutes safety timeout for crashed worker

    @classmethod
    def from_env(cls, db_path_override: str | None = None) -> "TrackerConfig":
        def _bool(name: str, default: bool) -> bool:
            val = os.getenv(name)
            if val is None:
                return default
            return val.strip().lower() in ("1", "true", "yes", "on")

        def _int(name: str, default: int) -> int:
            val = os.getenv(name)
            if val is None or not val.strip():
                return default
            try:
                return int(val.strip())
            except ValueError:
                return default

        def _float(name: str, default: float) -> float:
            val = os.getenv(name)
            if val is None or not val.strip():
                return default
            try:
                return float(val.strip())
            except ValueError:
                return default

        origins_raw = os.getenv("CORS_ORIGINS", "*").strip()
        origins = [o.strip() for o in origins_raw.split(",") if o.strip()] or ["*"]

        return cls(
            target_region=os.getenv("TARGET_REGION", "IN").upper(),
            steam_country_code=os.getenv("STEAM_COUNTRY_CODE", "in").lower(),
            steam_language=os.getenv("STEAM_LANGUAGE", "english").lower(),
            default_currency=os.getenv("DEFAULT_CURRENCY", "INR").upper(),
            timezone=os.getenv("TIMEZONE", "Asia/Kolkata"),
            scan_interval_minutes=max(5, _int("SCAN_INTERVAL_MINUTES", 60)),
            db_path=db_path_override or os.getenv("DATABASE_PATH", "data/steam_tracker.db"),
            pdf_cache_dir=os.getenv("PDF_CACHE_DIR", "data/reports"),
            backup_dir=os.getenv("BACKUP_DIR", "data/backups"),
            request_timeout_seconds=_float("REQUEST_TIMEOUT_SECONDS", 20.0),
            max_retries=max(0, _int("MAX_RETRIES", 3)),
            backoff_base_seconds=_float("BACKOFF_BASE_SECONDS", 1.5),
            max_search_pages=max(1, _int("MAX_SEARCH_PAGES", 50)),
            search_page_size=min(100, max(10, _int("SEARCH_PAGE_SIZE", 50))),
            enable_cheapshark=_bool("ENABLE_CHEAPSHARK", True),
            enable_gamerpower=_bool("ENABLE_GAMERPOWER", True),
            enable_steam_store_page_check=_bool("ENABLE_STEAM_STORE_PAGE_CHECK", True),
            ntfy_server_url=os.getenv("NTFY_SERVER_URL", "https://ntfy.sh").rstrip("/"),
            ntfy_topic=os.getenv("NTFY_TOPIC", "").strip(),
            ntfy_token=os.getenv("NTFY_TOKEN", "").strip(),
            telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", "").strip(),
            telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID", "").strip(),
            notify_quarantined=_bool("NOTIFY_QUARANTINED", False),
            notification_max_retries=max(1, _int("NOTIFICATION_MAX_RETRIES", 5)),
            host=os.getenv("HOST", "0.0.0.0"),
            port=_int("PORT", 8000),
            trigger_api_key=os.getenv("TRIGGER_API_KEY", "").strip(),
            cors_origins=origins,
            lock_stale_after_seconds=max(120, _int("LOCK_STALE_AFTER_SECONDS", 1800)),
        )

    def ensure_directories(self) -> None:
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        Path(self.pdf_cache_dir).mkdir(parents=True, exist_ok=True)
        Path(self.backup_dir).mkdir(parents=True, exist_ok=True)

    def configured_notification_providers(self) -> List[str]:
        providers: List[str] = []
        if self.ntfy_topic:
            providers.append("ntfy")
        if self.telegram_bot_token and self.telegram_chat_id:
            providers.append("telegram")
        return providers

    def public_summary(self) -> dict:
        """Safe non-secret configuration view for the dashboard Settings page."""
        return {
            "target_region": self.target_region,
            "steam_country_code": self.steam_country_code,
            "default_currency": self.default_currency,
            "timezone": self.timezone,
            "scan_interval_minutes": self.scan_interval_minutes,
            "enable_cheapshark": self.enable_cheapshark,
            "enable_gamerpower": self.enable_gamerpower,
            "enable_steam_store_page_check": self.enable_steam_store_page_check,
            "ntfy_configured": bool(self.ntfy_topic),
            "ntfy_server_url": self.ntfy_server_url,
            "telegram_configured": bool(self.telegram_bot_token and self.telegram_chat_id),
            "notify_quarantined": self.notify_quarantined,
            "trigger_auth_enabled": bool(self.trigger_api_key),
        }
