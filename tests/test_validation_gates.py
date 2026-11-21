"""Tests for the 10-Gate Free-to-Keep Validation Engine covering all 19+ game scenarios."""
from __future__ import annotations

import unittest
from datetime import datetime, timezone

from backend.config import TrackerConfig
from backend.models import ClassificationStatus, DiscoveredCandidate, SecondaryEvidence
from backend.validator import TenGateValidator


def _valid_appdetails(app_id: int = 123456, name: str = "Chrono Odyssey") -> dict:
    return {
        "steam_appid": app_id,
        "name": name,
        "type": "game",
        "is_free": False,
        "release_date": {"coming_soon": False, "date": "10 Jan, 2024"},
        "price_overview": {
            "currency": "INR",
            "initial": 149900,
            "final": 0,
            "discount_percent": 100,
            "initial_formatted": "₹ 1,499",
            "final_formatted": "₹ 0",
        },
        "package_groups": [
            {
                "subs": [
                    {
                        "option_text": f"{name} - Free to keep",
                        "percent_savings_text": "-100%",
                        "price_in_cents_with_discount": 0,
                    }
                ]
            }
        ],
    }


def _valid_secondary(app_id: int = 123456, title: str = "Chrono Odyssey") -> SecondaryEvidence:
    return SecondaryEvidence(
        source_name="cheapshark",
        matched=True,
        is_direct_steam_free_to_keep=True,
        is_third_party_key_giveaway=False,
        title=title,
        steam_app_id=app_id,
        normal_price=19.99,
        sale_price=0.0,
        currency="USD",
        discount_percent=100,
    )


class TestTenGateValidator(unittest.TestCase):
    def setUp(self) -> None:
        self.config = TrackerConfig()
        self.validator = TenGateValidator(self.config)
        self.now = datetime(2026, 11, 21, 11, 0, 0, tzinfo=timezone.utc)

    def _cand(self, app_id=123456, title="Chrono Odyssey", url=None, source="steam_search", third_party=False):
        return DiscoveredCandidate(
            app_id=app_id,
            title=title,
            source=source,
            product_url=url if url is not None else (f"https://store.steampowered.com/app/{app_id}/" if app_id else "https://example.com/key"),
            is_third_party_giveaway=third_party,
            giveaway_url="https://thirdparty.example.com/giveaway" if third_party else None,
        )

    def test_01_genuine_paid_game_free_to_keep_verified(self):
        rec = self.validator.evaluate(
            self._cand(),
            _valid_appdetails(),
            None,
            {"has_free_to_keep_banner": True, "has_free_weekend_banner": False, "expiration_text": "2026-11-25 18:00:00", "is_demo_or_playtest_page": False},
            [_valid_secondary()],
            set(),
            self.now,
        )
        self.assertEqual(rec.final_status, ClassificationStatus.VERIFIED)
        self.assertEqual(len(rec.gates), 10)
        self.assertTrue(all(g.status.value == "PASS" for g in rec.gates))

    def test_02_permanent_free_to_play_rejected(self):
        ad = _valid_appdetails(app_id=570, name="Dota 2")
        ad["is_free"] = True
        ad["price_overview"] = {}
        ad["package_groups"] = []
        rec = self.validator.evaluate(self._cand(570, "Dota 2"), ad, None, None, [], set(), self.now)
        self.assertEqual(rec.final_status, ClassificationStatus.REJECTED)
        self.assertIn("Permanent Free-to-Play", rec.decision_reason)

    def test_03_free_weekend_rejected(self):
        ad = _valid_appdetails(app_id=2001, name="Weekend Racer")
        rec = self.validator.evaluate(
            self._cand(2001, "Weekend Racer"),
            ad,
            None,
            {"has_free_to_keep_banner": False, "has_free_weekend_banner": True, "expiration_text": None, "is_demo_or_playtest_page": False},
            [_valid_secondary(2001, "Weekend Racer")],
            set(),
            self.now,
        )
        self.assertEqual(rec.final_status, ClassificationStatus.REJECTED)
        self.assertIn("Free weekend", rec.decision_reason)

    def test_04_free_trial_rejected(self):
        ad = _valid_appdetails(app_id=2002, name="Space Quest Free Trial")
        rec = self.validator.evaluate(self._cand(2002, "Space Quest Free Trial"), ad, None, None, [_valid_secondary(2002)], set(), self.now)
        self.assertEqual(rec.final_status, ClassificationStatus.REJECTED)
        self.assertIn("trial", rec.decision_reason.lower())

    def test_05_demo_rejected(self):
        ad = _valid_appdetails(app_id=2003, name="Action RPG Demo")
        ad["type"] = "demo"
        rec = self.validator.evaluate(self._cand(2003, "Action RPG Demo"), ad, None, None, [_valid_secondary(2003)], set(), self.now)
        self.assertEqual(rec.final_status, ClassificationStatus.REJECTED)
        self.assertIn("demo", rec.decision_reason.lower())

    def test_06_beta_rejected(self):
        ad = _valid_appdetails(app_id=2004, name="Shooter Open Beta")
        rec = self.validator.evaluate(self._cand(2004, "Shooter Open Beta"), ad, None, None, [_valid_secondary(2004)], set(), self.now)
        self.assertEqual(rec.final_status, ClassificationStatus.REJECTED)
        self.assertIn("beta", rec.decision_reason.lower())

    def test_07_playtest_rejected(self):
        ad = _valid_appdetails(app_id=2005, name="Survival Playtest")
        rec = self.validator.evaluate(self._cand(2005, "Survival Playtest"), ad, None, None, [_valid_secondary(2005)], set(), self.now)
        self.assertEqual(rec.final_status, ClassificationStatus.REJECTED)
        self.assertIn("playtest", rec.decision_reason.lower())

    def test_08_dlc_rejected(self):
        ad = _valid_appdetails(app_id=2006, name="Armor Pack")
        ad["type"] = "dlc"
        rec = self.validator.evaluate(self._cand(2006, "Armor Pack"), ad, None, None, [_valid_secondary(2006)], set(), self.now)
        self.assertEqual(rec.final_status, ClassificationStatus.REJECTED)
        self.assertIn("dlc", rec.decision_reason.lower())

    def test_09_soundtrack_rejected(self):
        ad = _valid_appdetails(app_id=2007, name="Game Original Soundtrack")
        ad["type"] = "music"
        rec = self.validator.evaluate(self._cand(2007, "Game Original Soundtrack"), ad, None, None, [_valid_secondary(2007)], set(), self.now)
        self.assertEqual(rec.final_status, ClassificationStatus.REJECTED)
        self.assertIn("music", rec.decision_reason.lower())

    def test_10_third_party_key_giveaway_quarantined(self):
        sec = SecondaryEvidence(
            source_name="gamerpower",
            matched=True,
            is_direct_steam_free_to_keep=False,
            is_third_party_key_giveaway=True,
            title="Indie Key Drop",
            offer_url="https://fanatical.example.com/free-key",
        )
        rec = self.validator.evaluate(
            self._cand(app_id=None, title="Indie Key Drop", url="https://fanatical.example.com/free-key", source="gamerpower", third_party=True),
            None,
            "No Steam App ID",
            None,
            [sec],
            set(),
            self.now,
        )
        self.assertEqual(rec.final_status, ClassificationStatus.QUARANTINED)
        self.assertIn("Third-party", rec.decision_reason)

    def test_11_expired_promotion_rejected(self):
        ad = _valid_appdetails(app_id=2008, name="Expired Classic")
        cand = self._cand(2008, "Expired Classic")
        cand.expiration_hint = "2026-11-20 10:00:00"
        rec = self.validator.evaluate(cand, ad, None, None, [_valid_secondary(2008)], set(), self.now)
        self.assertEqual(rec.final_status, ClassificationStatus.REJECTED)
        self.assertIn("Expired", rec.decision_reason)

    def test_12_incorrect_original_price_zero_quarantined(self):
        ad = _valid_appdetails(app_id=2009, name="Zero Baseline")
        ad["price_overview"]["initial"] = 0
        rec = self.validator.evaluate(self._cand(2009, "Zero Baseline"), ad, None, None, [_valid_secondary(2009)], set(), self.now)
        self.assertEqual(rec.final_status, ClassificationStatus.QUARANTINED)

    def test_13_incorrect_discounted_price_nonzero_rejected(self):
        ad = _valid_appdetails(app_id=2010, name="Still Paid")
        ad["price_overview"]["final"] = 14900
        ad["price_overview"]["discount_percent"] = 90
        ad["price_overview"]["final_formatted"] = "₹ 149"
        ad["package_groups"] = []
        rec = self.validator.evaluate(self._cand(2010, "Still Paid"), ad, None, None, [_valid_secondary(2010)], set(), self.now)
        self.assertEqual(rec.final_status, ClassificationStatus.REJECTED)
        self.assertIn("non-zero", rec.decision_reason)

    def test_14_invalid_app_id_rejected(self):
        rec = self.validator.evaluate(
            self._cand(app_id=0, title="Bad AppID", url="https://store.steampowered.com/app/0/"),
            _valid_appdetails(0, "Bad AppID"),
            None,
            None,
            [_valid_secondary(0)],
            set(),
            self.now,
        )
        self.assertEqual(rec.final_status, ClassificationStatus.REJECTED)
        self.assertIn("App ID", rec.decision_reason)

    def test_15_incorrect_product_url_rejected(self):
        rec = self.validator.evaluate(
            self._cand(app_id=2011, title="Wrong URL", url="https://store.steampowered.com/app/999999/"),
            _valid_appdetails(2011, "Wrong URL"),
            None,
            None,
            [_valid_secondary(2011)],
            set(),
            self.now,
        )
        self.assertEqual(rec.final_status, ClassificationStatus.REJECTED)
        self.assertIn("URL", rec.decision_reason)

    def test_16_region_mismatch_rejected(self):
        ad = _valid_appdetails(app_id=2012, name="USD Only")
        ad["price_overview"]["currency"] = "USD"
        rec = self.validator.evaluate(self._cand(2012, "USD Only"), ad, None, None, [_valid_secondary(2012)], set(), self.now)
        self.assertEqual(rec.final_status, ClassificationStatus.REJECTED)
        self.assertIn("Region mismatch", rec.decision_reason)

    def test_17_duplicate_candidate_rejected(self):
        seen = {123456}
        rec = self.validator.evaluate(self._cand(123456), _valid_appdetails(123456), None, None, [_valid_secondary(123456)], seen, self.now)
        self.assertEqual(rec.final_status, ClassificationStatus.REJECTED)
        self.assertIn("Duplicate", rec.decision_reason)

    def test_18_missing_secondary_confirmation_quarantined(self):
        rec = self.validator.evaluate(self._cand(2013, "No Secondary"), _valid_appdetails(2013, "No Secondary"), None, None, [], set(), self.now)
        self.assertEqual(rec.final_status, ClassificationStatus.QUARANTINED)
        self.assertIn("secondary-source", rec.decision_reason.lower())

    def test_19_conflicting_source_data_quarantined(self):
        conflict = SecondaryEvidence(
            source_name="cheapshark",
            matched=True,
            is_direct_steam_free_to_keep=False,
            normal_price=19.99,
            sale_price=9.99,
            currency="USD",
            discount_percent=50,
        )
        rec = self.validator.evaluate(self._cand(2014, "Conflicting Price"), _valid_appdetails(2014, "Conflicting Price"), None, None, [conflict], set(), self.now)
        self.assertEqual(rec.final_status, ClassificationStatus.QUARANTINED)
        self.assertIn("Conflicting", rec.decision_reason)


if __name__ == "__main__":
    unittest.main()
