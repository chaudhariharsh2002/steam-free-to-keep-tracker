"""Tests for HTTP resilience (429, timeout, malformed JSON), API failures (Steam, CheapShark,
GamerPower), DB write failure, concurrent scan execution lock, and cycle counter persistence.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from backend.config import TrackerConfig
from backend.database import DatabaseError, TrackerDatabase
from backend.http_client import ResilientHTTPClient, ResilientHTTPError
from backend.models import ExecutionOutcome
from backend.worker import ScanAlreadyRunningError, SteamTrackerWorker


SAMPLE_SEARCH_HTML = """
<a class="search_result_row" data-ds-appid="555001" href="https://store.steampowered.com/app/555001/Chrono_Quest/">
  <span class="title">Chrono Quest</span>
  <div class="discount_pct">-100%</div>
  <div class="discount_original_price">₹ 999</div>
  <div class="discount_final_price">₹ 0</div>
</a>
"""


def _mock_transport(routes: dict):
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        for prefix, responder in routes.items():
            if prefix in url:
                if callable(responder):
                    return responder(request)
                return responder
        return httpx.Response(404, json={"error": "not found"})

    return httpx.MockTransport(handler)


class TestResilienceAndWorker(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / "tracker.db")
        self.config = TrackerConfig(
            db_path=self.db_path,
            max_retries=2,
            backoff_base_seconds=0.01,
            enable_steam_store_page_check=True,
        )
        self.db = TrackerDatabase(self.db_path)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_20_steam_api_failure_marks_scan_failed_not_zero_success(self):
        transport = _mock_transport({
            "store.steampowered.com/search/results": httpx.Response(503, text="Service Unavailable"),
            "cheapshark.com": httpx.Response(200, json=[]),
            "gamerpower.com": httpx.Response(200, json=[]),
        })
        http = ResilientHTTPClient(max_retries=1, backoff_base_seconds=0.001, sleep_fn=lambda _: None, transport=transport)
        worker = SteamTrackerWorker(self.config, self.db, http=http)
        summary = worker.run_cycle()
        self.assertEqual(summary["status"], ExecutionOutcome.FAILED.value)
        self.assertNotEqual(summary["status"], ExecutionOutcome.SUCCESS_NO_RESULTS.value)
        self.assertGreater(len(summary["errors"]), 0)

    def test_21_cheapshark_api_failure_records_error_gracefully(self):
        transport = _mock_transport({
            "store.steampowered.com/search/results": httpx.Response(200, json={"total_count": 0, "results_html": ""}),
            "cheapshark.com": httpx.Response(500, text="Internal Server Error"),
            "gamerpower.com": httpx.Response(200, json=[]),
        })
        http = ResilientHTTPClient(max_retries=1, backoff_base_seconds=0.001, sleep_fn=lambda _: None, transport=transport)
        worker = SteamTrackerWorker(self.config, self.db, http=http)
        summary = worker.run_cycle()
        self.assertEqual(summary["status"], ExecutionOutcome.COMPLETED_WITH_ERRORS.value)
        self.assertTrue(any("CheapShark" in e for e in summary["errors"]))

    def test_22_gamerpower_api_failure_records_error_gracefully(self):
        transport = _mock_transport({
            "store.steampowered.com/search/results": httpx.Response(200, json={"total_count": 0, "results_html": ""}),
            "cheapshark.com": httpx.Response(200, json=[]),
            "gamerpower.com": httpx.Response(502, text="Bad Gateway"),
        })
        http = ResilientHTTPClient(max_retries=1, backoff_base_seconds=0.001, sleep_fn=lambda _: None, transport=transport)
        worker = SteamTrackerWorker(self.config, self.db, http=http)
        summary = worker.run_cycle()
        self.assertEqual(summary["status"], ExecutionOutcome.COMPLETED_WITH_ERRORS.value)
        self.assertTrue(any("GamerPower" in e for e in summary["errors"]))

    def test_23_http_429_rate_limit_retries_with_retry_after(self):
        calls = {"n": 0}

        def responder(_req):
            calls["n"] += 1
            if calls["n"] == 1:
                return httpx.Response(429, headers={"Retry-After": "1"}, text="Too Many Requests")
            return httpx.Response(200, json={"ok": True})

        slept = []
        http = ResilientHTTPClient(
            max_retries=2,
            backoff_base_seconds=0.01,
            sleep_fn=lambda d: slept.append(d),
            transport=_mock_transport({"example.test": responder}),
        )
        data, _ = http.get_json("https://example.test/api", source="test_src")
        self.assertEqual(data, {"ok": True})
        self.assertEqual(http.telemetry.retried_requests, 1)
        self.assertGreaterEqual(slept[0], 1.0)

    def test_24_timeout_retries_and_raises_resilient_error(self):
        def timeout_handler(_req):
            raise httpx.ReadTimeout("timed out")

        http = ResilientHTTPClient(
            max_retries=1,
            backoff_base_seconds=0.001,
            sleep_fn=lambda _: None,
            transport=_mock_transport({"timeout.test": timeout_handler}),
        )
        with self.assertRaises(ResilientHTTPError):
            http.get_json("https://timeout.test/api", source="timeout_src")
        self.assertEqual(http.telemetry.retried_requests, 1)
        self.assertEqual(http.telemetry.failed_requests, 1)

    def test_25_malformed_json_handled_cleanly(self):
        http = ResilientHTTPClient(
            max_retries=0,
            sleep_fn=lambda _: None,
            transport=_mock_transport({"badjson.test": httpx.Response(200, text="{not valid json")}),
        )
        with self.assertRaises(ResilientHTTPError) as ctx:
            http.get_json("https://badjson.test/api", source="bad_json")
        self.assertIn("Malformed JSON", str(ctx.exception))

    def test_26_database_write_failure_raises_database_error(self):
        with patch.object(self.db, "finalize_scan_cycle", side_effect=DatabaseError("Disk full")):
            transport = _mock_transport({
                "store.steampowered.com/search/results": httpx.Response(200, json={"total_count": 0, "results_html": ""}),
                "cheapshark.com": httpx.Response(200, json=[]),
                "gamerpower.com": httpx.Response(200, json=[]),
            })
            http = ResilientHTTPClient(max_retries=0, sleep_fn=lambda _: None, transport=transport)
            worker = SteamTrackerWorker(self.config, self.db, http=http)
            with self.assertRaises(DatabaseError):
                worker.run_cycle()

    def test_27_duplicate_scan_execution_blocked_by_lock(self):
        acquired = self.db.acquire_scan_lock("scan-active-001", stale_after_seconds=600)
        self.assertTrue(acquired)
        worker = SteamTrackerWorker(self.config, self.db)
        with self.assertRaises(ScanAlreadyRunningError):
            worker.run_cycle()
        self.db.release_scan_lock("scan-active-001")

    def test_28_cycle_counter_persists_across_restarts(self):
        transport = _mock_transport({
            "store.steampowered.com/search/results": httpx.Response(200, json={"total_count": 1, "results_html": SAMPLE_SEARCH_HTML}),
            "store.steampowered.com/api/appdetails": httpx.Response(200, json={
                "555001": {
                    "success": True,
                    "data": {
                        "steam_appid": 555001,
                        "name": "Chrono Quest",
                        "type": "game",
                        "is_free": False,
                        "release_date": {"coming_soon": False},
                        "price_overview": {
                            "currency": "INR", "initial": 99900, "final": 0, "discount_percent": 100,
                            "initial_formatted": "₹ 999", "final_formatted": "₹ 0",
                        },
                        "package_groups": [],
                    },
                }
            }),
            "store.steampowered.com/app/555001": httpx.Response(200, text="<html>Free to keep when you get it before 30 Nov @ 10:00am.</html>"),
            "cheapshark.com": httpx.Response(200, json=[{
                "storeID": "1", "steamAppID": "555001", "title": "Chrono Quest",
                "normalPrice": "14.99", "salePrice": "0.00", "savings": "100.00", "isOnSale": "1", "dealID": "abc",
            }]),
            "gamerpower.com": httpx.Response(200, json=[]),
        })
        http1 = ResilientHTTPClient(max_retries=0, sleep_fn=lambda _: None, transport=transport)
        s1 = SteamTrackerWorker(self.config, self.db, http=http1).run_cycle()
        self.assertEqual(s1["cycle_number"], 1)
        self.assertEqual(s1["status"], ExecutionOutcome.SUCCESS_WITH_RESULTS.value)
        self.assertEqual(s1["verified_count"], 1)

        # Simulate process restart / new database connection instance
        db_reopened = TrackerDatabase(self.db_path)
        http2 = ResilientHTTPClient(max_retries=0, sleep_fn=lambda _: None, transport=transport)
        s2 = SteamTrackerWorker(self.config, db_reopened, http=http2).run_cycle()
        self.assertEqual(s2["cycle_number"], 2)
        self.assertEqual(db_reopened.get_last_cycle_number(), 2)


if __name__ == "__main__":
    unittest.main()
