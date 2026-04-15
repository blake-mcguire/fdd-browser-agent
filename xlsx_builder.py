"""
Two-tab XLSX builder: Company Lead Sheet + Person Lead Sheet.
Also writes JSONL audit trail.
"""

import io
import json
import logging
from pathlib import Path

import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment

from models import EntityRecord

logger = logging.getLogger("fdd-agent")

# ── Styles ────────────────────────────────────────────────────
HEADER_FILL = PatternFill("solid", fgColor="1F4E79")
HEADER_FONT = Font(bold=True, color="FFFFFF", size=10)
ALT_FILL = PatternFill("solid", fgColor="D6E4F0")
WHITE_FILL = PatternFill("solid", fgColor="FFFFFF")
BODY_FONT = Font(size=10)
CENTER = Alignment(horizontal="center", vertical="center", wrap_text=True)
LEFT = Alignment(horizontal="left", vertical="center", wrap_text=True)


def build_xlsx(records: list[EntityRecord]) -> bytes:
    """
    Build a two-tab XLSX:
      Tab 1: Company Lead Sheet (one row per entity)
      Tab 2: Person Lead Sheet (one row per person per entity)
    """
    wb = openpyxl.Workbook()

    # ── Tab 1: Company Lead Sheet ─────────────────────────────
    ws_co = wb.active
    ws_co.title = "Company Leads"

    # Determine max officers across all entities for dynamic columns
    max_officers = max((len(rec.people) for rec in records), default=0)
    max_officers = max(max_officers, 1)  # at least 1 slot

    # Static columns before officers
    static_cols_before = [
        "Entity Name", "# Locations", "States of Operation", "Primary State",
        "State of Formation", "Entity Status", "Entity Type", "Formation Date",
        "Registered Agent", "Agent Address",
    ]

    # Dynamic officer columns: Officer 1 Name, Officer 1 Title, Officer 1 Address, ...
    officer_cols = []
    for i in range(1, max_officers + 1):
        officer_cols.extend([
            f"Officer {i} Name",
            f"Officer {i} Title",
            f"Officer {i} Address",
        ])

    # Static columns after officers
    static_cols_after = [
        "Company Website", "Recent News",
        "Key Developments", "Risk Signals", "Company Notes",
        "Original Notes", "Source File", "Franchisor",
        "SOS Confidence", "SOS Source URL",
    ]

    company_cols = static_cols_before + officer_cols + static_cols_after
    _write_header(ws_co, company_cols)

    for row_idx, rec in enumerate(records, 2):
        fill = ALT_FILL if row_idx % 2 == 0 else WHITE_FILL

        co = rec.company

        # Static data before officers
        row_data = [
            rec.entity_name,
            rec.num_locations,
            rec.all_states,
            rec.state,
            rec.state,  # state_of_formation (from SOS, same as primary for now)
            rec.entity_status,
            rec.entity_type,
            rec.formation_date,
            rec.registered_agent,
            rec.agent_address,
        ]

        # Officer columns (3 per officer slot)
        for i in range(max_officers):
            if i < len(rec.people):
                p = rec.people[i]
                row_data.extend([p.name, p.title, p.address])
            else:
                row_data.extend(["", "", ""])

        # Static data after officers
        row_data.extend([
            co.website if co else "",
            co.recent_news_summary if co else "",
            "; ".join(co.key_developments) if co and co.key_developments else "",
            "; ".join(co.risk_signals) if co and co.risk_signals else "",
            co.notes if co else "",
            rec.original_notes,
            rec.source_file,
            rec.franchisor,
            rec.sos_confidence,
            rec.sos_source_url,
        ])

        for col_idx, value in enumerate(row_data, 1):
            cell = ws_co.cell(row=row_idx, column=col_idx, value=value or "")
            cell.fill = fill
            cell.font = BODY_FONT
            cell.alignment = LEFT

    # Column widths for company sheet
    co_widths_before = [40, 8, 20, 8, 8, 12, 14, 14, 30, 40]
    co_widths_officers = [30, 18, 40] * max_officers
    co_widths_after = [30, 50, 50, 30, 40, 40, 20, 20, 12, 50]
    co_widths = co_widths_before + co_widths_officers + co_widths_after

    for col_idx, width in enumerate(co_widths, 1):
        ws_co.column_dimensions[openpyxl.utils.get_column_letter(col_idx)].width = width
    ws_co.freeze_panes = "A2"
    ws_co.auto_filter.ref = ws_co.dimensions

    # ── Tab 2: Person Lead Sheet ──────────────────────────────
    ws_p = wb.create_sheet("Person Leads")

    person_cols = [
        "Entity Name", "Person Name", "Title", "SOS Address",
        "LinkedIn URL", "LinkedIn Location", "LinkedIn Headline",
        "Phone", "Email", "Home Address", "Background",
        "Years with Org", "Source File",
    ]

    _write_header(ws_p, person_cols)

    p_row = 2
    for rec in records:
        fill_base = p_row  # track for alternating colors
        for pr in rec.person_results:
            fill = ALT_FILL if p_row % 2 == 0 else WHITE_FILL
            row_data = [
                pr.entity_name,
                pr.person_name,
                pr.title,
                pr.sos_address,
                pr.linkedin_url,
                pr.linkedin_location,
                pr.linkedin_headline,
                pr.personal_phone or pr.business_phone,
                pr.email,
                pr.home_address,
                pr.background,
                pr.years_with_org,
                rec.source_file,
            ]
            for col_idx, value in enumerate(row_data, 1):
                cell = ws_p.cell(row=p_row, column=col_idx, value=value or "")
                cell.fill = fill
                cell.font = BODY_FONT
                cell.alignment = LEFT
            p_row += 1

        # Also include people from SOS that had no enrichment results
        enriched_names = {pr.person_name.lower() for pr in rec.person_results}
        for pe in rec.people:
            if pe.name.lower() not in enriched_names:
                fill = ALT_FILL if p_row % 2 == 0 else WHITE_FILL
                row_data = [
                    rec.entity_name, pe.name, pe.title, pe.address,
                    "", "", "", "", "", "", "", "", rec.source_file,
                ]
                for col_idx, value in enumerate(row_data, 1):
                    cell = ws_p.cell(row=p_row, column=col_idx, value=value or "")
                    cell.fill = fill
                    cell.font = BODY_FONT
                    cell.alignment = LEFT
                p_row += 1

    p_widths = [40, 30, 18, 40, 40, 25, 35, 16, 30, 40, 60, 10, 20]
    for col_idx, width in enumerate(p_widths, 1):
        ws_p.column_dimensions[openpyxl.utils.get_column_letter(col_idx)].width = width
    ws_p.freeze_panes = "A2"
    if ws_p.dimensions:
        ws_p.auto_filter.ref = ws_p.dimensions

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _write_header(ws, columns: list[str]):
    """Write a styled header row."""
    for col_idx, col_name in enumerate(columns, 1):
        cell = ws.cell(row=1, column=col_idx, value=col_name)
        cell.fill = HEADER_FILL
        cell.font = HEADER_FONT
        cell.alignment = CENTER


def write_audit_trail(records: list[EntityRecord], output_dir: Path, job_id: str):
    """Write a JSONL audit trail file."""
    audit_path = output_dir / f"{job_id}_audit.jsonl"
    with open(audit_path, "w") as f:
        for rec in records:
            line = rec.model_dump(mode="json")
            f.write(json.dumps(line) + "\n")
    logger.info(f"Audit trail written: {audit_path}")
    return audit_path
