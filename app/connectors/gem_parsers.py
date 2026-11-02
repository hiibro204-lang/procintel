"""Pure parsers for GeM pages. No I/O, no database — so they are cheap to test.

The bid-result page is the single most valuable artefact in Indian public
procurement: it publishes the technical evaluation list (every participating
seller and whether they qualified) and the financial evaluation table (qualified
sellers ranked L1, L2, L3 ... with each one's total quoted price).

The parser is written against the page's semantic structure rather than its
markup: it finds the section headings by text, then reads the table that follows.
That survives cosmetic redesigns and fails loudly — via the self-test — when the
structure really changes.
"""
from __future__ import annotations

import re
from typing import Any

from bs4 import BeautifulSoup

from .base import clean, parse_date, parse_int, parse_money, parse_rank

BID_NUMBER_RE = re.compile(r"GEM\s*/\s*\d{4}\s*/\s*[BRA]\s*/\s*\d+", re.I)

SECTION_BID = ("bid details", "bid detail")
SECTION_TECH = ("technical evaluation", "technical evaluation".upper())
SECTION_FIN = ("financial evaluation",)


def _norm(text: str) -> str:
    return clean(text).lower()


def _tables(soup: BeautifulSoup) -> list[dict]:
    """Extract every table as {headers, rows} using header text when present."""
    out = []
    for table in soup.find_all("table"):
        rows = []
        for tr in table.find_all("tr"):
            cells = [clean(td.get_text(" ")) for td in tr.find_all(["td", "th"])]
            if cells:
                rows.append(cells)
        if not rows:
            continue
        headers = [h.lower() for h in rows[0]]
        out.append({"headers": headers, "raw_headers": rows[0], "rows": rows[1:]})
    return out


def _find_table(tables: list[dict], *required: str) -> dict | None:
    for t in tables:
        joined = " | ".join(t["headers"])
        if all(req in joined for req in required):
            return t
    return None


def _col(table: dict, *candidates: str) -> int | None:
    """Column lookup: an exact header match wins over a substring match.

    Without this, "Status" would resolve to "EMD Status" and every bidder would
    look like it had passed technical evaluation.
    """
    for cand in candidates:
        for i, h in enumerate(table["headers"]):
            if h.strip() == cand:
                return i
    for cand in candidates:
        for i, h in enumerate(table["headers"]):
            if cand in h and "emd" not in h and "mse" not in h:
                return i
    return None


def _cell(row: list[str], idx: int | None) -> str | None:
    if idx is None or idx >= len(row):
        return None
    return clean(row[idx]) or None


_SELLER_SUFFIX = re.compile(
    r"\s*\((?:MII|MSE|MSE Social Category[^)]*|Under PMA|PMA)\)\s*$", re.I
)


def canonical_seller_name(name: str | None) -> str:
    """Strip GeM's display suffixes: '(MII)', '(MSE Social Category:OBC)', '(Under PMA)'.

    The technical table and the financial table print the same seller
    differently; without this the platform would create two suppliers for one
    company and split its win record.
    """
    if not name:
        return ""
    cleaned = clean(name)
    while True:
        new = _SELLER_SUFFIX.sub("", cleaned).strip()
        if new == cleaned:
            return cleaned
        cleaned = new


def _kv_pairs(text: str) -> dict[str, str]:
    """Pull 'Label : value' pairs out of the bid-details block."""
    pairs: dict[str, str] = {}
    for line in text.splitlines():
        line = clean(line)
        if ":" not in line:
            continue
        label, _, value = line.partition(":")
        label, value = clean(label).lower(), clean(value)
        if label and value and len(label) < 60:
            pairs.setdefault(label, value)
    return pairs


def _split_address(address: str | None) -> dict[str, str | None]:
    """GeM buyer addresses are 'Name,email,address,city,STATE,pincode,India,phone'."""
    if not address:
        return {}
    parts = [clean(p) for p in address.split(",")]
    out: dict[str, str | None] = {"buyer_name": parts[0] if parts else None}
    for part in parts[1:]:
        if "@" in part and "email" not in out:
            out["buyer_email"] = part
        elif re.fullmatch(r"\d{6}", part):
            out["pincode"] = part
        elif part.isupper() and len(part) > 2 and "state" not in out:
            out["state"] = part.title()
        elif re.fullmatch(r"[\d\-\s]{6,}", part) and "buyer_phone" not in out:
            out["buyer_phone"] = part
    return out


def parse_bid_result(html: str | bytes, url: str = "") -> dict[str, Any]:
    """Parse a GeM bid-result page into a normalised record.

    Returns:
        {
          tender: {...}, bidders: [...], technical_results: [...],
          financial_bids: [...], warnings: [...]
        }
    Raises ValueError if the page does not look like a bid result at all.
    """
    soup = BeautifulSoup(html, "lxml")
    text = soup.get_text("\n")
    warnings: list[str] = []

    m = BID_NUMBER_RE.search(text)
    bid_number = clean(m.group(0)).replace(" ", "") if m else None
    if not bid_number:
        raise ValueError("no GeM bid number found; page is not a bid result")

    kv = _kv_pairs(text)
    tables = _tables(soup)

    tech_table = _find_table(tables, "seller name", "status")
    fin_table = _find_table(tables, "seller name", "total price")
    if tech_table is None and fin_table is None:
        raise ValueError("no evaluation tables found; page structure changed")

    # ---- tender-level fields -------------------------------------------- #
    buyer_bits = _split_address(kv.get("address"))
    title = kv.get("items") or kv.get("item category") or kv.get("bid title")
    category = kv.get("item categories") or kv.get("category")
    tender = {
        "portal_tender_id": bid_number,
        "tender_ref": bid_number,
        "title": title or f"GeM bid {bid_number}",
        "description": category,
        "category_name": category,
        "quantity": parse_int(kv.get("quantity")),
        "published_at": parse_date(kv.get("bid start date / time") or kv.get("bid start date")),
        "closing_at": parse_date(kv.get("bid end date / time") or kv.get("bid end date")),
        "opening_at": parse_date(kv.get("bid opening date / time") or kv.get("bid opening date")),
        "state": buyer_bits.get("state") or kv.get("state"),
        "city": kv.get("city"),
        "pincode": buyer_bits.get("pincode"),
        "department": kv.get("department"),
        "organisation": kv.get("organisation") if kv.get("organisation", "").lower() not in {"n/a", "na"} else None,
        "office": kv.get("office"),
        "ministry": kv.get("ministry"),
        "buyer_name": buyer_bits.get("buyer_name") or kv.get("name"),
        "buyer_email": buyer_bits.get("buyer_email"),
        "buyer_phone": buyer_bits.get("buyer_phone"),
        "bid_status": kv.get("bid status"),
        "portal_url": url or f"https://bidplus.gem.gov.in/bidding/bid/getBidResultView/{bid_number}",
    }
    if not tender["closing_at"]:
        warnings.append("closing date not found")
    if not tender["quantity"]:
        warnings.append("quantity not found")

    # ---- bidders + technical results ------------------------------------ #
    bidders: list[dict] = []
    technical: list[dict] = []
    if tech_table is not None:
        i_name = _col(tech_table, "seller name")
        i_item = _col(tech_table, "offered item")
        i_part = _col(tech_table, "participated on")
        i_emd = _col(tech_table, "emd status")
        i_mse = _col(tech_table, "mse")
        i_mii = _col(tech_table, "mii")
        i_status = _col(tech_table, "status", "seller status")
        for row in tech_table["rows"]:
            name = canonical_seller_name(_cell(row, i_name))
            if not name:
                continue
            status = (_cell(row, i_status) or "unknown").lower()
            make = model = None
            offered = _cell(row, i_item) or ""
            mk = re.search(r"make\s*:\s*([^,;]+)", offered, re.I)
            md = re.search(r"model\s*:\s*([^,;]+)", offered, re.I)
            if mk:
                make = clean(mk.group(1))
            if md:
                model = clean(md.group(1))
            bidders.append(
                {
                    "seller_name_raw": name,
                    "offered_make": make,
                    "offered_model": model,
                    "participated_at": parse_date(_cell(row, i_part)),
                    "emd_status": _cell(row, i_emd),
                    "mse_status": _cell(row, i_mse),
                    "mii_status": _cell(row, i_mii),
                }
            )
            technical.append({"seller_name_raw": name, "status": status})

    # ---- financial ladder ----------------------------------------------- #
    financial: list[dict] = []
    if fin_table is not None:
        i_name = _col(fin_table, "seller name")
        i_price = _col(fin_table, "total price")
        i_rank = _col(fin_table, "rank")
        for row in fin_table["rows"]:
            name = canonical_seller_name(_cell(row, i_name))
            price = parse_money(_cell(row, i_price))
            rank = parse_rank(_cell(row, i_rank))
            if not name or price is None:
                continue
            if rank is None:
                warnings.append(f"rank missing for {name}; derived from price order")
            financial.append(
                {
                    "seller_name_raw": name,
                    "total_price": price,
                    "rank": rank,
                    "currency": "INR",
                }
            )
        # Derive ranks only when the page did not publish them, and say so.
        if financial and any(f["rank"] is None for f in financial):
            for i, f in enumerate(sorted(financial, key=lambda x: x["total_price"]), start=1):
                if f["rank"] is None:
                    f["rank"] = i
        financial.sort(key=lambda f: f["rank"])

    if not financial:
        warnings.append("no financial evaluation published yet")
    if not bidders:
        warnings.append("no technical evaluation published yet")

    return {
        "tender": tender,
        "bidders": bidders,
        "technical_results": technical,
        "financial_bids": financial,
        "warnings": warnings,
        "result_url": url,
    }


LISTING_JSON_HINTS = ("bid_number", "bidNumber", "bid_id")


def parse_listing(html: str | bytes, url: str = "") -> list[dict]:
    """Parse a GeM bid-listing page.

    Two shapes are handled: an embedded JSON payload (the portal's own data
    blob), and a plain HTML table/card listing. Both are common across the
    portal's views; the connector therefore degrades instead of breaking.
    """
    soup = BeautifulSoup(html, "lxml")
    records: list[dict] = []

    # Shape 1: embedded JSON array of bid objects.
    for script in soup.find_all("script"):
        body = script.string or script.get_text() or ""
        if not body or "bid_number" not in body and "bidNumber" not in body:
            continue
        start, end = body.find("["), body.rfind("]")
        if start == -1 or end <= start:
            continue
        import json

        try:
            payload = json.loads(body[start : end + 1])
        except json.JSONDecodeError:
            continue
        for item in payload if isinstance(payload, list) else []:
            if not isinstance(item, dict):
                continue
            number = item.get("bid_number") or item.get("bidNumber") or item.get("bid_id")
            if not number:
                continue
            records.append(
                {
                    "portal_tender_id": clean(str(number)).replace(" ", ""),
                    "title": clean(item.get("category") or item.get("title") or ""),
                    "category_name": clean(item.get("category") or ""),
                    "quantity": item.get("quantity"),
                    "ministry": clean(item.get("ministry") or "") or None,
                    "department": clean(item.get("department") or "") or None,
                    "published_at": parse_date(item.get("start_date") or item.get("startDate")),
                    "closing_at": parse_date(item.get("end_date") or item.get("endDate")),
                    "document_url": item.get("document_url") or item.get("documentUrl"),
                    "portal_url": url,
                    "bid_status": item.get("status"),
                }
            )

    # Shape 2: HTML table listing with recognisable headers.
    if not records:
        for table in _tables(soup):
            joined = " | ".join(table["headers"])
            if "bid number" not in joined and "bid no" not in joined:
                continue
            i_num = _col(table, "bid number", "bid no")
            i_cat = _col(table, "category", "items")
            i_qty = _col(table, "quantity")
            i_start = _col(table, "start date", "published")
            i_end = _col(table, "end date", "closing")
            i_dept = _col(table, "department")
            i_min = _col(table, "ministry")
            for row in table["rows"]:
                number = _cell(row, i_num)
                if not number:
                    continue
                m = BID_NUMBER_RE.search(number)
                if not m:
                    continue
                records.append(
                    {
                        "portal_tender_id": clean(m.group(0)).replace(" ", ""),
                        "title": _cell(row, i_cat) or f"GeM bid {number}",
                        "category_name": _cell(row, i_cat),
                        "quantity": parse_int(_cell(row, i_qty)),
                        "published_at": parse_date(_cell(row, i_start)),
                        "closing_at": parse_date(_cell(row, i_end)),
                        "department": _cell(row, i_dept),
                        "ministry": _cell(row, i_min),
                        "portal_url": url,
                    }
                )

    return records
