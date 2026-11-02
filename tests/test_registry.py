"""The compliance gate is the platform's legal position expressed as data."""
from __future__ import annotations

import datetime as dt

from app.registry import check, load_registry, runnable_sources
from app.models import Source


def test_registry_file_is_wellformed():
    rows = load_registry()
    assert len(rows) >= 10
    keys = [r["key"] for r in rows]
    assert len(keys) == len(set(keys)), "source keys must be unique"
    for row in rows:
        assert row["policy_reviewed_at"], f"{row['key']} has no policy review date"
        assert row["robots_status"], f"{row['key']} has no robots status"


def test_gem_results_is_runnable(db, gem_source):
    result = check(gem_source)
    assert result.ok, result.reasons


def test_robots_disallowed_source_is_blocked(db):
    maharashtra = db.query(Source).filter(Source.key == "maharashtra").one()
    result = check(maharashtra)
    assert not result.ok
    assert any("robots" in r for r in result.reasons)


def test_captcha_blocked_source_is_blocked(db):
    cppp = db.query(Source).filter(Source.key == "cppp_listing").one()
    result = check(cppp)
    assert not result.ok
    assert any("blocked" in r for r in result.reasons)


def test_authenticated_source_is_never_runnable(db):
    bidassist = db.query(Source).filter(Source.key == "bidassist_api").one()
    result = check(bidassist)
    assert not result.ok
    assert any("authentication" in r for r in result.reasons)


def test_stale_policy_blocks_the_source(db, gem_source):
    fresh = check(gem_source)
    assert fresh.ok
    stale = check(gem_source, today=dt.date.today() + dt.timedelta(days=500))
    assert not stale.ok
    assert any("policy review is" in r for r in stale.reasons)


def test_runnable_set_is_small_and_deliberate(db):
    keys = {s.key for s in runnable_sources(db)}
    assert "gem_listing" in keys and "gem_results" in keys
    assert "maharashtra" not in keys
    assert "cppp_listing" not in keys
