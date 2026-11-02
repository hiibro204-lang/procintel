"""Run a connector against the live source, or self-test it.

    python -m scripts.run_connector --source gem_results --self-test
    python -m scripts.run_connector --source gem_listing --run
    python -m scripts.run_connector --source gem_results --bid GEM/2026/B/8069492

The compliance gate is checked before anything is fetched. A blocked source exits
non-zero with the reasons, which is what you want in a cron job.
"""
from __future__ import annotations

import argparse
import json
import sys

from sqlalchemy import select

from app.connectors import ConnectorError, build_connector
from app.db import init_db, session_scope
from app.intelligence.aggregates import recompute_aggregates
from app.models import Source
from app.registry import check, sync_sources


def main() -> int:
    parser = argparse.ArgumentParser(description="Run or self-test a connector")
    parser.add_argument("--source", required=True)
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--bid", action="append", default=[], help="specific bid number(s) to fetch")
    args = parser.parse_args()

    init_db()
    with session_scope() as db:
        sync_sources(db)
        source = db.scalar(select(Source).where(Source.key == args.source))
        if source is None:
            print(f"unknown source '{args.source}'", file=sys.stderr)
            return 2

        try:
            kwargs = {"bid_ids": args.bid} if args.bid else {}
            connector = build_connector(db, source, **kwargs)
        except ConnectorError as exc:
            print(str(exc), file=sys.stderr)
            return 2

        if args.self_test:
            result = connector.self_test()
            print(json.dumps(result, indent=2, default=str))
            return 0 if result.get("ok") else 1

        gate = check(source)
        if not gate.ok:
            print(f"refusing to run '{source.key}': " + "; ".join(gate.reasons), file=sys.stderr)
            return 3

        run = connector.run()
        print(json.dumps({
            "run_id": run.id, "status": run.status, "pages_fetched": run.pages_fetched,
            "records_parsed": run.records_parsed, "records_rejected": run.records_rejected,
            "docs_archived": run.docs_archived, "error_class": run.error_class,
            "error_detail": run.error_detail,
        }, indent=2))
        recompute_aggregates(db)
        return 0 if run.status in {"ok", "partial"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
