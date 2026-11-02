# Procurement Intelligence Platform

An India-wide procurement intelligence system: it collects tender notices and **published bid results**
from government procurement portals, structures them into a searchable database, and answers questions
about historical prices, L1/L2/L3 bid ladders, suppliers and buyers — with every figure traceable to the
record it came from.

Built for InfiSource Corporation as an internal tool first, and architected for multi-tenant SaaS later.

> **The design principle everything else follows from:** a procurement intelligence product is bought on
> one thing — that when it says a chair was awarded at a particular price to a particular supplier, that
> is what happened. So the platform refuses to show a number it cannot source, refuses to answer a
> question its corpus cannot support, and reports its own coverage instead of hiding gaps.

---

## Status — what actually works

This repository implements **Phases 0–4** of the blueprint (see `docs/`), which is a working internal
v1. Later phases are scaffolded, not finished, and are labelled as such everywhere.

| Phase | Scope | State |
|---|---|---|
| 0 | Source registry, compliance gate, immutable archive, rate governor, run reporting | **Done** |
| 1 | GeM listing + bid-result connectors, parsers, self-tests | **Done** (parsers need a captured live page before production use) |
| 2 | Core schema (36 tables), extraction pipeline, dedupe, taxonomy, search UI | **Done** |
| 3 | Price intelligence: normalisation, comparable sets, statistics with sample-size floors, bid recommender | **Done** |
| 4 | Research assistant with an evidence contract and numeric verifier | **Done** |
| 4b | Operator settings UI — paste an API key, validated and encrypted, effective immediately | **Done** |
| 5 | State portals (GePNIC template), CPPP, IREPS | **Not built** — registry rows exist, disabled |
| 6 | Supplier/buyer intelligence (entity resolution beyond exact matching) | **Partial** — profiles work; review queue not built |
| 7 | Opportunity discovery, recurrence scoring | **Not built** |
| 8 | Alerts, scheduled reports | **Schema only** |
| 9 | Multi-tenancy, RBAC enforcement, billing, public API keys | **Schema + API shape only** |
| 10 | Market price benchmarking | **Not built** |

Two things to be clear about before you rely on this:

1. **The GeM parsers are written against the page's semantic structure, not a captured live page.** They
   are tested against fixtures in `app/connectors/fixtures/`. Before pointing the connectors at the live
   portal, capture one real result page and run `python -m scripts.run_connector --source gem_results
   --self-test` against it. If a selector is wrong, the self-test fails loudly rather than silently
   producing empty records.
2. **The demo corpus is synthetic.** `scripts/seed_demo.py` generates plausible-but-invented records,
   attached to a source called "Demo corpus (synthetic)" and marked `extraction_method='demo_seed'`. It
   exists so the app runs on a fresh clone. Never present those numbers as findings.

---

## Quickstart

```bash
git clone <this repo> && cd procintel
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env          # optional; everything has a default

make seed                     # schema + source registry + 60 synthetic tenders
make fixture                  # ingest a GeM result fixture through the real connector
make run                      # http://localhost:8000
```

Then open **`/settings`** and paste an OpenRouter key. That is the whole setup: the key is
validated against the provider, encrypted at rest, and used on the very next request — no
environment file, no restart, no redeploy. Everything else already works without it.

The screens:

- `/` — coverage overview (tenders, ladder coverage, price basis split)
- `/search` — tender search with state/status filters
- `/prices` — price intelligence, comparable sets, bid recommendation calculator
- `/assistant` — the research assistant
- `/sources` — source registry and why each source is or isn't runnable
- `/settings` — paste a key, pick models, test the connection
- `/docs` — OpenAPI

### With PostgreSQL

```bash
docker compose up --build
# app on :8000, PostgreSQL 16 on :5432
```

### Run the tests

```bash
make test        # 77 tests, no network required
```

The suite covers the things that would quietly ruin the product: normalisation arithmetic, citation
verification, parser regressions, the compliance gate, sample-size rules, and the assistant's refusal
behaviour.

---

## Connecting a model (the settings screen)

`/settings` is the only setup step. Paste a key, press **Save & connect**, done.

What happens behind that button, and why it is built this way:

| Step | Behaviour |
|---|---|
| **Validate** | The key is checked against the provider (`GET /key`). A rejected key is **never** saved, so a typo cannot silently become the configured key. |
| **Encrypt** | Stored with Fernet, using a key held outside the database (`APP_SECRET_KEY` or a `0600` `.secret.key` file). A database dump or backup does not expose it. |
| **Apply** | Read on the next request. No restart, no redeploy. |
| **Never return** | The API and the HTML only ever emit a masked form (`sk-or-v1-••••••••cdef`). Nothing logs the key. |
| **Test only** | Checks the key without saving it. |
| **Remove** | Clears the stored key and falls back to `OPENROUTER_API_KEY` if one is set. |
| **Save without verifying** | For a deployment that cannot reach the provider from the server. The key is stored, marked unverified, and used anyway. |

Two model slots are configured separately, because they do different jobs: a cheap model for
bulk PDF extraction, a stronger one for phrasing assistant answers. The picker is populated from
the provider's live catalogue (450+ models), and falls back to a curated list when the catalogue
cannot be fetched.

Without a key the platform is still fully usable — search, price intelligence and the assistant all
work, with deterministic rules for extraction and templated answers. The header chip shows
**AI connected** or **AI off** on every page.

## The five ideas that make this different from a tender aggregator

### 1. The compliance gate is data, not a policy document

`app/data/sources.yaml` carries a policy record per source: robots status, terms summary, permitted
paths, rate limit, licence, redistribution flag, review date. `app/registry.py` refuses to run a
connector whose record is missing, stale, disallowed by robots, or authentication-gated.

```bash
$ python -m scripts.run_connector --source maharashtra --run
refusing to run 'maharashtra': source is disabled in the registry; robots.txt disallows collection
$ echo $?
3
```

Editing that file is how you change what the platform is allowed to do — not editing a crawler.

### 2. Archive first, parse second

Every fetch is stored content-addressed before it is parsed. A better extraction model therefore improves
*your history* without re-crawling, and a portal redesign cannot destroy data you already had.

### 3. Provenance is a column

Every asserted fact carries `source_id`, `retrieved_at`, `extraction_method`, `confidence` and
`verification_state`. Every price additionally carries a `basis` — `awarded`, `l1_quote`, `l2_quote`,
`l3_quote`, `estimated` or `market` — and **bases are never blended in a statistic**. That single enum is
what makes it impossible for an estimated tender value to be presented as an awarded price.

### 4. Extraction discards what it cannot cite

The LLM extraction path requires a verbatim quote per field. A value whose quote is not found in the
source text is dropped, not stored with low confidence:

```python
>>> verify_citations({"quantity": {"value": 400, "quote": "supply of 400 chairs in three lots"}}, text).dropped
[{'field': 'quantity', 'value': 400, 'reason': 'quote not found verbatim in source'}]
```

The platform also runs with **no API key at all**: deterministic rules handle money, dates, GSTIN, HSN,
quantity and warranty, and the assistant answers from templates.

### 5. The assistant cannot invent a number

The model is only ever asked to *phrase* a fact set the database produced. Every figure in its output is
checked against that fact set; anything unsupported is treated as a hallucination and the deterministic
answer is served instead.

```python
>>> verify_numeric_claims("The median is ₹13,584 and the high was ₹9,99,999.", {13584.0})
[999999.0]      # → phrasing rejected, template used
```

When the corpus holds nothing, the assistant declines and says what it searched:

> "I hold no awarded-price records for 'Submarine Periscope Assembly', so I can't give you a price for it.
> I won't estimate one."

---

## Architecture

```
  Ingestion      source registry · scheduler · connectors · rate governor
                        ↓ (every artefact archived, content-addressed)
  Archive        HTML / PDF / XLS, immutable, hash-addressed
                        ↓
  Extraction     PDF text → OCR check → rules → LLM (cited) → overrides
                 → validation → dedupe → entity resolution
                        ↓
  Facts          PostgreSQL: 36 tables — tenders, line items, bidders,
                 technical results, financial bids, awards, price observations
                        ↓
  Intelligence   normalisation · comparable sets · statistics with n floors
                 · bid recommender · assistant with evidence verifier
                        ↓
  Surface        REST API (OpenAPI) · server-rendered UI · exports
```

Key directories:

```
app/
  models.py            36 tables, portable across SQLite and PostgreSQL
  registry.py          source registry + compliance gate
  entities.py          organisation / supplier / product resolution
  connectors/
    base.py            governor, archive, run report, self-test contract
    gem_parsers.py     pure parsers for GeM result and listing pages
    gem_results.py     the price-ladder connector
    gem_listing.py     discovery connector
    fixtures/          synthetic fixtures with a documented rationale
  extraction/
    pdf.py             text layer, density check, BOQ spreadsheet parser
    rules.py           deterministic high-precision field rules
    llm.py             OpenRouter client + citation verifier
    pipeline.py        stages 1–9, validation, logging
  intelligence/
    normalise.py       price normalisation with factor provenance
    matching.py        explainable product matching, tiered comparable sets
    aggregates.py      statistics, coverage discipline, bid recommender
    assistant.py       evidence contract, numeric verifier, abstention
  settings_store.py    encrypted operator settings (the API key)
  llm_config.py        effective LLM config, key validation, model catalogue
  routers/api.py       versioned REST API
  routers/settings.py  settings API (save / test / clear)
  routers/pages.py     server-rendered UI
scripts/               seed, ingest-fixture, run-connector, sync-registry
tests/                 77 tests, no network
docs/                  the implementation blueprint (PDF)
```

---

## API

```bash
# Search tenders
curl 'localhost:8000/api/v1/tenders?q=office+chair&state=Madhya+Pradesh&limit=5'

# A tender with its price ladder, bidders and provenance
curl localhost:8000/api/v1/tenders/1

# Price statistics — one basis at a time, never blended
curl 'localhost:8000/api/v1/products/1/prices?basis=awarded'

# Comparable set, tiered by how strict the match is
curl localhost:8000/api/v1/products/1/comparables

# Bid recommendation with every step shown
curl -X POST 'localhost:8000/api/v1/products/1/recommend?landed_cost=12000&target_margin_pct=0.15'

# Ask the assistant
curl -X POST localhost:8000/api/v1/assistant \
  -H 'Content-Type: application/json' \
  -d '{"question":"What were the L1, L2 and L3 prices for office chairs?"}'

# Operator: connector self-test and a gated run
curl -X POST localhost:8000/api/v1/connectors/gem_results/self-test
curl -X POST localhost:8000/api/v1/connectors/cppp_listing/run    # → 403, with reasons
```

---

## Configuration

Everything is environment-driven; see `.env.example`.

| Variable | Default | Notes |
|---|---|---|
| `DATABASE_URL` | `sqlite:///./procintel.db` | Use `postgresql+psycopg://…` in production |
| `ARCHIVE_DIR` | `./archive` | Content-addressed artefact store |
| `OPENROUTER_API_KEY` | *empty* | Optional, and **not needed** — the key is normally set in `/settings`. Setting it here seeds a default that the UI can override or clear |
| `APP_SECRET_KEY` | *generated* | Fernet key for encrypting UI-stored secrets. Set it in production; otherwise one is generated into `.secret.key` (gitignored) |
| `EXTRACTION_MODEL` | `google/gemini-2.0-flash-001` | Cheap tier for bulk extraction |
| `ASSISTANT_MODEL` | `anthropic/claude-3.5-haiku` | Stronger tier for phrasing |
| `CRAWLER_USER_AGENT` | identified bot + contact | Never use a browser UA to disguise the crawler |
| `CRAWLER_MAX_RPM` | `20` | Per-domain ceiling |
| `POLICY_MAX_AGE_DAYS` | `365` | Older policy reviews block the source |
| `MIN_N_FOR_STATS` | `3` | Below this, show records and no statistic |
| `MIN_N_FOR_DISTRIBUTION` | `8` | Below this, no percentiles and no trend |

**Never commit an API key.** The supported path is to paste it into `/settings`, where it is encrypted at
rest and never returned. If you set it in the environment instead, use `.env` (gitignored) or a secret store.
Either way it never reaches a log.

---

## Legal and ethical position (read this before enabling a source)

- **Collect only what a member of the public can reach without credentials.** No captcha solving, no
  account creation, no digital-signature-certificate access.
- **robots.txt is a hard stop.** A disallowed source stays in the registry marked `disallowed` and never
  runs. Maharashtra's portal is in the registry for exactly this reason.
- **Authenticated sources are never crawled** with credentials. Licensed feeds are used under contract.
- **Individual officer data is restricted.** Tender pages publish the buyer's name, official email and
  phone. The database stores these classified, and the API never serialises individual-identifying
  fields — no search, filter or export by officer name. Buyer intelligence is built around the *office and
  the official channel*, which is what a supplier actually needs.
- **Documents are not re-served.** Tender PDFs are archived for extraction; customers get extracted facts
  plus a link to the origin, because the documents remain the issuing department's copyright.
- **Licensed data is flagged.** Records from licensed feeds carry `redistribution_allowed=false` and are
  filtered out of customer-facing surfaces until a written redistribution right exists.

None of this is legal advice. Have Indian counsel review the collection policy and customer terms before
commercialising.

---

## Roadmap

Next, in order (from the blueprint):

1. **Capture live pages** for GeM listing and results; replace the synthetic fixtures; widen the parser
   self-tests to a handful of real pages per lifecycle stage.
2. **GePNIC connector template** — one declarative definition per state portal rather than a project per
   state. Start with Tamil Nadu, Telangana, Karnataka, Gujarat.
3. **Entity-resolution review queue** — candidate merges surfaced for an analyst, with the evidence shown.
4. **Opportunity scoring** — recurrence and seasonality per buyer-category pair, with predicted windows
   labelled as predictions.
5. **Alerts and scheduled reports**, then multi-tenant enforcement and API keys.

## Contributing

Read `app/data/sources.yaml` and `app/registry.py` first: any new source is a registry row plus a
declarative connector, and the gate will tell you what is missing. Run `make test` before opening a PR —
the parser regression tests and the assistant's abstention tests are the ones that catch real damage.

## Licence

Proprietary, © InfiSource Corporation. Public procurement data collected by this platform remains subject
to the terms and copyright of the issuing departments; the licence here covers the software, not the data.
