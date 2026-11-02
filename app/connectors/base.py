"""Connector framework: fetch governor, immutable archive, run reporting.

Rules this module enforces so that individual connectors cannot get them wrong:
  * every fetch is rate-limited per domain and identified by user agent;
  * every artefact is archived content-addressed before it is parsed, so
    extraction can be re-run over history without re-crawling;
  * every run emits a report, and "the source returned nothing" is distinguished
    from "the connector broke" — conflating them is how coverage silently dies.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
from sqlalchemy.orm import Session

from ..config import settings
from ..models import Document, IngestionRun, Source

log = logging.getLogger(__name__)


class ConnectorError(RuntimeError):
    """Base class for connector failures."""


class SourceBlocked(ConnectorError):
    """The source refused us: 401/403, captcha wall, geo-block, NICNET-only."""


class SourceRestructured(ConnectorError):
    """The page loaded but the fields we depend on are gone — a redesign."""


class SourceEmpty(ConnectorError):
    """The source answered normally and legitimately had no records."""


# --------------------------------------------------------------------------- #
# Rate governor
# --------------------------------------------------------------------------- #
class FetchGovernor:
    """Per-domain token bucket. Deliberately conservative: a procurement portal
    should never see traffic from us that a human browsing session would not."""

    def __init__(self, requests_per_min: int, jitter: float = 0.25):
        self.rpm = max(1, requests_per_min)
        self.min_interval = 60.0 / self.rpm
        self.jitter = jitter
        self._last: dict[str, float] = {}

    def wait(self, url: str) -> None:
        domain = urlparse(url).netloc
        last = self._last.get(domain, 0.0)
        elapsed = time.monotonic() - last
        target = self.min_interval * (1.0 + self.jitter)
        if elapsed < target:
            time.sleep(target - elapsed)
        self._last[domain] = time.monotonic()


# --------------------------------------------------------------------------- #
# Immutable archive
# --------------------------------------------------------------------------- #
def archive_bytes(content: bytes, source_key: str, url: str, suffix: str = ".bin") -> tuple[str, str]:
    """Store an artefact content-addressed. Returns (object_key, sha256)."""
    digest = hashlib.sha256(content).hexdigest()
    day = dt.date.today().isoformat()
    rel = Path(source_key) / day / f"{digest[:16]}{suffix}"
    path = settings.archive_dir / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_bytes(content)
    return str(rel), digest


def archive_text(text: str, source_key: str, url: str, suffix: str = ".txt") -> tuple[str, str]:
    return archive_bytes(text.encode("utf-8"), source_key, url, suffix)


def read_archived(object_key: str) -> bytes:
    return (settings.archive_dir / object_key).read_bytes()


# --------------------------------------------------------------------------- #
# Parsing helpers shared by connectors
# --------------------------------------------------------------------------- #
_WS = re.compile(r"\s+")
_MONEY = re.compile(r"(?:rs\.?|inr|₹)?\s*([\d,]+(?:\.\d+)?)", re.I)


def clean(text: str | None) -> str:
    return _WS.sub(" ", (text or "")).strip()


def parse_money(text: str | None) -> float | None:
    """Parse Indian-format money. Handles 1,94,1200.00 and ₹ 1941200.00 alike."""
    if not text:
        return None
    if isinstance(text, (int, float)):
        return float(text)
    m = _MONEY.search(str(text).replace("\u20b9", " "))
    if not m:
        return None
    raw = m.group(1).replace(",", "")
    try:
        return float(raw)
    except ValueError:
        return None


_DATE_FORMATS = ("%d-%m-%Y %H:%M:%S", "%d-%m-%Y %H:%M", "%d-%m-%Y", "%d/%m/%Y", "%Y-%m-%d", "%d-%b-%Y")


def parse_date(text: str | None) -> dt.datetime | None:
    if not text:
        return None
    raw = clean(str(text))
    for fmt in _DATE_FORMATS:
        try:
            parsed = dt.datetime.strptime(raw, fmt)
            return parsed.replace(tzinfo=dt.timezone.utc)
        except ValueError:
            continue
    return None


def parse_int(text: str | None) -> int | None:
    if text is None:
        return None
    m = re.search(r"-?\d+", str(text).replace(",", ""))
    return int(m.group()) if m else None


def parse_rank(text: str | None) -> int | None:
    """'L1' -> 1. Ranks are the price ladder's ordering key."""
    if not text:
        return None
    m = re.search(r"L\s*(\d+)", str(text), re.I)
    return int(m.group(1)) if m else None


# --------------------------------------------------------------------------- #
# Run report
# --------------------------------------------------------------------------- #
@dataclass
class RunReport:
    source_key: str
    pages_fetched: int = 0
    records_parsed: int = 0
    records_rejected: int = 0
    docs_archived: int = 0
    duplicates_skipped: int = 0
    error_class: str | None = None
    error_detail: str | None = None
    metrics: dict[str, Any] = field(default_factory=dict)

    @property
    def status(self) -> str:
        if self.error_class and self.records_parsed == 0:
            return "failed"
        if self.error_class or self.records_rejected:
            return "partial"
        return "ok"


# --------------------------------------------------------------------------- #
# Base connector
# --------------------------------------------------------------------------- #
class Connector:
    """Subclasses implement fetch() and parse() and declare self-test assertions."""

    key: str = "base"

    def __init__(self, db: Session, source: Source, client: httpx.Client | None = None):
        self.db = db
        self.source = source
        self.governor = FetchGovernor(source.rate_limit_per_min or settings.crawler_max_requests_per_min)
        self.report = RunReport(source_key=source.key)
        self.client = client or httpx.Client(
            timeout=30.0,
            follow_redirects=True,
            headers={
                "User-Agent": settings.crawler_user_agent,
                "Accept-Language": "en-IN,en;q=0.9",
            },
        )

    # -- to implement ----------------------------------------------------- #
    def fetch(self) -> list[tuple[str, bytes]]:
        """Return a list of (url, content) artefacts to parse."""
        raise NotImplementedError

    def parse(self, url: str, content: bytes) -> list[dict]:
        """Turn one artefact into structured records."""
        raise NotImplementedError

    # -- provided --------------------------------------------------------- #
    def get(self, url: str, *, raise_on_block: bool = True) -> httpx.Response:
        self.governor.wait(url)
        self.report.pages_fetched += 1
        resp = self.client.get(url)
        if resp.status_code in (401, 403, 429):
            raise SourceBlocked(f"HTTP {resp.status_code} from {url}")
        if resp.status_code >= 500:
            raise SourceBlocked(f"HTTP {resp.status_code} from {url}")
        if resp.status_code >= 400 and raise_on_block:
            raise ConnectorError(f"HTTP {resp.status_code} from {url}")
        return resp

    def archive(self, url: str, content: bytes, suffix: str = ".html") -> Document:
        object_key, digest = archive_bytes(content, self.source.key, url, suffix)
        doc = (
            self.db.query(Document)
            .filter(Document.sha256 == digest, Document.source_url == url)
            .one_or_none()
        )
        if doc is None:
            doc = Document(
                source_id=self.source.id,
                source_url=url,
                object_key=object_key,
                sha256=digest,
                mime="text/html",
                bytes=len(content),
            )
            self.db.add(doc)
            self.report.docs_archived += 1
        return doc

    def run(self) -> IngestionRun:
        run = IngestionRun(source_id=self.source.id, status="running")
        self.db.add(run)
        self.db.flush()
        try:
            artefacts = self.fetch()
            for url, content in artefacts:
                try:
                    self.archive(url, content)
                    records = self.parse(url, content)
                except SourceBlocked:
                    raise
                except ConnectorError as exc:
                    log.warning("parse failed for %s: %s", url, exc)
                    self.report.records_rejected += 1
                    continue
                if not records:
                    self.report.records_rejected += 1
                for record in records:
                    try:
                        self.persist(record)
                        self.report.records_parsed += 1
                    except Exception as exc:  # noqa: BLE001 - a bad record must not kill the run
                        log.exception("persist failed: %s", exc)
                        self.report.records_rejected += 1
            if self.report.pages_fetched and not self.report.records_parsed and not self.report.records_rejected:
                raise SourceEmpty("source returned no records")
        except SourceBlocked as exc:
            self.report.error_class = "blocked"
            self.report.error_detail = str(exc)
        except SourceRestructured as exc:
            self.report.error_class = "restructured"
            self.report.error_detail = str(exc)
        except SourceEmpty as exc:
            self.report.error_class = "empty"
            self.report.error_detail = str(exc)
        except Exception as exc:  # noqa: BLE001
            self.report.error_class = "network"
            self.report.error_detail = f"{type(exc).__name__}: {exc}"
            log.exception("connector %s failed", self.source.key)
        finally:
            self.finalise(run)
        return run

    def persist(self, record: dict) -> None:
        """Persist one parsed record. Implemented per connector (writes facts)."""
        raise NotImplementedError

    def ingest_bytes(self, url: str, content: bytes) -> int:
        """Archive + parse + persist an artefact we already hold.

        Used by the fixture ingestion script and by the tests, so the exact code
        path exercised offline is the one that runs against a live source.
        """
        self.archive(url, content)
        records = self.parse(url, content)
        written = 0
        for record in records:
            self.persist(record)
            written += 1
        return written

    def finalise(self, run: IngestionRun) -> None:
        run.finished_at = dt.datetime.now(dt.timezone.utc)
        run.status = self.report.status
        run.pages_fetched = self.report.pages_fetched
        run.records_parsed = self.report.records_parsed
        run.records_rejected = self.report.records_rejected
        run.docs_archived = self.report.docs_archived
        run.duplicates_skipped = self.report.duplicates_skipped
        run.error_class = self.report.error_class
        run.error_detail = self.report.error_detail
        run.metrics = self.report.metrics
        self.source.last_attempt_at = run.started_at
        if self.report.status == "failed":
            self.source.consecutive_failures = (self.source.consecutive_failures or 0) + 1
            self.source.last_error = self.report.error_detail
        else:
            self.source.consecutive_failures = 0
            self.source.last_success_at = run.finished_at
            self.source.last_error = None
        self.db.add(run)
        self.db.commit()

    # -- self-test -------------------------------------------------------- #
    def self_test(self) -> dict:
        """Parse a known-good fixture and assert expected fields are present.

        A redesign that silently empties records is the failure mode this catches.
        """
        raise NotImplementedError


def load_fixture(name: str) -> bytes:
    path = Path(__file__).resolve().parent / "fixtures" / name
    return path.read_bytes()


def dump_json(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, default=str)
