"""PDF text extraction and BOQ spreadsheet parsing.

Deterministic parsing wherever structure exists, probabilistic extraction only
where it does not. A GePNIC/GeM BOQ is often a known XLS template — parsing it as
a spreadsheet is both cheaper and more accurate than asking a model to read it.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

OCR_TEXT_DENSITY_THRESHOLD = 50  # characters per page


@dataclass
class PdfExtract:
    text: str
    page_count: int
    char_count: int
    density: float
    needs_ocr: bool
    pages: list[str] = field(default_factory=list)

    @property
    def has_text_layer(self) -> bool:
        return not self.needs_ocr


def extract_pdf(path: str | Path) -> PdfExtract:
    """Extract the text layer and classify the document.

    A scanned page carries almost no characters; those are routed to OCR in a
    later stage rather than silently producing an empty record.
    """
    import pymupdf  # imported lazily so the app boots without the dependency

    doc = pymupdf.open(str(path))
    pages = [page.get_text("text") or "" for page in doc]
    text = "\n".join(pages)
    page_count = max(1, doc.page_count)
    char_count = len(text.strip())
    density = char_count / page_count
    return PdfExtract(
        text=text,
        page_count=page_count,
        char_count=char_count,
        density=round(density, 1),
        needs_ocr=density < OCR_TEXT_DENSITY_THRESHOLD,
        pages=pages,
    )


def page_of_offset(pages: list[str], offset: int) -> int:
    """Map a character offset in the joined text to a 1-based page number."""
    running = 0
    for i, page in enumerate(pages, start=1):
        running += len(page) + 1  # the newline joiner
        if offset < running:
            return i
    return max(1, len(pages))


# --------------------------------------------------------------------------- #
# BOQ spreadsheets
# --------------------------------------------------------------------------- #
HEADER_HINTS = {
    "description": ("description", "item description", "item", "particulars", "material"),
    "quantity": ("quantity", "qty", "no. of units"),
    "unit": ("unit", "uom", "units"),
    "rate": ("rate", "unit rate", "price"),
    "hsn": ("hsn", "hsn code"),
}


def _match_header(value: str) -> str | None:
    low = (value or "").strip().lower()
    for key, hints in HEADER_HINTS.items():
        if any(h == low or (h in low and len(low) <= len(h) + 6) for h in hints):
            return key
    return None


def parse_boq_xlsx(path: str | Path, max_rows: int = 500) -> list[dict]:
    """Parse a BOQ spreadsheet into line items.

    Finds the header row by looking for a description column, then reads the
    columns that follow. Returns [] rather than raising when the sheet is not a
    BOQ — the caller decides what to do with that.
    """
    from openpyxl import load_workbook

    wb = load_workbook(str(path), read_only=True, data_only=True)
    items: list[dict] = []
    for sheet in wb.worksheets:
        rows = list(sheet.iter_rows(values_only=True))
        header_idx = None
        mapping: dict[str, int] = {}
        for i, row in enumerate(rows[:40]):
            found = {}
            for j, cell in enumerate(row):
                key = _match_header(str(cell) if cell is not None else "")
                if key and key not in found:
                    found[key] = j
            if "description" in found:
                header_idx, mapping = i, found
                break
        if header_idx is None:
            continue
        for row in rows[header_idx + 1 : header_idx + 1 + max_rows]:
            if row is None:
                continue
            desc = row[mapping["description"]] if mapping.get("description") is not None else None
            if not desc or not str(desc).strip():
                continue
            if re.fullmatch(r"[\d\s.,/-]+", str(desc).strip()):
                continue  # a totals row, not an item
            qty = row[mapping["quantity"]] if "quantity" in mapping else None
            items.append(
                {
                    "description": str(desc).strip()[:1000],
                    "quantity": _num(qty),
                    "unit": str(row[mapping["unit"]]).strip()[:32] if "unit" in mapping and row[mapping["unit"]] else None,
                    "rate": _num(row[mapping["rate"]]) if "rate" in mapping else None,
                    "hsn_code": str(row[mapping["hsn"]]).strip()[:16] if "hsn" in mapping and row[mapping["hsn"]] else None,
                    "sheet": sheet.title,
                }
            )
    wb.close()
    return items


def _num(value) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).replace(",", "").strip())
    except ValueError:
        return None
