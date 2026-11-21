"""Tests for FastAPI endpoints, PDF report generation (cycle & historical),
offline/online synchronization, multi-day catch-up (e.g. 10 missed hourly cycles),
duplicate history merging, and JSON/CSV backup exports.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from backend.config import TrackerConfig
from backend.database import TrackerDatabase
from backend.http_client import ResilientHTTPClient
from backend.server import create_app
from backend.static_publisher import publish_static_site
from backend.worker import SteamTrackerWorker


def _cycle_transport(app_id: int, title: str):
    html = f"""
    <a class="search_result_row" data-ds-appid="{app_id}" href="https://store.steampowered.com/app/{app_id}/">
      <span class="title">{title}</span>
    </a>
    """

    def handler(req: httpx.Request) -> httpx.Response:
        u = str(req.url)
        if "search/results" in u:
            return httpx.Response(200, json={"total_count": 1, "results_html": html})
        if "api/appdetails" in u:
            return httpx.Response(200, json={
                str(app_id): {
                    "success": True,
                    "data": {
                        "steam_appid": app_id,
                        "name": title,
                        "type": "game",
                        "is_free": False,
                        "release_date": {"coming_soon": False},
                        "price_overview": {
                            "currency": "INR", "initial": 129900, "final": 0, "discount_percent": 100,
                            "initial_formatted": "₹ 1,299", "final_formatted": "₹ 0",
                        },
                        "package_groups": [],
                    },
                }
            })
        if f"store.steampowered.com/app/{app_id}" in u:
            return httpx.Response(200, text="<html>Free to keep when you get it before 2026-12-10.</html>")
        if "cheapshark.com" in u:
            return httpx.Response(200, json=[{
                "storeID": "1", "steamAppID": str(app_id), "title": title,
                "normalPrice": "19.99", "salePrice": "0.00", "savings": "100", "isOnSale": "1", "dealID": "x",
            }])
        if "gamerpower.com" in u:
            return httpx.Response(200, json=[])
        return httpx.Response(404)

    return httpx.MockTransport(handler)


class TestApiPdfAndSync(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / "api.db")
        self.config = TrackerConfig(db_path=self.db_path, trigger_api_key="secret-trigger-key")
        self.db = TrackerDatabase(self.db_path)
        self.app = create_app(config=self.config, db=self.db)
        self.client = TestClient(self.app)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _run_mock_cycle(self, app_id: int, title: str):
        http = ResilientHTTPClient(max_retries=0, sleep_fn=lambda _: None, transport=_cycle_transport(app_id, title))
        return SteamTrackerWorker(self.config, self.db, http=http).run_cycle()

    def test_33_multi_hour_offline_reconnection_and_catchup_sync(self):
        # Run 10 hourly cycles while client was offline since cycle 0
        for i in range(1, 11):
            self._run_mock_cycle(900000 + i, f"Free Game #{i}")

        # Client reconnects after 10 hours with since_cycle=0
        r = self.client.get("/api/v1/sync?since_cycle=0&client_id=android-1")
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertEqual(data["server_latest_cycle"], 10)
        self.assertEqual(data["missed_cycles_count"], 10)
        self.assertEqual(len(data["historical_games"]), 10)

        # Duplicate history merging simulation: sync again from cycle 7 -> only 3 new cycles returned
        r2 = self.client.get("/api/v1/sync?since_cycle=7&client_id=android-1")
        data2 = r2.json()
        self.assertEqual(data2["missed_cycles_count"], 3)
        returned_nums = sorted(c["cycle_number"] for c in data2["missed_cycles"])
        self.assertEqual(returned_nums, [8, 9, 10])

        # Re-sync from latest cycle 10 -> 0 missed cycles (no misleading catch-up popup)
        r3 = self.client.get("/api/v1/sync?since_cycle=10&client_id=android-1")
        self.assertEqual(r3.json()["missed_cycles_count"], 0)

    def test_34_individual_and_historical_pdf_generation(self):
        self._run_mock_cycle(888001, "PDF Verified Game")
        r_cyc = self.client.get("/api/v1/reports/cycle/1.pdf")
        self.assertEqual(r_cyc.status_code, 200)
        self.assertEqual(r_cyc.headers["content-type"], "application/pdf")
        self.assertTrue(r_cyc.content.startswith(b"%PDF-"))
        self.assertGreater(len(r_cyc.content), 1500)

        r_hist = self.client.get("/api/v1/reports/historical.pdf?download=true")
        self.assertEqual(r_hist.status_code, 200)
        self.assertTrue(r_hist.content.startswith(b"%PDF-"))
        self.assertIn("attachment;", r_hist.headers["content-disposition"])

    def test_35_json_csv_exports_and_manual_claimed_toggle(self):
        self._run_mock_cycle(888002, "Exportable Game")
        r_claim = self.client.patch("/api/v1/games/appid:888002/claimed", json={"claimed": True})
        self.assertEqual(r_claim.status_code, 200)
        self.assertTrue(r_claim.json()["game"]["manual_claimed"])

        r_json = self.client.get("/api/v1/export/json")
        self.assertEqual(r_json.status_code, 200)
        self.assertEqual(r_json.json()["last_cycle_number"], 1)

        r_csv = self.client.get("/api/v1/export/csv?dataset=games")
        self.assertEqual(r_csv.status_code, 200)
        self.assertIn("Exportable Game", r_csv.text)

    def test_36_trigger_endpoint_requires_bearer_token_and_serves_pwa_assets(self):
        r_unauth = self.client.post("/api/v1/scans/trigger")
        self.assertEqual(r_unauth.status_code, 401)

        r_idx = self.client.get("/")
        self.assertEqual(r_idx.status_code, 200)
        self.assertIn("Steam Free-to-Keep Tracker", r_idx.text)

        r_sw = self.client.get("/sw.js")
        self.assertEqual(r_sw.status_code, 200)
        r_man = self.client.get("/manifest.webmanifest")
        self.assertEqual(r_man.status_code, 200)


    def test_37_static_github_pages_publisher(self):
        self._run_mock_cycle(888003, "Zero Cost Game")
        out_dir = Path(self.tmp.name) / "site_out"
        res = publish_static_site(self.config, self.db, str(out_dir))
        self.assertEqual(res["cycles_published"], 1)
        self.assertEqual(res["cycle_pdfs_written"], 1)
        self.assertTrue((out_dir / "index.html").exists())
        self.assertTrue((out_dir / "api/v1/sync.json").exists())
        self.assertTrue((out_dir / "api/v1/reports/cycle/1.pdf").exists())
        self.assertTrue((out_dir / "api/v1/reports/historical.pdf").exists())


if __name__ == "__main__":
    unittest.main()
