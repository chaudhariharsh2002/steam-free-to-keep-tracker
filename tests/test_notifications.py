"""Tests for transactional outbox notifications: delivery failure, partial delivery
(Telegram OK / ntfy FAIL), duplicate suppression, and missing configuration handling.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import httpx

from backend.config import TrackerConfig
from backend.database import TrackerDatabase
from backend.http_client import ResilientHTTPClient
from backend.notifications import NotificationDispatcher
from backend.worker import SteamTrackerWorker


SEARCH_HTML = """
<a class="search_result_row" data-ds-appid="777001" href="https://store.steampowered.com/app/777001/Star_Forge/">
  <span class="title">Star Forge</span>
</a>
"""


def _build_worker_transport(ntfy_status: int = 200, telegram_status: int = 200):
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "search/results" in url:
            return httpx.Response(200, json={"total_count": 1, "results_html": SEARCH_HTML})
        if "api/appdetails" in url:
            return httpx.Response(200, json={
                "777001": {
                    "success": True,
                    "data": {
                        "steam_appid": 777001,
                        "name": "Star Forge",
                        "type": "game",
                        "is_free": False,
                        "release_date": {"coming_soon": False},
                        "price_overview": {
                            "currency": "INR", "initial": 69900, "final": 0, "discount_percent": 100,
                            "initial_formatted": "₹ 699", "final_formatted": "₹ 0",
                        },
                        "package_groups": [],
                    },
                }
            })
        if "store.steampowered.com/app/777001" in url:
            return httpx.Response(200, text="<html>Free to keep when you get it before 2026-12-01.</html>")
        if "cheapshark.com" in url:
            return httpx.Response(200, json=[{
                "storeID": "1", "steamAppID": "777001", "title": "Star Forge",
                "normalPrice": "9.99", "salePrice": "0.00", "savings": "100", "isOnSale": "1", "dealID": "d1",
            }])
        if "gamerpower.com" in url:
            return httpx.Response(200, json=[])
        if "ntfy.sh" in url:
            return httpx.Response(ntfy_status, text="ntfy response")
        if "api.telegram.org" in url:
            return httpx.Response(telegram_status, json={"ok": telegram_status == 200})
        return httpx.Response(404)

    return httpx.MockTransport(handler)


class TestNotifications(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / "notif.db")
        self.db = TrackerDatabase(self.db_path)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_29_partial_notification_delivery_tracks_providers_independently(self):
        cfg = TrackerConfig(
            db_path=self.db_path,
            ntfy_topic="private-test-topic-99",
            telegram_bot_token="12345:TESTTOKEN",
            telegram_chat_id="987654",
            max_retries=0,
        )
        http = ResilientHTTPClient(max_retries=0, sleep_fn=lambda _: None, transport=_build_worker_transport(ntfy_status=500, telegram_status=200))
        summary = SteamTrackerWorker(cfg, self.db, http=http).run_cycle()
        self.assertIn("PARTIAL", summary["notification_status"])

        notifs = self.db.list_notifications()
        by_prov = {n["provider"]: n for n in notifs}
        self.assertEqual(by_prov["telegram"]["status"], "DELIVERED")
        self.assertEqual(by_prov["ntfy"]["status"], "FAILED")
        self.assertEqual(by_prov["ntfy"]["last_http_status"], 500)

    def test_30_notification_delivery_failure_recorded_for_retry(self):
        cfg = TrackerConfig(
            db_path=self.db_path,
            ntfy_topic="private-test-topic-99",
            max_retries=0,
        )
        http = ResilientHTTPClient(max_retries=0, sleep_fn=lambda _: None, transport=_build_worker_transport(ntfy_status=503))
        summary = SteamTrackerWorker(cfg, self.db, http=http).run_cycle()
        self.assertIn("FAILED", summary["notification_status"])
        notifs = self.db.list_notifications()
        self.assertEqual(notifs[0]["status"], "FAILED")
        self.assertEqual(notifs[0]["attempt_count"], 1)

    def test_31_duplicate_notifications_suppressed_across_repeated_scans(self):
        cfg = TrackerConfig(
            db_path=self.db_path,
            ntfy_topic="private-test-topic-99",
            telegram_bot_token="12345:TESTTOKEN",
            telegram_chat_id="987654",
            max_retries=0,
        )
        transport = _build_worker_transport(200, 200)
        SteamTrackerWorker(cfg, self.db, http=ResilientHTTPClient(max_retries=0, sleep_fn=lambda _: None, transport=transport)).run_cycle()
        SteamTrackerWorker(cfg, self.db, http=ResilientHTTPClient(max_retries=0, sleep_fn=lambda _: None, transport=transport)).run_cycle()
        notifs = self.db.list_notifications()
        # Only 2 delivery rows total (1 ntfy + 1 telegram), NOT 4!
        self.assertEqual(len(notifs), 2)

    def test_32_missing_notification_config_handled_gracefully(self):
        cfg = TrackerConfig(db_path=self.db_path, ntfy_topic="", telegram_bot_token="", telegram_chat_id="", max_retries=0)
        http = ResilientHTTPClient(max_retries=0, sleep_fn=lambda _: None, transport=_build_worker_transport(200, 200))
        summary = SteamTrackerWorker(cfg, self.db, http=http).run_cycle()
        self.assertEqual(summary["notification_status"], "UNCONFIGURED")
        self.assertEqual(len(self.db.list_notifications()), 0)


if __name__ == "__main__":
    unittest.main()
