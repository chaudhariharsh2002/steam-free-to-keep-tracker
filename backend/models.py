"""Domain models, enums, and time helpers for Steam Free-to-Keep Tracker."""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone, timedelta
from enum import Enum
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo


IST_TZ = ZoneInfo("Asia/Kolkata")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_iso(dt: Optional[datetime] = None) -> str:
    d = dt or utc_now()
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def ist_iso(dt: Optional[datetime] = None) -> str:
    d = dt or utc_now()
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.astimezone(IST_TZ).isoformat()


def format_ist_human(iso_str: Optional[str]) -> str:
    if not iso_str:
        return "N/A"
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        return dt.astimezone(IST_TZ).strftime("%d %b %Y, %I:%M:%S %p IST")
    except Exception:
        return iso_str


class ExecutionOutcome(str, Enum):
    SUCCESS_WITH_RESULTS = "SUCCESS_WITH_RESULTS"
    SUCCESS_NO_RESULTS = "SUCCESS_NO_RESULTS"
    COMPLETED_WITH_ERRORS = "COMPLETED_WITH_ERRORS"
    FAILED = "FAILED"
    RUNNING = "RUNNING"
    IDLE = "IDLE"
    AWAITING_FIRST_SCAN = "AWAITING_FIRST_SCAN"


class ClassificationStatus(str, Enum):
    VERIFIED = "VERIFIED"
    QUARANTINED = "QUARANTINED"
    REJECTED = "REJECTED"


class GateStatus(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    UNCERTAIN = "UNCERTAIN"


@dataclass
class GateResult:
    gate_number: int
    gate_name: str
    status: GateStatus
    evidence: str
    source: str
    timestamp: str
    reason: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "gate_number": self.gate_number,
            "gate_name": self.gate_name,
            "status": self.status.value,
            "evidence": self.evidence,
            "source": self.source,
            "timestamp": self.timestamp,
            "reason": self.reason,
        }


@dataclass
class SecondaryEvidence:
    source_name: str  # "cheapshark" | "gamerpower"
    matched: bool = False
    is_direct_steam_free_to_keep: bool = False
    is_third_party_key_giveaway: bool = False
    title: Optional[str] = None
    steam_app_id: Optional[int] = None
    normal_price: Optional[float] = None
    sale_price: Optional[float] = None
    currency: Optional[str] = None
    discount_percent: Optional[int] = None
    offer_url: Optional[str] = None
    expiration_text: Optional[str] = None
    raw_status: Optional[str] = None
    error: Optional[str] = None
    checked_at: str = field(default_factory=utc_iso)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class DiscoveredCandidate:
    app_id: Optional[int]
    title: str
    source: str  # "steam_search" | "cheapshark" | "gamerpower"
    product_url: str
    search_original_price: Optional[str] = None
    search_final_price: Optional[str] = None
    search_discount_pct: Optional[int] = None
    is_third_party_giveaway: bool = False
    giveaway_url: Optional[str] = None
    expiration_hint: Optional[str] = None
    raw_metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ValidationRecord:
    app_id: Optional[int]
    title: str
    final_status: ClassificationStatus
    original_price_minor: Optional[int]  # paise / cents
    current_price_minor: Optional[int]
    original_price_formatted: str
    current_price_formatted: str
    discount_percent: Optional[int]
    currency: str
    region: str
    product_type: str
    claim_url: str
    expiration_at: Optional[str]
    primary_source: str
    secondary_sources: List[Dict[str, Any]]
    gates: List[GateResult]
    decision_reason: str
    rejection_reasons: List[str] = field(default_factory=list)
    quarantine_reasons: List[str] = field(default_factory=list)
    checked_at_utc: str = field(default_factory=utc_iso)
    checked_at_ist: str = field(default_factory=ist_iso)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "app_id": self.app_id,
            "title": self.title,
            "final_status": self.final_status.value,
            "original_price_minor": self.original_price_minor,
            "current_price_minor": self.current_price_minor,
            "original_price_formatted": self.original_price_formatted,
            "current_price_formatted": self.current_price_formatted,
            "discount_percent": self.discount_percent,
            "currency": self.currency,
            "region": self.region,
            "product_type": self.product_type,
            "claim_url": self.claim_url,
            "expiration_at": self.expiration_at,
            "primary_source": self.primary_source,
            "secondary_sources": self.secondary_sources,
            "gates": [g.to_dict() for g in self.gates],
            "decision_reason": self.decision_reason,
            "rejection_reasons": self.rejection_reasons,
            "quarantine_reasons": self.quarantine_reasons,
            "checked_at_utc": self.checked_at_utc,
            "checked_at_ist": self.checked_at_ist,
        }


def compute_next_scheduled_iso(interval_minutes: int, reference_utc: Optional[datetime] = None) -> str:
    ref = reference_utc or utc_now()
    return utc_iso(ref + timedelta(minutes=interval_minutes))
