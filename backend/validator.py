"""10-Gate Evidence-Based Steam Free-to-Keep Validation Engine.

Strict classification rules:
- Category A (VERIFIED): Previously paid full Steam game, active 100% discount directly on Steam,
  permanent library retention (not free weekend/trial/demo/beta/playtest/DLC/soundtrack),
  valid numeric Steam App ID, available in target India (IN) region, and corroborated by
  independent secondary evidence (or no contradicting evidence — missing secondary evidence
  quarantines the candidate rather than falsely verifying it!).
- Category B (QUARANTINED): Third-party Steam key giveaways, missing secondary-source
  corroboration, conflicting source data, incomplete regional/pricing evidence, or uncertain
  permanent-claim conditions.
- Category C (REJECTED): Permanent Free-to-Play, DLC, Soundtrack, Demo, Trial, Free Weekend,
  Beta, Playtest, Expired offer, Paid game not discounted to 0, Invalid App ID, Mismatched URL,
  or Region mismatch.

No invented confidence scores — purely evidence-based gate outcomes.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set

from backend.config import TrackerConfig
from backend.models import (
    ClassificationStatus,
    DiscoveredCandidate,
    GateResult,
    GateStatus,
    SecondaryEvidence,
    ValidationRecord,
    ist_iso,
    utc_iso,
)

EXCLUDED_TITLE_MARKERS = [
    ("demo", re.compile(r"\bdemo\b", re.IGNORECASE)),
    ("beta", re.compile(r"\b(open|closed)?\s*beta\b", re.IGNORECASE)),
    ("playtest", re.compile(r"\bplaytest\b", re.IGNORECASE)),
    ("trial", re.compile(r"\b(free\s+)?trial\b", re.IGNORECASE)),
    ("soundtrack", re.compile(r"\b(original\s+)?soundtrack\b|\bost\b", re.IGNORECASE)),
    ("dlc", re.compile(r"\bdlc\b|\bseason pass\b|\bexpansion pack\b", re.IGNORECASE)),
]

FREE_WEEKEND_MARKERS = re.compile(
    r"free weekend|play for free|temporary access|trial period", re.IGNORECASE
)

STEAM_APP_CANONICAL_RE = re.compile(r"^https?://store\.steampowered\.com/app/(\d+)(?:/|$|\?)", re.IGNORECASE)


def format_minor_price(minor: Optional[int], currency: str) -> str:
    if minor is None:
        return "Unknown"
    symbol = {"INR": "₹", "USD": "$", "EUR": "€", "GBP": "£"}.get(currency.upper(), f"{currency.upper()} ")
    if minor == 0:
        return f"{symbol}0.00"
    return f"{symbol}{minor / 100:.2f}"


def _parse_iso_datetime(val: Optional[str]) -> Optional[datetime]:
    if not val:
        return None
    cleaned = val.strip()
    if cleaned.upper() in ("N/A", "NONE", "UNKNOWN"):
        return None
    for fmt in (
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%d",
    ):
        try:
            dt = datetime.strptime(cleaned, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except ValueError:
            continue
    try:
        dt = datetime.fromisoformat(cleaned.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except ValueError:
        return None


class TenGateValidator:
    """Evaluates a candidate game across all 10 mandatory validation gates."""

    def __init__(self, config: TrackerConfig) -> None:
        self.config = config

    def evaluate(
        self,
        candidate: DiscoveredCandidate,
        appdetails: Optional[Dict[str, Any]],
        appdetails_error: Optional[str],
        store_page_signals: Optional[Dict[str, Any]],
        secondary_evidence: List[SecondaryEvidence],
        seen_app_ids_in_cycle: Set[int],
        now_utc: Optional[datetime] = None,
    ) -> ValidationRecord:
        now = now_utc or datetime.now(timezone.utc)
        now_u = utc_iso(now)
        now_i = ist_iso(now)
        gates: List[GateResult] = []
        rejection_reasons: List[str] = []
        quarantine_reasons: List[str] = []

        # Extract Steam appdetails fields safely
        steam_title = (appdetails or {}).get("name") or candidate.title
        steam_type = str((appdetails or {}).get("type") or "unknown").lower()
        is_free_flag = bool((appdetails or {}).get("is_free", False))
        price_ov = (appdetails or {}).get("price_overview") or {}
        package_groups = (appdetails or {}).get("package_groups") or []
        release_date = (appdetails or {}).get("release_date") or {}

        currency = str(price_ov.get("currency") or self.config.default_currency).upper()
        initial_minor: Optional[int] = price_ov.get("initial") if isinstance(price_ov.get("initial"), int) else None
        final_minor: Optional[int] = price_ov.get("final") if isinstance(price_ov.get("final"), int) else None
        discount_pct: Optional[int] = (
            price_ov.get("discount_percent") if isinstance(price_ov.get("discount_percent"), int) else None
        )

        # Inspect package_groups for 100%-off free-to-keep sub or free weekend sub
        has_free_sub = False
        has_free_weekend_sub = False
        sub_original_minor: Optional[int] = None
        for pg in package_groups:
            if not isinstance(pg, dict):
                continue
            for sub in pg.get("subs") or []:
                if not isinstance(sub, dict):
                    continue
                opt_text = str(sub.get("option_text") or "")
                opt_desc = str(sub.get("option_description") or "")
                pct_savings = str(sub.get("percent_savings_text") or "")
                price_cents = sub.get("price_in_cents_with_discount")
                if FREE_WEEKEND_MARKERS.search(opt_text) or FREE_WEEKEND_MARKERS.search(opt_desc):
                    has_free_weekend_sub = True
                if (
                    price_cents == 0
                    and ("-100%" in pct_savings or "100%" in pct_savings or sub.get("percent_savings") == 100)
                ):
                    has_free_sub = True
                    # Some Steam 100%-off packages still record base price in option_text or price_overview
                    if sub.get("price_in_cents_without_discount"):
                        sub_original_minor = int(sub["price_in_cents_without_discount"])

        if initial_minor is None and sub_original_minor:
            initial_minor = sub_original_minor
        if final_minor is None and has_free_sub:
            final_minor = 0
        if discount_pct is None and has_free_sub:
            discount_pct = 100

        original_formatted = (
            str(price_ov.get("initial_formatted"))
            if price_ov.get("initial_formatted")
            else (
                format_minor_price(initial_minor, currency)
                if initial_minor is not None
                else (candidate.search_original_price or "Unknown")
            )
        )
        current_formatted = (
            str(price_ov.get("final_formatted"))
            if price_ov.get("final_formatted")
            else (
                format_minor_price(final_minor, currency)
                if final_minor is not None
                else (candidate.search_final_price or "Unknown")
            )
        )

        # Detect third-party key giveaway upfront (used across gates)
        is_third_party_only = candidate.is_third_party_giveaway and candidate.source == "gamerpower"
        if not is_third_party_only and secondary_evidence:
            if all(s.is_third_party_key_giveaway for s in secondary_evidence) and candidate.source != "steam_search":
                is_third_party_only = True

        # ------------------------------------------------------------------
        # GATE 4 (checked logically alongside 1-3, recorded in canonical 1..10 order)
        # Let's evaluate in strict 1..10 order.
        # ------------------------------------------------------------------

        # === GATE 1: Paid Game Baseline ===
        if appdetails is None:
            g1 = GateResult(
                gate_number=1,
                gate_name="Paid Game Baseline",
                status=GateStatus.UNCERTAIN,
                evidence=appdetails_error or "No Steam appdetails available",
                source="steam_appdetails",
                timestamp=now_u,
                reason="Unable to confirm historical paid baseline from Steam appdetails.",
            )
            quarantine_reasons.append("Missing Steam appdetails to confirm paid baseline")
        elif is_free_flag and (initial_minor is None or initial_minor <= 0) and not has_free_sub:
            g1 = GateResult(
                gate_number=1,
                gate_name="Paid Game Baseline",
                status=GateStatus.FAIL,
                evidence=f"is_free=True, initial_price={initial_minor}",
                source="steam_appdetails",
                timestamp=now_u,
                reason="Permanent Free-to-Play or ordinary free game with no paid baseline.",
            )
            rejection_reasons.append("Permanent Free-to-Play game (no non-zero paid baseline)")
        elif initial_minor is not None and initial_minor > 0:
            g1 = GateResult(
                gate_number=1,
                gate_name="Paid Game Baseline",
                status=GateStatus.PASS,
                evidence=f"Original price {original_formatted} ({initial_minor} minor units {currency})",
                source="steam_appdetails",
                timestamp=now_u,
                reason="Verified non-zero original paid price on Steam.",
            )
        else:
            g1 = GateResult(
                gate_number=1,
                gate_name="Paid Game Baseline",
                status=GateStatus.UNCERTAIN,
                evidence=f"initial_price={initial_minor}, is_free={is_free_flag}",
                source="steam_appdetails",
                timestamp=now_u,
                reason="Original paid price missing or zero in Steam appdetails.",
            )
            quarantine_reasons.append("Missing non-zero original paid price evidence")
        gates.append(g1)

        # === GATE 2: Active Promotion ===
        sp_ftk = bool((store_page_signals or {}).get("has_free_to_keep_banner", False))
        sp_fw = bool((store_page_signals or {}).get("has_free_weekend_banner", False))
        coming_soon = bool(release_date.get("coming_soon", False))

        if coming_soon:
            g2 = GateResult(
                gate_number=2,
                gate_name="Active Promotion",
                status=GateStatus.FAIL,
                evidence="release_date.coming_soon=True",
                source="steam_appdetails",
                timestamp=now_u,
                reason="Unreleased game cannot have an active Free-to-Keep claim promotion.",
            )
            rejection_reasons.append("Unreleased game (coming_soon=True)")
        elif has_free_weekend_sub or sp_fw:
            g2 = GateResult(
                gate_number=2,
                gate_name="Active Promotion",
                status=GateStatus.FAIL,
                evidence="Free weekend / temporary play-for-free banner or package detected",
                source="steam_store_page" if sp_fw else "steam_appdetails",
                timestamp=now_u,
                reason="Temporary Free Weekend / Free Trial access, NOT permanent Free-to-Keep.",
            )
            rejection_reasons.append("Free weekend / temporary access promotion (not Free-to-Keep)")
        elif is_third_party_only:
            g2 = GateResult(
                gate_number=2,
                gate_name="Active Promotion",
                status=GateStatus.UNCERTAIN,
                evidence=f"Third-party giveaway URL: {candidate.giveaway_url or candidate.product_url}",
                source=candidate.source,
                timestamp=now_u,
                reason="Third-party Steam key giveaway; not a direct Steam Store promotional discount.",
            )
            quarantine_reasons.append("Third-party Steam key giveaway (not direct Steam Store Free-to-Keep)")
        elif appdetails is None:
            g2 = GateResult(
                gate_number=2,
                gate_name="Active Promotion",
                status=GateStatus.UNCERTAIN,
                evidence=appdetails_error or "Steam appdetails unavailable",
                source="steam_appdetails",
                timestamp=now_u,
                reason="Cannot verify active Steam promotion without appdetails.",
            )
            quarantine_reasons.append("Could not confirm active Steam promotion")
        elif (discount_pct == 100 and final_minor == 0) or has_free_sub or sp_ftk:
            g2 = GateResult(
                gate_number=2,
                gate_name="Active Promotion",
                status=GateStatus.PASS,
                evidence=f"discount_percent={discount_pct}, free_to_keep_banner={sp_ftk}, free_sub={has_free_sub}",
                source="steam_store_page" if sp_ftk else "steam_appdetails",
                timestamp=now_u,
                reason="Active 100% Free-to-Keep promotion confirmed on Steam.",
            )
        else:
            g2 = GateResult(
                gate_number=2,
                gate_name="Active Promotion",
                status=GateStatus.FAIL,
                evidence=f"discount_percent={discount_pct}, final_price={final_minor}",
                source="steam_appdetails",
                timestamp=now_u,
                reason="No active 100%-off promotion found on Steam (expired or regular paid listing).",
            )
            rejection_reasons.append("No active 100% Free-to-Keep promotion on Steam")
        gates.append(g2)

        # === GATE 3: Current Price Verification ===
        if appdetails is None:
            g3 = GateResult(
                gate_number=3,
                gate_name="Current Price Verification",
                status=GateStatus.UNCERTAIN,
                evidence=appdetails_error or "No price_overview",
                source="steam_appdetails",
                timestamp=now_u,
                reason="Steam price data unavailable.",
            )
            quarantine_reasons.append("Missing Steam current price verification")
        elif final_minor is not None and final_minor == 0 and discount_pct == 100:
            g3 = GateResult(
                gate_number=3,
                gate_name="Current Price Verification",
                status=GateStatus.PASS,
                evidence=f"final={final_minor} ({current_formatted}), discount={discount_pct}%",
                source="steam_appdetails",
                timestamp=now_u,
                reason="Verified 100% discount with current price strictly zero.",
            )
        elif final_minor is not None and final_minor > 0:
            g3 = GateResult(
                gate_number=3,
                gate_name="Current Price Verification",
                status=GateStatus.FAIL,
                evidence=f"final={final_minor} ({current_formatted}), discount={discount_pct}%",
                source="steam_appdetails",
                timestamp=now_u,
                reason=f"Paid game is NOT discounted to zero (currently {current_formatted}).",
            )
            rejection_reasons.append(f"Current price is non-zero ({current_formatted}, discount {discount_pct}%)")
        elif discount_pct is not None and discount_pct < 100:
            g3 = GateResult(
                gate_number=3,
                gate_name="Current Price Verification",
                status=GateStatus.FAIL,
                evidence=f"discount={discount_pct}%, final={final_minor}",
                source="steam_appdetails",
                timestamp=now_u,
                reason=f"Discount is {discount_pct}%, not 100%.",
            )
            rejection_reasons.append(f"Discount is {discount_pct}%, below 100%")
        else:
            g3 = GateResult(
                gate_number=3,
                gate_name="Current Price Verification",
                status=GateStatus.UNCERTAIN,
                evidence=f"final={final_minor}, discount={discount_pct}",
                source="steam_appdetails",
                timestamp=now_u,
                reason="Incomplete promotional price breakdown.",
            )
            quarantine_reasons.append("Incomplete current price evidence")
        gates.append(g3)

        # === GATE 4: App ID & Product URL Verification ===
        url_match = STEAM_APP_CANONICAL_RE.match(candidate.product_url or "")
        url_appid = int(url_match.group(1)) if url_match else None
        steam_returned_appid = (appdetails or {}).get("steam_appid")

        if candidate.app_id is None or not isinstance(candidate.app_id, int) or candidate.app_id <= 0:
            if is_third_party_only:
                g4 = GateResult(
                    gate_number=4,
                    gate_name="App ID Verification",
                    status=GateStatus.UNCERTAIN,
                    evidence=f"app_id={candidate.app_id}, url={candidate.product_url}",
                    source=candidate.source,
                    timestamp=now_u,
                    reason="Third-party giveaway lacks a direct Steam App ID.",
                )
                quarantine_reasons.append("No numeric Steam App ID on third-party giveaway")
            else:
                g4 = GateResult(
                    gate_number=4,
                    gate_name="App ID Verification",
                    status=GateStatus.FAIL,
                    evidence=f"app_id={candidate.app_id}, url={candidate.product_url}",
                    source=candidate.source,
                    timestamp=now_u,
                    reason="Invalid or missing numeric Steam App ID.",
                )
                rejection_reasons.append("Invalid or missing numeric Steam App ID")
        elif not is_third_party_only and (url_appid is None or url_appid != candidate.app_id):
            g4 = GateResult(
                gate_number=4,
                gate_name="App ID Verification",
                status=GateStatus.FAIL,
                evidence=f"candidate_app_id={candidate.app_id}, url={candidate.product_url}",
                source=candidate.source,
                timestamp=now_u,
                reason="Product URL does not match canonical Steam App ID URL.",
            )
            rejection_reasons.append("Incorrect or mismatched Steam product URL")
        elif steam_returned_appid is not None and int(steam_returned_appid) != candidate.app_id:
            g4 = GateResult(
                gate_number=4,
                gate_name="App ID Verification",
                status=GateStatus.FAIL,
                evidence=f"requested={candidate.app_id}, steam_returned={steam_returned_appid}",
                source="steam_appdetails",
                timestamp=now_u,
                reason="Steam appdetails returned a different steam_appid than requested.",
            )
            rejection_reasons.append("Steam App ID mismatch in appdetails response")
        else:
            g4 = GateResult(
                gate_number=4,
                gate_name="App ID Verification",
                status=GateStatus.PASS,
                evidence=f"Verified numeric App ID {candidate.app_id} -> https://store.steampowered.com/app/{candidate.app_id}/",
                source="steam_appdetails" if appdetails else candidate.source,
                timestamp=now_u,
                reason="Valid numeric Steam App ID and canonical Steam store URL confirmed.",
            )
        gates.append(g4)

        # === GATE 5: Product Type Verification ===
        title_exclusion = None
        for label, rx in EXCLUDED_TITLE_MARKERS:
            if rx.search(steam_title) or rx.search(candidate.title):
                title_exclusion = label
                break

        if appdetails is not None and steam_type != "game":
            g5 = GateResult(
                gate_number=5,
                gate_name="Product Type Verification",
                status=GateStatus.FAIL,
                evidence=f"steam_type='{steam_type}'",
                source="steam_appdetails",
                timestamp=now_u,
                reason=f"Excluded product type '{steam_type}' (only full 'game' is eligible).",
            )
            rejection_reasons.append(f"Excluded product type: {steam_type}")
        elif title_exclusion:
            g5 = GateResult(
                gate_number=5,
                gate_name="Product Type Verification",
                status=GateStatus.FAIL,
                evidence=f"title='{steam_title}', matched_exclusion='{title_exclusion}'",
                source="steam_appdetails" if appdetails else candidate.source,
                timestamp=now_u,
                reason=f"Title indicates excluded product category ({title_exclusion}).",
            )
            rejection_reasons.append(f"Excluded category detected in title: {title_exclusion}")
        elif (store_page_signals or {}).get("is_demo_or_playtest_page"):
            g5 = GateResult(
                gate_number=5,
                gate_name="Product Type Verification",
                status=GateStatus.FAIL,
                evidence="Store page contains demo/playtest/free-weekend markers without Free-to-Keep banner",
                source="steam_store_page",
                timestamp=now_u,
                reason="Store page indicates Demo or Playtest rather than full Free-to-Keep game.",
            )
            rejection_reasons.append("Demo or Playtest detected on Steam Store page")
        elif appdetails is None:
            g5 = GateResult(
                gate_number=5,
                gate_name="Product Type Verification",
                status=GateStatus.UNCERTAIN,
                evidence="Steam appdetails unavailable",
                source="steam_appdetails",
                timestamp=now_u,
                reason="Cannot confirm product type='game' without Steam appdetails.",
            )
            quarantine_reasons.append("Unverified product type")
        else:
            g5 = GateResult(
                gate_number=5,
                gate_name="Product Type Verification",
                status=GateStatus.PASS,
                evidence=f"type='game', title='{steam_title}'",
                source="steam_appdetails",
                timestamp=now_u,
                reason="Confirmed full eligible Steam game (not DLC, demo, soundtrack, beta, or playtest).",
            )
        gates.append(g5)

        # === GATE 6: Duplicate Detection ===
        if candidate.app_id is not None and candidate.app_id in seen_app_ids_in_cycle:
            g6 = GateResult(
                gate_number=6,
                gate_name="Duplicate Detection",
                status=GateStatus.FAIL,
                evidence=f"App ID {candidate.app_id} already processed in this scan cycle",
                source="internal_dedupe",
                timestamp=now_u,
                reason="Duplicate candidate suppressed within the same scan cycle.",
            )
            rejection_reasons.append(f"Duplicate candidate for App ID {candidate.app_id}")
        else:
            if candidate.app_id is not None:
                seen_app_ids_in_cycle.add(candidate.app_id)
            g6 = GateResult(
                gate_number=6,
                gate_name="Duplicate Detection",
                status=GateStatus.PASS,
                evidence=f"Unique App ID {candidate.app_id or 'N/A'} in current cycle",
                source="internal_dedupe",
                timestamp=now_u,
                reason="No duplicate detected in current scan cycle.",
            )
        gates.append(g6)

        # === GATE 7: Region Verification (India / INR) ===
        if appdetails is None:
            g7 = GateResult(
                gate_number=7,
                gate_name="Region Verification",
                status=GateStatus.UNCERTAIN,
                evidence=f"Target region={self.config.target_region}, appdetails unavailable",
                source="steam_appdetails",
                timestamp=now_u,
                reason="Cannot verify India (IN) availability without Steam appdetails.",
            )
            quarantine_reasons.append("Incomplete regional availability data for India (IN)")
        elif price_ov and str(price_ov.get("currency", "")).upper() != self.config.default_currency:
            actual_cur = str(price_ov.get("currency", "")).upper()
            g7 = GateResult(
                gate_number=7,
                gate_name="Region Verification",
                status=GateStatus.FAIL,
                evidence=f"Expected currency {self.config.default_currency} (region {self.config.target_region}), got {actual_cur}",
                source="steam_appdetails",
                timestamp=now_u,
                reason=f"Region/currency mismatch: expected {self.config.default_currency} for {self.config.target_region}, got {actual_cur}.",
            )
            rejection_reasons.append(f"Region mismatch: returned currency {actual_cur} instead of {self.config.default_currency}")
        elif not price_ov and not package_groups:
            g7 = GateResult(
                gate_number=7,
                gate_name="Region Verification",
                status=GateStatus.FAIL,
                evidence=f"No price_overview or purchase packages for cc={self.config.steam_country_code}",
                source="steam_appdetails",
                timestamp=now_u,
                reason=f"Not available for purchase/claim in region {self.config.target_region}.",
            )
            rejection_reasons.append(f"Unavailable in target region {self.config.target_region}")
        else:
            g7 = GateResult(
                gate_number=7,
                gate_name="Region Verification",
                status=GateStatus.PASS,
                evidence=f"Region={self.config.target_region} (cc={self.config.steam_country_code}), currency={currency}",
                source="steam_appdetails",
                timestamp=now_u,
                reason=f"Confirmed available in {self.config.target_region} storefront ({currency}).",
            )
        gates.append(g7)

        # === GATE 8: Expiration Verification ===
        expiration_str: Optional[str] = None
        if store_page_signals and store_page_signals.get("expiration_text"):
            expiration_str = store_page_signals["expiration_text"]
        elif candidate.expiration_hint:
            expiration_str = candidate.expiration_hint
        else:
            for s in secondary_evidence:
                if s.expiration_text:
                    expiration_str = s.expiration_text
                    break

        parsed_exp = _parse_iso_datetime(expiration_str)
        if parsed_exp is not None and parsed_exp <= now:
            g8 = GateResult(
                gate_number=8,
                gate_name="Expiration Verification",
                status=GateStatus.FAIL,
                evidence=f"expiration={expiration_str} <= now={now_u}",
                source="expiration_check",
                timestamp=now_u,
                reason="Promotional period has already expired.",
            )
            rejection_reasons.append(f"Expired promotion (ended {expiration_str})")
        elif expiration_str:
            g8 = GateResult(
                gate_number=8,
                gate_name="Expiration Verification",
                status=GateStatus.PASS,
                evidence=f"Active window; expiration='{expiration_str}'",
                source="steam_store_page" if (store_page_signals and store_page_signals.get("expiration_text")) else "secondary_source",
                timestamp=now_u,
                reason="Promotion expiration window is active.",
            )
        else:
            # Steam appdetails often omits exact end timestamp; pass if store page confirmed FTK banner,
            # otherwise pass with explicit note that expiration timestamp is unknown (not a hard failure if active on Steam)
            g8 = GateResult(
                gate_number=8,
                gate_name="Expiration Verification",
                status=GateStatus.PASS if (discount_pct == 100 and final_minor == 0) else GateStatus.UNCERTAIN,
                evidence="Exact expiration timestamp not exposed by API",
                source="steam_appdetails",
                timestamp=now_u,
                reason="Promotion currently active on Steam; exact expiration time not disclosed by endpoint.",
            )
        gates.append(g8)

        # === GATE 9: Independent Secondary Verification ===
        supporting_sec = [s for s in secondary_evidence if s.matched and s.is_direct_steam_free_to_keep]
        third_party_sec = [s for s in secondary_evidence if s.matched and s.is_third_party_key_giveaway]
        conflicting_sec = [
            s
            for s in secondary_evidence
            if s.matched
            and not s.is_third_party_key_giveaway
            and s.sale_price is not None
            and s.sale_price > 0
        ]

        if conflicting_sec:
            details = "; ".join(
                f"{s.source_name}: salePrice={s.sale_price} {s.currency or ''}" for s in conflicting_sec
            )
            g9 = GateResult(
                gate_number=9,
                gate_name="Independent Secondary Verification",
                status=GateStatus.UNCERTAIN,
                evidence=f"Conflicting secondary data: {details}",
                source=",".join(s.source_name for s in conflicting_sec),
                timestamp=now_u,
                reason="Secondary pricing source conflicts with Steam 100%-off claim.",
            )
            quarantine_reasons.append(f"Conflicting secondary source data ({details})")
        elif third_party_sec and not supporting_sec:
            g9 = GateResult(
                gate_number=9,
                gate_name="Independent Secondary Verification",
                status=GateStatus.UNCERTAIN,
                evidence=f"Third-party giveaway on {', '.join(s.source_name for s in third_party_sec)}",
                source=",".join(s.source_name for s in third_party_sec),
                timestamp=now_u,
                reason="Secondary source lists a third-party key giveaway rather than a direct Steam Store offer.",
            )
            quarantine_reasons.append("Secondary source indicates third-party key giveaway")
        elif supporting_sec:
            details = "; ".join(
                f"{s.source_name} (normal={s.normal_price}, sale={s.sale_price}, savings={s.discount_percent}%)"
                for s in supporting_sec
            )
            g9 = GateResult(
                gate_number=9,
                gate_name="Independent Secondary Verification",
                status=GateStatus.PASS,
                evidence=details,
                source=",".join(s.source_name for s in supporting_sec),
                timestamp=now_u,
                reason="Independent secondary source corroborates active 100%-off Steam promotion.",
            )
        else:
            # Missing secondary source MUST NOT be treated as confirmation!
            g9 = GateResult(
                gate_number=9,
                gate_name="Independent Secondary Verification",
                status=GateStatus.UNCERTAIN,
                evidence="No corroborating secondary source (CheapShark/GamerPower) confirmed this Steam offer.",
                source="secondary_check",
                timestamp=now_u,
                reason="Missing independent secondary-source corroboration; quarantined for safety.",
            )
            quarantine_reasons.append("Missing independent secondary-source corroboration")
        gates.append(g9)

        # === GATE 10: Final Evidence-Based Decision ===
        any_fail = any(g.status == GateStatus.FAIL for g in gates)
        any_uncertain = any(g.status == GateStatus.UNCERTAIN for g in gates)

        if any_fail:
            final_status = ClassificationStatus.REJECTED
            decision_reason = "; ".join(dict.fromkeys(rejection_reasons)) or "Failed one or more mandatory validation gates."
            g10_status = GateStatus.FAIL
        elif any_uncertain:
            final_status = ClassificationStatus.QUARANTINED
            decision_reason = "; ".join(dict.fromkeys(quarantine_reasons)) or "Incomplete or conflicting validation evidence."
            g10_status = GateStatus.UNCERTAIN
        else:
            final_status = ClassificationStatus.VERIFIED
            decision_reason = (
                "All 9 evidence gates passed: previously paid full Steam game, active 100% discount "
                f"in {self.config.target_region}, valid App ID, corroborated by independent secondary source."
            )
            g10_status = GateStatus.PASS

        g10 = GateResult(
            gate_number=10,
            gate_name="Final Evidence-Based Decision",
            status=g10_status,
            evidence=f"PASS={sum(1 for g in gates if g.status == GateStatus.PASS)}, "
                     f"UNCERTAIN={sum(1 for g in gates if g.status == GateStatus.UNCERTAIN)}, "
                     f"FAIL={sum(1 for g in gates if g.status == GateStatus.FAIL)}",
            source="validation_engine",
            timestamp=now_u,
            reason=f"Assigned {final_status.value}: {decision_reason}",
        )
        gates.append(g10)

        claim_url = (
            f"https://store.steampowered.com/app/{candidate.app_id}/"
            if (candidate.app_id and not is_third_party_only)
            else (candidate.giveaway_url or candidate.product_url)
        )

        return ValidationRecord(
            app_id=candidate.app_id,
            title=steam_title,
            final_status=final_status,
            original_price_minor=initial_minor,
            current_price_minor=final_minor,
            original_price_formatted=original_formatted,
            current_price_formatted=current_formatted,
            discount_percent=discount_pct,
            currency=currency,
            region=self.config.target_region,
            product_type=steam_type,
            claim_url=claim_url,
            expiration_at=expiration_str,
            primary_source=candidate.source,
            secondary_sources=[s.to_dict() for s in secondary_evidence],
            gates=gates,
            decision_reason=decision_reason,
            rejection_reasons=list(dict.fromkeys(rejection_reasons)),
            quarantine_reasons=list(dict.fromkeys(quarantine_reasons)),
            checked_at_utc=now_u,
            checked_at_ist=now_i,
        )
