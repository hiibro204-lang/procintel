"""The source registry and the compliance gate.

A connector may only run if its Source row carries a current policy record.
That single rule turns the legal decision from a paragraph in a wiki into a
data field that code can check — see blueprint Section 3.6.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from pathlib import Path

import yaml
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import settings
from .models import Source

REGISTRY_PATH = Path(__file__).resolve().parent / "data" / "sources.yaml"

RUNNABLE_ROBOTS = {"allowed", "partial"}


class ComplianceError(RuntimeError):
    """Raised when a source may not be collected from."""


@dataclass
class GateResult:
    ok: bool
    reasons: list[str]

    def __bool__(self) -> bool:  # convenience
        return self.ok


def load_registry(path: Path | None = None) -> list[dict]:
    path = path or REGISTRY_PATH
    with open(path, "r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    sources = data.get("sources") or []
    if not isinstance(sources, list):
        raise ValueError(f"{path}: 'sources' must be a list")
    return sources


def _coerce_date(value) -> dt.date | None:
    if value is None:
        return None
    if isinstance(value, dt.date):
        return value
    return dt.date.fromisoformat(str(value))


def sync_sources(db: Session, path: Path | None = None) -> dict[str, int]:
    """Upsert the registry file into the database. Idempotent."""
    created = updated = 0
    for row in load_registry(path):
        existing = db.scalar(select(Source).where(Source.key == row["key"]))
        payload = {
            "name": row["name"],
            "tier": row["tier"],
            "category": row["category"],
            "base_url": row["base_url"],
            "connector": row.get("connector"),
            "enabled": bool(row.get("enabled", False)),
            "auth_required": bool(row.get("auth_required", False)),
            "robots_status": row.get("robots_status", "unknown"),
            "terms_summary": row.get("terms_summary"),
            "permitted_paths": row.get("permitted_paths") or [],
            "rate_limit_per_min": int(row.get("rate_limit_per_min", 10)),
            "egress_region": row.get("egress_region", "IN"),
            "licence_id": row.get("licence_id"),
            "redistribution_allowed": bool(row.get("redistribution_allowed", True)),
            "policy_reviewed_at": _coerce_date(row.get("policy_reviewed_at")),
            "policy_reviewed_by": row.get("policy_reviewed_by"),
            "policy_note": row.get("policy_note"),
            "schedule_cron": row.get("schedule_cron"),
        }
        if existing:
            for k, v in payload.items():
                setattr(existing, k, v)
            updated += 1
        else:
            db.add(Source(key=row["key"], **payload))
            created += 1
    db.commit()
    return {"created": created, "updated": updated}


def get_source(db: Session, key: str) -> Source | None:
    return db.scalar(select(Source).where(Source.key == key))


def check(source: Source | None, today: dt.date | None = None) -> GateResult:
    """Evaluate the compliance gate for a source. Pure function, easy to test."""
    today = today or dt.date.today()
    reasons: list[str] = []
    if source is None:
        return GateResult(False, ["source not found in registry"])
    if not source.enabled:
        reasons.append("source is disabled in the registry")
    if source.robots_status == "disallowed":
        reasons.append("robots.txt disallows collection")
    elif source.robots_status not in RUNNABLE_ROBOTS:
        reasons.append(f"robots_status is '{source.robots_status}', not runnable")
    if source.auth_required:
        reasons.append("source requires authentication; we do not use credentials")
    if not source.connector:
        reasons.append("no connector configured")
    if source.policy_reviewed_at is None:
        reasons.append("no policy review on record")
    else:
        age = (today - source.policy_reviewed_at).days
        if age > settings.policy_max_age_days:
            reasons.append(f"policy review is {age} days old (limit {settings.policy_max_age_days})")
    return GateResult(not reasons, reasons)


def assert_runnable(source: Source | None) -> Source:
    result = check(source)
    if not result.ok:
        name = getattr(source, "key", "<unknown>")
        raise ComplianceError(f"refusing to run source '{name}': " + "; ".join(result.reasons))
    return source


def runnable_sources(db: Session) -> list[Source]:
    return [s for s in db.scalars(select(Source).order_by(Source.key)) if check(s).ok]
