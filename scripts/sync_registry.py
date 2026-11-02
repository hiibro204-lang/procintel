"""Sync app/data/sources.yaml into the database."""
from __future__ import annotations

from app.db import init_db, session_scope
from app.registry import runnable_sources, sync_sources


def main() -> None:
    init_db()
    with session_scope() as db:
        result = sync_sources(db)
        runnable = [s.key for s in runnable_sources(db)]
    print(f"registry synced: {result}")
    print(f"runnable sources: {', '.join(runnable) or 'none'}")


if __name__ == "__main__":
    main()
