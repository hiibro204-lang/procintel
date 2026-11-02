"""Ingest a captured page from a local fixture, end to end.

This is the offline equivalent of a live run: the same connector, the same
archive, the same parsers and the same persistence path — only the HTTP fetch is
replaced by a file. Use it to demo the pipeline, to reproduce a parsing bug from a
captured page, or to load the fixture corpus in CI.

    python -m scripts.ingest_fixture gem_results
    python -m scripts.ingest_fixture gem_listing
"""
from __future__ import annotations

import argparse

from sqlalchemy import select

from app.connectors import build_connector
from app.connectors.base import load_fixture
from app.db import init_db, session_scope
from app.intelligence.aggregates import recompute_aggregates
from app.models import Source
from app.registry import sync_sources

FIXTURES = {
    "gem_results": ("gem_result_fixture.html", "https://bidplus.gem.gov.in/bidding/bid/getBidResultView/demo"),
    "gem_listing": ("gem_listing_fixture.html", "https://bidplus.gem.gov.in/all-bids-data"),
}


def main() -> None:
    parser = argparse.ArgumentParser(description="Ingest a local fixture through a real connector")
    parser.add_argument("source", choices=sorted(FIXTURES))
    args = parser.parse_args()

    init_db()
    fixture_name, url = FIXTURES[args.source]
    content = load_fixture(fixture_name)

    with session_scope() as db:
        sync_sources(db)
        source = db.scalar(select(Source).where(Source.connector == args.source))
        if source is None:
            raise SystemExit(f"no source registered for connector '{args.source}'")
        connector = build_connector(db, source)
        written = connector.ingest_bytes(url, content)
        recompute_aggregates(db)
        print(f"ingested {written} record(s) from {fixture_name} via {args.source}")


if __name__ == "__main__":
    main()
