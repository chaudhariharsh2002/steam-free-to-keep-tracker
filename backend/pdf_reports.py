"""Server-side PDF report generator using ReportLab.

Generates:
1. Individual Hourly Cycle Audit PDF Reports containing all 20 mandatory fields.
2. Full Historical Audit PDF Report across all recorded cycles and game history.
"""
from __future__ import annotations

import io
from typing import Any, Dict, List

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from backend.models import format_ist_human, utc_iso


DARK_NAVY = colors.HexColor("#0F172A")
SLATE_BG = colors.HexColor("#F1F5F9")
ACCENT_BLUE = colors.HexColor("#1D4ED8")
GREEN_OK = colors.HexColor("#15803D")
AMBER_WARN = colors.HexColor("#B45309")
RED_FAIL = colors.HexColor("#B91C1C")
BORDER_GRAY = colors.HexColor("#CBD5E1")


def _esc(val: Any) -> str:
    s = str(val if val is not None else "N/A")
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _build_styles():
    styles = getSampleStyleSheet()
    return {
        "title": ParagraphStyle(
            "ReportTitle",
            parent=styles["Heading1"],
            fontSize=16,
            leading=20,
            textColor=DARK_NAVY,
            spaceAfter=6,
        ),
        "subtitle": ParagraphStyle(
            "ReportSub",
            parent=styles["Normal"],
            fontSize=9.5,
            leading=13,
            textColor=colors.HexColor("#475569"),
            spaceAfter=10,
        ),
        "h2": ParagraphStyle(
            "SectionH2",
            parent=styles["Heading2"],
            fontSize=11.5,
            leading=15,
            textColor=ACCENT_BLUE,
            spaceBefore=10,
            spaceAfter=4,
        ),
        "body": ParagraphStyle(
            "BodyTextSmall",
            parent=styles["Normal"],
            fontSize=8.5,
            leading=11.5,
            textColor=DARK_NAVY,
        ),
        "cell": ParagraphStyle(
            "CellText",
            parent=styles["Normal"],
            fontSize=7.8,
            leading=10,
            textColor=DARK_NAVY,
        ),
    }


def generate_cycle_pdf_bytes(cycle: Dict[str, Any]) -> bytes:
    """Build a complete individual hourly cycle audit PDF with all 20 required fields."""
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=A4,
        leftMargin=14 * mm,
        rightMargin=14 * mm,
        topMargin=14 * mm,
        bottomMargin=14 * mm,
        title=f"Steam Free-to-Keep Cycle #{cycle['cycle_number']} Audit Report",
    )
    st = _build_styles()
    story: List[Any] = []

    # 1. Report Title, 2. Scan ID, 3. Cycle Number, 4. Date & Time, 5. Region
    story.append(
        Paragraph(
            f"Steam Free-to-Keep Tracker — Hourly Audit Report (Cycle #{_esc(cycle['cycle_number'])})",
            st["title"],
        )
    )
    story.append(
        Paragraph(
            f"Scan ID: <b>{_esc(cycle['scan_id'])}</b> | "
            f"Region: <b>{_esc(cycle['region'])} ({_esc(cycle['currency'])})</b> | "
            f"Execution Status: <b>{_esc(cycle['status'])}</b>",
            st["subtitle"],
        )
    )

    # Core Execution Metadata Table (Fields 2-9, 18-20)
    pages_total = cycle.get("total_pages_exposed")
    pages_text = f"{cycle.get('pages_retrieved', 0)} / {pages_total if pages_total is not None else 'UNKNOWN'}"
    meta_rows = [
        ["Cycle Number", str(cycle["cycle_number"]), "Scan ID", _esc(cycle["scan_id"])],
        ["Started (IST)", format_ist_human(cycle.get("started_at_utc")), "Completed (IST)", format_ist_human(cycle.get("completed_at_utc"))],
        ["Started (UTC)", _esc(cycle.get("started_at_utc")), "Scan Duration", f"{cycle.get('duration_seconds', 0):.2f} s"],
        ["Target Region", f"{_esc(cycle['region'])} ({_esc(cycle['currency'])})", "Final Status", _esc(cycle["status"])],
        ["Sources Examined", ", ".join(cycle.get("sources_checked") or ["None"]), "Pages Retrieved", pages_text],
        ["Candidates Discovered", str(cycle.get("candidates_discovered", 0)), "Games Individually Checked", str(cycle.get("games_checked", 0))],
        ["Verified / Quarantined / Rejected", f"{cycle.get('verified_count', 0)} / {cycle.get('quarantined_count', 0)} / {cycle.get('rejected_count', 0)}", "Notification Status", _esc(cycle.get("notification_status", "NONE"))],
        ["Requests (OK / Fail / Retry)", f"{cycle.get('completed_requests', 0)} / {cycle.get('failed_requests', 0)} / {cycle.get('retried_requests', 0)}", "Data Freshness", _esc(cycle.get("data_freshness", "FRESH"))],
    ]
    t_meta = Table(meta_rows, colWidths=[38 * mm, 53 * mm, 38 * mm, 53 * mm])
    t_meta.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (0, -1), SLATE_BG),
                ("BACKGROUND", (2, 0), (2, -1), SLATE_BG),
                ("FONTNAME", (0, 0), (0, -1), "Helvetica-Bold"),
                ("FONTNAME", (2, 0), (2, -1), "Helvetica-Bold"),
                ("FONTSIZE", (0, 0), (-1, -1), 8),
                ("GRID", (0, 0), (-1, -1), 0.4, BORDER_GRAY),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 5),
                ("RIGHTPADDING", (0, 0), (-1, -1), 5),
                ("TOPPADDING", (0, 0), (-1, -1), 4),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ]
        )
    )
    story.append(t_meta)
    story.append(Spacer(1, 6))

    if cycle.get("coverage_disclosure"):
        story.append(Paragraph(f"<b>Coverage Disclosure:</b> {_esc(cycle['coverage_disclosure'])}", st["body"]))
        story.append(Spacer(1, 6))

    evals: List[Dict[str, Any]] = cycle.get("evaluations") or []
    verified = [e for e in evals if e["final_status"] == "VERIFIED"]
    quarantined = [e for e in evals if e["final_status"] == "QUARANTINED"]
    rejected = [e for e in evals if e["final_status"] == "REJECTED"]

    def _add_eval_section(heading: str, items: List[Dict[str, Any]], empty_msg: str) -> None:
        story.append(Paragraph(f"{heading} ({len(items)})", st["h2"]))
        if not items:
            story.append(Paragraph(empty_msg, st["body"]))
            return
        rows = [["Game Title", "App ID", "Orig. Price", "Curr. Price", "Decision & Validation Evidence"]]
        for ev in items:
            gates = ev.get("gates") or []
            pass_c = sum(1 for g in gates if g.get("status") == "PASS")
            unc_c = sum(1 for g in gates if g.get("status") == "UNCERTAIN")
            fail_c = sum(1 for g in gates if g.get("status") == "FAIL")
            ev_summary = (
                f"<b>{_esc(ev.get('decision_reason', ''))}</b><br/>"
                f"Gates: {pass_c} PASS, {unc_c} UNCERTAIN, {fail_c} FAIL | "
                f"URL: {_esc(ev.get('claim_url', 'N/A'))}"
            )
            rows.append(
                [
                    Paragraph(_esc(ev.get("title")), st["cell"]),
                    Paragraph(_esc(ev.get("app_id") or "N/A"), st["cell"]),
                    Paragraph(_esc(ev.get("original_price_formatted")), st["cell"]),
                    Paragraph(_esc(ev.get("current_price_formatted")), st["cell"]),
                    Paragraph(ev_summary, st["cell"]),
                ]
            )
        tbl = Table(rows, colWidths=[38 * mm, 18 * mm, 22 * mm, 22 * mm, 82 * mm], repeatRows=1)
        tbl.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, 0), DARK_NAVY),
                    ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                    ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                    ("FONTSIZE", (0, 0), (-1, 0), 8),
                    ("GRID", (0, 0), (-1, -1), 0.4, BORDER_GRAY),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("LEFTPADDING", (0, 0), (-1, -1), 4),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 4),
                    ("TOPPADDING", (0, 0), (-1, -1), 4),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
                ]
            )
        )
        story.append(tbl)

    # Fields 10-16: Verified, Quarantined, Rejected, Prices, Validation Evidence, Reasons
    _add_eval_section("Verified Direct Steam Free-to-Keep Games", verified, "No verified Direct Steam Free-to-Keep promotions detected in this cycle.")
    _add_eval_section("Quarantined Opportunities", quarantined, "No quarantined candidates in this cycle.")
    _add_eval_section("Rejected Candidates", rejected, "No rejected candidates in this cycle.")

    # Field 17: Errors & Retries
    errors = cycle.get("errors") or []
    story.append(Paragraph(f"Errors & Retries ({len(errors)} error(s), {cycle.get('retried_requests', 0)} retry attempt(s))", st["h2"]))
    if errors:
        for err in errors:
            story.append(Paragraph(f"• {_esc(err)}", st["body"]))
    else:
        story.append(Paragraph("No execution errors recorded during this cycle.", st["body"]))

    story.append(Spacer(1, 8))
    story.append(
        Paragraph(
            f"Generated automatically from persisted audit database at {format_ist_human(utc_iso())}.",
            st["subtitle"],
        )
    )

    doc.build(story)
    return buf.getvalue()


def generate_historical_pdf_bytes(
    cycles: List[Dict[str, Any]],
    games: List[Dict[str, Any]],
    stats: Dict[str, Any],
) -> bytes:
    """Build a complete historical audit PDF across all completed cycles and game inventory."""
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=A4,
        leftMargin=14 * mm,
        rightMargin=14 * mm,
        topMargin=14 * mm,
        bottomMargin=14 * mm,
        title="Steam Free-to-Keep Tracker — Complete Historical Audit Report",
    )
    st = _build_styles()
    story: List[Any] = []

    story.append(Paragraph("Steam Free-to-Keep Tracker — Complete Historical Audit Report", st["title"]))
    story.append(
        Paragraph(
            f"Generated: <b>{format_ist_human(utc_iso())}</b> | "
            f"Total Completed Cycles: <b>{stats.get('total_completed_cycles', 0)}</b> | "
            f"Historical Games Tracked: <b>{len(games)}</b>",
            st["subtitle"],
        )
    )

    # Aggregate Statistics Table
    stat_rows = [
        ["Completed Cycles", str(stats.get("total_completed_cycles", 0)), "Pages Retrieved", str(stats.get("total_pages_retrieved", 0))],
        ["Candidates Discovered", str(stats.get("total_candidates_discovered", 0)), "Games Checked", str(stats.get("total_games_checked", 0))],
        ["Active Verified Offers", str(stats.get("active_verified_opportunities", 0)), "Total Verified (Incl. Expired)", str(stats.get("total_verified_opportunities", 0))],
        ["Quarantined Games", str(stats.get("total_quarantined_opportunities", 0)), "Rejected Candidates", str(stats.get("total_rejected_candidates", 0))],
        ["Notifications Delivered", str(stats.get("total_notifications_delivered", 0)), "Total Request Errors", str(stats.get("total_errors", 0))],
    ]
    t_stat = Table(stat_rows, colWidths=[42 * mm, 49 * mm, 42 * mm, 49 * mm])
    t_stat.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (0, -1), SLATE_BG),
                ("BACKGROUND", (2, 0), (2, -1), SLATE_BG),
                ("FONTNAME", (0, 0), (0, -1), "Helvetica-Bold"),
                ("FONTNAME", (2, 0), (2, -1), "Helvetica-Bold"),
                ("FONTSIZE", (0, 0), (-1, -1), 8),
                ("GRID", (0, 0), (-1, -1), 0.4, BORDER_GRAY),
                ("LEFTPADDING", (0, 0), (-1, -1), 5),
                ("TOPPADDING", (0, 0), (-1, -1), 4),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ]
        )
    )
    story.append(t_stat)

    # Historical Game Inventory Table
    story.append(Paragraph(f"Complete Historical Game Inventory ({len(games)})", st["h2"]))
    if games:
        g_rows = [["Title", "App ID", "Status", "Orig. Price", "Promo Price", "First Seen (IST)", "Claimed"]]
        for g in games[:500]:
            g_rows.append(
                [
                    Paragraph(_esc(g.get("title")), st["cell"]),
                    Paragraph(_esc(g.get("app_id") or "N/A"), st["cell"]),
                    Paragraph(_esc(g.get("current_status")), st["cell"]),
                    Paragraph(_esc(g.get("original_price_formatted")), st["cell"]),
                    Paragraph(_esc(g.get("promotional_price_formatted")), st["cell"]),
                    Paragraph(format_ist_human(g.get("first_discovered_utc")), st["cell"]),
                    Paragraph("YES" if g.get("manual_claimed") else "NO", st["cell"]),
                ]
            )
        t_games = Table(g_rows, colWidths=[44 * mm, 16 * mm, 22 * mm, 20 * mm, 20 * mm, 44 * mm, 16 * mm], repeatRows=1)
        t_games.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, 0), DARK_NAVY),
                    ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                    ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                    ("FONTSIZE", (0, 0), (-1, 0), 7.8),
                    ("GRID", (0, 0), (-1, -1), 0.4, BORDER_GRAY),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ]
            )
        )
        story.append(t_games)
    else:
        story.append(Paragraph("No historical game records yet.", st["body"]))

    # Hourly Scan Ledger Table
    story.append(Paragraph(f"Permanent Hourly Scan Ledger ({len(cycles)} cycle(s))", st["h2"]))
    if cycles:
        c_rows = [["Cycle", "Scan ID", "Started (IST)", "Status", "Pages", "Checked", "V/Q/R", "Dur(s)"]]
        for c in cycles[:500]:
            c_rows.append(
                [
                    Paragraph(str(c["cycle_number"]), st["cell"]),
                    Paragraph(_esc(c["scan_id"]), st["cell"]),
                    Paragraph(format_ist_human(c.get("started_at_utc")), st["cell"]),
                    Paragraph(_esc(c["status"]), st["cell"]),
                    Paragraph(str(c.get("pages_retrieved", 0)), st["cell"]),
                    Paragraph(str(c.get("games_checked", 0)), st["cell"]),
                    Paragraph(f"{c.get('verified_count', 0)}/{c.get('quarantined_count', 0)}/{c.get('rejected_count', 0)}", st["cell"]),
                    Paragraph(f"{c.get('duration_seconds', 0):.1f}", st["cell"]),
                ]
            )
        t_cyc = Table(c_rows, colWidths=[14 * mm, 32 * mm, 40 * mm, 34 * mm, 14 * mm, 16 * mm, 18 * mm, 14 * mm], repeatRows=1)
        t_cyc.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, 0), DARK_NAVY),
                    ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                    ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                    ("FONTSIZE", (0, 0), (-1, 0), 7.8),
                    ("GRID", (0, 0), (-1, -1), 0.4, BORDER_GRAY),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ]
            )
        )
        story.append(t_cyc)
    else:
        story.append(Paragraph("No completed scan cycles yet.", st["body"]))

    doc.build(story)
    return buf.getvalue()
