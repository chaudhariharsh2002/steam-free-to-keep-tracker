"""Steam and secondary-source discovery connectors with complete pagination and coverage metrics.

Primary sources:
- Steam Store Filtered Search:
  https://store.steampowered.com/search/results/?query=&maxprice=free&specials=1&category1=998&cc=in&l=english&infinite=1&start=<offset>&count=50
- Steam AppDetails API:
  https://store.steampowered.com/api/appdetails?appids=<id>&cc=in&l=english
- Steam Store Product Page:
  https://store.steampowered.com/app/<id>/?cc=in&l=english

Secondary sources:
- CheapShark API (storeID=1 = Steam, USD):
  https://www.cheapshark.com/api/1.0/deals?storeID=1&upperPrice=0&onSale=1&pageNumber=<n>&pageSize=60
- GamerPower API (Steam giveaways — distinguishes direct Steam links vs third-party key giveaways):
  https://www.gamerpower.com/api/giveaways?platform=steam&type=game
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional
from bs4 import BeautifulSoup

from backend.config import TrackerConfig
from backend.http_client import ResilientHTTPClient, ResilientHTTPError
from backend.models import DiscoveredCandidate, SecondaryEvidence, utc_iso


STEAM_APP_URL_RE = re.compile(r"https?://store\.steampowered\.com/app/(\d+)", re.IGNORECASE)
FREE_TO_KEEP_PHRASE_RE = re.compile(
    r"free to keep when you get it before\s+([^.<\n]+)", re.IGNORECASE
)
FREE_WEEKEND_PHRASE_RE = re.compile(
    r"(play for free|free weekend|ends in \d+\s+(days?|hours?)\s*[-–]?\s*play|free trial)",
    re.IGNORECASE,
)


@dataclass
class DiscoveryResult:
    candidates: List[DiscoveredCandidate] = field(default_factory=list)
    secondary_by_appid: Dict[int, List[SecondaryEvidence]] = field(default_factory=dict)
    secondary_by_title: Dict[str, List[SecondaryEvidence]] = field(default_factory=dict)
    sources_checked: List[str] = field(default_factory=list)
    steam_catalogue_size: Optional[int] = None  # Never invented; None if not exposed
    filtered_promo_total: Optional[int] = None
    pages_retrieved: int = 0
    total_pages_exposed: Optional[int] = None
    steam_search_requests: int = 0
    secondary_requests: int = 0
    errors: List[str] = field(default_factory=list)
    coverage_disclosure: str = ""


ProgressCallback = Callable[[str, str, Dict[str, Any]], None]


def _extract_steam_app_id(url: str) -> Optional[int]:
    if not url:
        return None
    m = STEAM_APP_URL_RE.search(url)
    if m:
        try:
            return int(m.group(1))
        except ValueError:
            return None
    return None


def parse_steam_search_html(results_html: str) -> List[DiscoveredCandidate]:
    """Parse Steam search/results `results_html` rows into DiscoveredCandidate objects."""
    if not results_html or not results_html.strip():
        return []
    soup = BeautifulSoup(results_html, "html.parser")
    found: List[DiscoveredCandidate] = []
    for row in soup.select("a.search_result_row"):
        href = (row.get("href") or "").strip()
        raw_appid = row.get("data-ds-appid")
        app_id: Optional[int] = None
        if raw_appid:
            # Sometimes comma-separated for bundles; take only single numeric app IDs
            parts = [p.strip() for p in str(raw_appid).split(",") if p.strip()]
            if len(parts) == 1 and parts[0].isdigit():
                app_id = int(parts[0])
        if app_id is None:
            app_id = _extract_steam_app_id(href)

        title_el = row.select_one(".title")
        title = title_el.get_text(strip=True) if title_el else f"Steam App {app_id or 'Unknown'}"

        orig_el = row.select_one(".discount_original_price")
        final_el = row.select_one(".discount_final_price")
        pct_el = row.select_one(".discount_pct")
        discount_pct: Optional[int] = None
        if pct_el:
            m = re.search(r"(\d+)\s*%", pct_el.get_text())
            if m:
                discount_pct = int(m.group(1))

        canonical_url = f"https://store.steampowered.com/app/{app_id}/" if app_id else href
        found.append(
            DiscoveredCandidate(
                app_id=app_id,
                title=title,
                source="steam_search",
                product_url=canonical_url,
                search_original_price=orig_el.get_text(strip=True) if orig_el else None,
                search_final_price=final_el.get_text(strip=True) if final_el else None,
                search_discount_pct=discount_pct,
            )
        )
    return found


def parse_steam_store_page(html: str) -> Dict[str, Any]:
    """Inspect Steam Store product page HTML for permanent-claim vs free-weekend markers."""
    if not html:
        return {
            "has_free_to_keep_banner": False,
            "has_free_weekend_banner": False,
            "expiration_text": None,
            "is_demo_or_playtest_page": False,
        }
    text = BeautifulSoup(html, "html.parser").get_text(" ", strip=True)
    ftk_match = FREE_TO_KEEP_PHRASE_RE.search(text)
    fw_match = FREE_WEEKEND_PHRASE_RE.search(text)
    lower = text.lower()
    is_demo_playtest = any(
        marker in lower
        for marker in ("download demo", "join the playtest", "steam playtest", "free weekend")
    )
    return {
        "has_free_to_keep_banner": bool(ftk_match),
        "has_free_weekend_banner": bool(fw_match) and not bool(ftk_match),
        "expiration_text": ftk_match.group(1).strip() if ftk_match else None,
        "is_demo_or_playtest_page": is_demo_playtest and not bool(ftk_match),
    }


class DiscoveryEngine:
    """Executes paginated discovery across Steam Store, CheapShark, and GamerPower."""

    STEAM_SEARCH_URL = "https://store.steampowered.com/search/results/"
    STEAM_APPDETAILS_URL = "https://store.steampowered.com/api/appdetails"
    CHEAPSHARK_DEALS_URL = "https://www.cheapshark.com/api/1.0/deals"
    GAMERPOWER_GIVEAWAYS_URL = "https://www.gamerpower.com/api/giveaways"

    def __init__(self, config: TrackerConfig, http: ResilientHTTPClient) -> None:
        self.config = config
        self.http = http

    def discover_all(self, emit: Optional[ProgressCallback] = None) -> DiscoveryResult:
        res = DiscoveryResult()
        cb = emit or (lambda et, msg, data: None)

        # 1. Primary Source: Steam Store Filtered Search (complete pagination)
        self._discover_steam_search(res, cb)

        # 2. Secondary Source A: CheapShark API (Steam storeID=1, upperPrice=0, onSale=1)
        if self.config.enable_cheapshark:
            self._discover_cheapshark(res, cb)

        # 3. Secondary Source B: GamerPower API (Steam games — flags third-party key giveaways)
        if self.config.enable_gamerpower:
            self._discover_gamerpower(res, cb)

        disclosures = [
            f"Steam filtered search (category1=998, specials=1, maxprice=free, cc={self.config.steam_country_code}) "
            f"retrieved {res.pages_retrieved} page(s)"
            + (
                f" of {res.total_pages_exposed} exposed page(s) ({res.filtered_promo_total} filtered results)."
                if res.total_pages_exposed is not None
                else " (total pages UNKNOWN from response)."
            ),
            "Full Steam catalogue is not downloaded hourly; server-side promotional filters + secondary feeds are used.",
        ]
        res.coverage_disclosure = " ".join(disclosures)
        return res

    def _discover_steam_search(self, res: DiscoveryResult, cb: ProgressCallback) -> None:
        res.sources_checked.append("steam_search")
        start = 0
        page_size = self.config.search_page_size
        page_num = 0

        while page_num < self.config.max_search_pages:
            page_num += 1
            params = {
                "query": "",
                "maxprice": "free",
                "specials": "1",
                "category1": "998",  # Full games only
                "cc": self.config.steam_country_code,
                "l": self.config.steam_language,
                "infinite": "1",
                "start": str(start),
                "count": str(page_size),
            }
            cb(
                "STEAM_SEARCH_REQUEST",
                f"Requesting Steam Store filtered search page {page_num} (start={start}, cc={self.config.steam_country_code})",
                {"source": "steam_search", "current_page": page_num, "total_pages": res.total_pages_exposed},
            )
            res.steam_search_requests += 1
            try:
                data, _ = self.http.get_json(self.STEAM_SEARCH_URL, source="steam_search", params=params)
            except ResilientHTTPError as exc:
                err = f"Steam search page {page_num} failed: {exc}"
                res.errors.append(err)
                cb("SOURCE_ERROR", err, {"source": "steam_search", "page": page_num})
                break

            if not isinstance(data, dict) or "results_html" not in data:
                err = f"Steam search page {page_num} returned unexpected payload structure"
                res.errors.append(err)
                cb("SOURCE_ERROR", err, {"source": "steam_search", "page": page_num})
                break

            res.pages_retrieved += 1
            total_count = data.get("total_count")
            if isinstance(total_count, int) and total_count >= 0:
                res.filtered_promo_total = total_count
                res.total_pages_exposed = max(1, math.ceil(total_count / page_size)) if total_count > 0 else 1

            rows = parse_steam_search_html(data.get("results_html") or "")
            res.candidates.extend(rows)
            cb(
                "STEAM_SEARCH_PAGE_COMPLETED",
                f"Steam search page {page_num} parsed: {len(rows)} candidate(s) found",
                {
                    "source": "steam_search",
                    "current_page": page_num,
                    "pages_retrieved": res.pages_retrieved,
                    "total_pages": res.total_pages_exposed,
                    "page_candidates": len(rows),
                },
            )

            start += page_size
            if not rows:
                break
            if res.filtered_promo_total is not None and start >= res.filtered_promo_total:
                break

    def _discover_cheapshark(self, res: DiscoveryResult, cb: ProgressCallback) -> None:
        res.sources_checked.append("cheapshark")
        page_num = 0
        max_pages = 10  # Safety cap for 100%-off deals

        while page_num < max_pages:
            params = {
                "storeID": "1",  # Steam
                "upperPrice": "0",
                "onSale": "1",
                "pageNumber": str(page_num),
                "pageSize": "60",
            }
            cb(
                "SECONDARY_SOURCE_QUERY",
                f"Querying CheapShark Steam 100%-off deals (page {page_num + 1})",
                {"source": "cheapshark", "page": page_num + 1},
            )
            res.secondary_requests += 1
            try:
                deals, headers = self.http.get_json(self.CHEAPSHARK_DEALS_URL, source="cheapshark", params=params)
            except ResilientHTTPError as exc:
                err = f"CheapShark API failed on page {page_num}: {exc}"
                res.errors.append(err)
                cb("SOURCE_ERROR", err, {"source": "cheapshark"})
                break

            if not isinstance(deals, list):
                err = "CheapShark API returned non-list payload"
                res.errors.append(err)
                cb("SOURCE_ERROR", err, {"source": "cheapshark"})
                break

            for deal in deals:
                if not isinstance(deal, dict):
                    continue
                raw_appid = deal.get("steamAppID")
                app_id = int(raw_appid) if raw_appid and str(raw_appid).isdigit() else None
                title = str(deal.get("title") or "").strip()
                try:
                    normal_price = float(deal.get("normalPrice")) if deal.get("normalPrice") is not None else None
                except (ValueError, TypeError):
                    normal_price = None
                try:
                    sale_price = float(deal.get("salePrice")) if deal.get("salePrice") is not None else None
                except (ValueError, TypeError):
                    sale_price = None
                try:
                    savings = int(round(float(deal.get("savings")))) if deal.get("savings") is not None else None
                except (ValueError, TypeError):
                    savings = None

                is_ftk = (
                    str(deal.get("storeID", "1")) == "1"
                    and normal_price is not None
                    and normal_price > 0
                    and sale_price is not None
                    and sale_price == 0.0
                    and (savings is None or savings >= 99)
                )
                ev = SecondaryEvidence(
                    source_name="cheapshark",
                    matched=True,
                    is_direct_steam_free_to_keep=is_ftk,
                    is_third_party_key_giveaway=False,
                    title=title,
                    steam_app_id=app_id,
                    normal_price=normal_price,
                    sale_price=sale_price,
                    currency="USD",
                    discount_percent=savings,
                    offer_url=f"https://www.cheapshark.com/redirect?dealID={deal.get('dealID', '')}",
                    raw_status="onSale" if str(deal.get("isOnSale", "1")) == "1" else "notOnSale",
                    checked_at=utc_iso(),
                )
                if app_id:
                    res.secondary_by_appid.setdefault(app_id, []).append(ev)
                if title:
                    res.secondary_by_title.setdefault(title.lower(), []).append(ev)

                # Also register as discovery candidate so Steam appdetails can verify it
                if app_id:
                    res.candidates.append(
                        DiscoveredCandidate(
                            app_id=app_id,
                            title=title or f"Steam App {app_id}",
                            source="cheapshark",
                            product_url=f"https://store.steampowered.com/app/{app_id}/",
                            search_original_price=f"${normal_price:.2f}" if normal_price is not None else None,
                            search_final_price=f"${sale_price:.2f}" if sale_price is not None else None,
                            search_discount_pct=savings,
                        )
                    )

            total_pages_hdr = headers.get("X-Total-Page-Count") or headers.get("x-total-page-count")
            if total_pages_hdr and str(total_pages_hdr).isdigit():
                if page_num >= int(total_pages_hdr):
                    break
            if len(deals) < 60:
                break
            page_num += 1

    def _discover_gamerpower(self, res: DiscoveryResult, cb: ProgressCallback) -> None:
        res.sources_checked.append("gamerpower")
        cb(
            "SECONDARY_SOURCE_QUERY",
            "Querying GamerPower Steam game giveaways feed",
            {"source": "gamerpower"},
        )
        res.secondary_requests += 1
        try:
            items, _ = self.http.get_json(
                self.GAMERPOWER_GIVEAWAYS_URL,
                source="gamerpower",
                params={"platform": "steam", "type": "game"},
            )
        except ResilientHTTPError as exc:
            # GamerPower returns 201 / empty or 404 when no giveaways exist; treat HTTP errors explicitly
            err = f"GamerPower API failed: {exc}"
            res.errors.append(err)
            cb("SOURCE_ERROR", err, {"source": "gamerpower"})
            return

        if isinstance(items, dict) and items.get("status") == 0:
            # GamerPower returns {"status": 0, "status_message": "No active giveaways..."} when empty
            return
        if not isinstance(items, list):
            err = "GamerPower API returned unexpected non-list payload"
            res.errors.append(err)
            cb("SOURCE_ERROR", err, {"source": "gamerpower"})
            return

        for g in items:
            if not isinstance(g, dict):
                continue
            title = str(g.get("title") or "").strip()
            open_url = str(g.get("open_giveaway_url") or g.get("gamerpower_url") or "").strip()
            instructions = str(g.get("instructions") or "")
            app_id = _extract_steam_app_id(open_url) or _extract_steam_app_id(instructions)
            is_direct_steam = bool(STEAM_APP_URL_RE.search(open_url))
            is_third_party = not is_direct_steam

            worth_str = str(g.get("worth") or "").strip()
            worth_val: Optional[float] = None
            m = re.search(r"(\d+(?:\.\d+)?)", worth_str)
            if m:
                try:
                    worth_val = float(m.group(1))
                except ValueError:
                    worth_val = None

            status_str = str(g.get("status") or "").strip()
            end_date = str(g.get("end_date") or "").strip()
            if end_date.upper() == "N/A":
                end_date = ""

            ev = SecondaryEvidence(
                source_name="gamerpower",
                matched=True,
                is_direct_steam_free_to_keep=(is_direct_steam and status_str.lower() == "active"),
                is_third_party_key_giveaway=is_third_party,
                title=title,
                steam_app_id=app_id,
                normal_price=worth_val,
                sale_price=0.0 if status_str.lower() == "active" else None,
                currency="USD" if "$" in worth_str else None,
                discount_percent=100 if (worth_val and worth_val > 0) else None,
                offer_url=open_url,
                expiration_text=end_date or None,
                raw_status=status_str,
                checked_at=utc_iso(),
            )
            if app_id:
                res.secondary_by_appid.setdefault(app_id, []).append(ev)
            if title:
                res.secondary_by_title.setdefault(title.lower(), []).append(ev)

            # Add as candidate so third-party giveaways are explicitly classified (Quarantined)
            res.candidates.append(
                DiscoveredCandidate(
                    app_id=app_id,
                    title=title or "Untitled Giveaway",
                    source="gamerpower",
                    product_url=f"https://store.steampowered.com/app/{app_id}/" if app_id else open_url,
                    search_original_price=worth_str if worth_str and worth_str != "N/A" else None,
                    search_final_price="Free" if status_str.lower() == "active" else None,
                    search_discount_pct=100 if (worth_val and worth_val > 0) else None,
                    is_third_party_giveaway=is_third_party,
                    giveaway_url=open_url,
                    expiration_hint=end_date or None,
                )
            )

    def fetch_steam_appdetails(self, app_id: int) -> tuple[Optional[Dict[str, Any]], Optional[str]]:
        """Fetch official Steam appdetails for a single App ID in the configured region."""
        params = {
            "appids": str(app_id),
            "cc": self.config.steam_country_code,
            "l": self.config.steam_language,
        }
        try:
            payload, _ = self.http.get_json(self.STEAM_APPDETAILS_URL, source="steam_appdetails", params=params)
        except ResilientHTTPError as exc:
            return None, str(exc)

        if not isinstance(payload, dict) or str(app_id) not in payload:
            return None, f"Steam appdetails response missing key '{app_id}'"

        entry = payload[str(app_id)]
        if not isinstance(entry, dict) or not entry.get("success"):
            return None, f"Steam appdetails returned success=false for App ID {app_id}"

        data = entry.get("data")
        if not isinstance(data, dict):
            return None, f"Steam appdetails returned missing data object for App ID {app_id}"
        return data, None

    def fetch_steam_store_page_signals(self, app_id: int) -> tuple[Optional[Dict[str, Any]], Optional[str]]:
        """Fetch Steam Store product page HTML to confirm Free-to-Keep retention wording & expiry."""
        url = f"https://store.steampowered.com/app/{app_id}/"
        params = {"cc": self.config.steam_country_code, "l": self.config.steam_language}
        headers = {"Cookie": "birthtime=568022401; lastagecheckage=1-0-1988; wants_mature_content=1"}
        try:
            html, _ = self.http.get_text(url, source="steam_store_page", params=params, headers=headers)
        except ResilientHTTPError as exc:
            return None, str(exc)
        return parse_steam_store_page(html), None
