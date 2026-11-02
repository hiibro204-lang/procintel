# Documentation

## Implementation blueprint

The design document this repository implements — feasibility analysis of Indian
procurement data sources, the legal position, system architecture, the 36-table
schema, the phased roadmap, stack, cost model and the assistant's evidence
contract — is delivered separately as `Procurement-Intelligence-Blueprint.pdf`
(v1.0, 2 November 2026). Ask the project owner for a copy; it is deliberately not
committed here because it contains commercial planning material.

## How the code maps to the blueprint

| Blueprint section | Where it lives |
|---|---|
| §2 Feasibility / source tiers | `app/data/sources.yaml` (each row carries its tier and policy record) |
| §3 Legal position / compliance gate | `app/registry.py`, `app/data/sources.yaml` |
| §4 Architecture | `app/connectors/` (ingestion), `app/extraction/` (extraction), `app/intelligence/` (derived) |
| §5 Schema | `app/models.py` |
| §6 Roadmap | README "Status" table |
| §9 Assistant evidence contract | `app/intelligence/assistant.py`, `app/extraction/llm.py::verify_citations` |
| §10 Price methodology | `app/intelligence/normalise.py`, `app/intelligence/aggregates.py` |
| §11 Security / tenancy | `SECURITY.md` (note: tenant isolation is specified, not yet enforced) |

## Adding a source

1. Add a row to `app/data/sources.yaml` with a full policy record.
2. Write a parser as pure functions (see `app/connectors/gem_parsers.py`) plus a
   fixture in `app/connectors/fixtures/`.
3. Subclass `Connector` with `fetch`, `parse`, `persist` and a `self_test`.
4. Register the class in `app/connectors/__init__.py`.
5. `python -m scripts.sync_registry` then `--self-test`, then `--run`.

The compliance gate will refuse to run the connector until the policy record is
complete and current. That is the intended workflow, not an obstacle.
