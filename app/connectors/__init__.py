"""Connector registry. Add a source by registering its class here and adding a
row to app/data/sources.yaml — no changes anywhere else."""
from __future__ import annotations

from sqlalchemy.orm import Session

from ..models import Source
from .base import Connector, ConnectorError, SourceBlocked, SourceEmpty, SourceRestructured
from .gem_listing import GemListingConnector
from .gem_results import GemResultsConnector

CONNECTORS: dict[str, type[Connector]] = {
    "gem_listing": GemListingConnector,
    "gem_results": GemResultsConnector,
}


def build_connector(db: Session, source: Source, **kwargs) -> Connector:
    cls = CONNECTORS.get(source.connector or "")
    if cls is None:
        raise ConnectorError(
            f"no connector registered for source '{source.key}' "
            f"(connector='{source.connector}'). Phase 5 sources are stubs."
        )
    return cls(db, source, **kwargs)


__all__ = [
    "CONNECTORS",
    "build_connector",
    "Connector",
    "ConnectorError",
    "SourceBlocked",
    "SourceEmpty",
    "SourceRestructured",
]
