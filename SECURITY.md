# Security notes

## Reporting
Email security@infisource.example with a description and, if possible, a minimal
reproduction. Please do not open a public issue for a vulnerability.

## Credentials
- The LLM API key is read from the environment (`OPENROUTER_API_KEY`) and is never
  written to disk, logged, or returned by any endpoint.
- `.env` is gitignored. CI uses no secrets; the test suite runs with the key unset
  so the rule-based path is the one that gets tested.

## Data handling
- Individual-identifying fields (officer names) are stored with a `data_class` of
  `individual_identifying` and are never serialised by the API. There is no search,
  filter or export path that returns them.
- Tenant-owned rows carry `tenant_id`. Row-level isolation is specified in the
  blueprint (Phase 9) and is **not yet enforced** — do not run this as a
  multi-tenant service until that work is done.
- The archive is append-only and content-addressed; it is safe to back up wholesale.

## Before production
1. Set a real `DATABASE_URL` (PostgreSQL) and run behind TLS.
2. Enable authentication (Phase 9) — the current build is single-tenant internal use.
3. Capture live portal pages and replace the synthetic parser fixtures.
4. Have counsel review the collection policy in `app/data/sources.yaml`.
