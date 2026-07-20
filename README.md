# Payment Reconciliation Service

An async FastAPI service that ingests payment lifecycle events from an external source system, maintains a current-state view of each transaction, and exposes reconciliation endpoints to detect inconsistencies between payment processing and settlement.

**Live deployment:** https://payment-service-setu.onrender.com/ (interactive docs at `/docs`)

## 1. Overview

The service has three responsibilities:

- **Event ingestion** (`POST /events`): accepts individual payment lifecycle events (`payment_initiated`, `payment_processed`, `payment_failed`, `settled`) and applies them to a per-transaction state machine.
- **Transaction tracking** (`GET /transactions`, `GET /transactions/{id}`): exposes the current state of every transaction, plus its full event history, with filtering, sorting, and cursor pagination.
- **Reconciliation** (`GET /reconciliation/summary`, `GET /reconciliation/discrepancies`): aggregates transaction volume by merchant/date/status, and surfaces transactions whose payment and settlement state disagree or have stalled past an SLA.

Every event is durably stored, whether or not it changes transaction state, so the full event history is always reconstructable and auditable.

## 2. Architecture

Request flow is a straight, one-directional pipeline:

```
Router (app/routers/*)
  -> Service (app/services/*)
      -> Repository (app/repositories/*)
          -> SQLAlchemy models (app/models.py) -> Postgres
```

- **Routers** own HTTP concerns only: path/query parameter parsing and validation (via Pydantic/FastAPI `Query`), status code selection, and translating `None`/absence into `404`/`400`. They contain no business logic.
- **Services** own business logic: the event state machine (`app/services/events.py`), response assembly and pagination bookkeeping (`app/services/transactions.py`), and reconciliation formatting (`app/services/reconciliation.py`). Services call repositories; they never issue SQL directly.
- **Repositories** own all SQL. Each function is a single, purpose-built query or statement (e.g. `upsert_transaction`, `list_transactions`, `get_discrepancies`) built with SQLAlchemy Core/ORM. This is where indexes, `ON CONFLICT` clauses, and query shape decisions live.
- **Models** (`app/models.py`) define the schema as SQLAlchemy declarative classes, shared by the application and by Alembic autogenerate.

There is no separate "domain object" layer between services and the ORM — services operate on SQLAlchemy model instances and plain dicts, and Pydantic schemas (`app/schemas.py`) are used strictly at the API boundary for request/response shaping.

## 3. Technology Stack

| Concern | Choice |
|---|---|
| Language | Python 3.13.5 |
| Web framework | FastAPI 0.139.2 (uvicorn ASGI server, standard extras) |
| Database | PostgreSQL 16 |
| ORM | SQLAlchemy 2.0 (async, asyncpg driver) |
| Migrations | Alembic |
| Validation | Pydantic v2 / pydantic-settings |
| Local database | Docker Compose (Postgres 16 container) |
| Testing | pytest, pytest-asyncio, httpx (ASGI transport), against a real Postgres test database |

## 4. Project Structure

```
app/
  main.py            # FastAPI app instantiation, router registration
  models.py           # SQLAlchemy models: Merchant, Transaction, PaymentEvent
  schemas.py           # Pydantic request/response models (API boundary only)
  core/
    config.py          # Settings (DATABASE_URL) via pydantic-settings
    db.py               # Async engine/session factory, naming convention, Base
    pagination.py        # Opaque cursor encode/decode for keyset pagination
  routers/             # HTTP layer: one router per resource
  services/             # Business logic: state machine, response assembly
  repositories/          # All SQL: queries, upserts, conflict handling
alembic/               # Schema migrations (source of truth for DB DDL)
data/sample_events.json # 10k+ synthetic events for local seeding
scripts/load_sample_events.py # Idempotent bulk loader, replays the same state machine as the service
tests/
  unit/                # Pure-function tests (state machine) — no DB
  integration/          # Full HTTP-to-Postgres tests, grouped by resource
docker/
  docker-compose.yml     # Local Postgres container
Dockerfile               # Builds the FastAPI app image (not wired into docker-compose.yml)
```

The `routers` / `services` / `repositories` split exists to keep SQL, business rules, and HTTP concerns independently testable and independently replaceable — e.g. the reconciliation SQL can change without touching the router, and the state machine (`tests/unit/events/test_state_machine.py`) is tested without a database at all.

## 5. Database Design

Three tables, defined in `app/models.py` and `alembic/versions/d74559a56dee_initial_schema.py`:

**`merchants`** — one row per source-system merchant.
- `id` (UUID PK, server + client-generated).
- `merchant_code` (unique, indexed) — the source system's slug identifier (e.g. `"merchant_2"`), decoupled from the internal PK.
- `merchant_name`.

**`transactions`** — one row per `transaction_id`, the **current-state projection** of that transaction's event stream. Not hand-maintained separately from the event log — it is fully reconstructable by replaying `payment_events` in order (this is exactly what `scripts/load_sample_events.py` does).
- `id` (UUID PK) — the `transaction_id` from the source payload, used directly as PK (no surrogate key or lookup indirection).
- `merchant_id` (FK to `merchants`, `ON DELETE RESTRICT`).
- `amount`, `currency`.
- `payment_status` (`INITIATED` / `PROCESSED` / `FAILED`) and `settlement_status` (`UNSETTLED` / `SETTLED`) — both native Postgres ENUMs, so invalid values are rejected at the database layer, not just by Pydantic.
- `first_event_at`, `last_event_at`, `last_event_type`, `settled_at` — business timestamps from event payloads (distinct from `created_at`/`updated_at`, which are row bookkeeping timestamps).
- `is_discrepant`, `discrepancy_reason` — maintained by the service layer on every applied event, so `GET /reconciliation/discrepancies` is a plain indexed read rather than a query-time recomputation.

**`payment_events`** — append-only ledger; the system of record. Rows are inserted, never updated or deleted.
- `id` (BigInteger `IDENTITY` PK) — insertion-ordered surrogate key, never referenced externally.
- `event_id` (UUID, `UNIQUE`) — the idempotency key from the source system.
- `event_type` (ENUM, shared type with `transactions.last_event_type`).
- `transaction_id` (FK to `transactions`), `merchant_id` (FK to `merchants`, denormalized from the transaction for merchant-scoped queries without a join).
- `amount`, `currency`, `event_timestamp` (business time), `received_at` (ingestion time, operational use only).
- `is_applied` — false when the event was stored but did not change transaction state (duplicate, out-of-order, or conflicting transition).

**Relationships**: `Merchant 1:N Transaction`, `Merchant 1:N PaymentEvent`, `Transaction 1:N PaymentEvent`. Both FKs from `payment_events` use `ON DELETE RESTRICT` — the ledger can never be silently orphaned by a parent delete.

**Indexes** (all in the initial migration, plus one follow-up migration):
- `ix_transactions_merchant_status` and `ix_transactions_merchant_first_event_at` — the two common `GET /transactions` filter shapes (by merchant, and by merchant + date range).
- `ix_transactions_created_at_id` — composite `(created_at, id)` index supporting keyset pagination's row-wise `(sort_column, id) < cursor` comparison as an index seek rather than a scan.
- `ix_transactions_processed_unsettled` and `ix_transactions_failed_settled` — **partial** indexes matching the exact WHERE clauses used by discrepancy detection, so they stay small regardless of total table size.
- `ix_transactions_discrepant` — partial index on `is_discrepant = true`, backing `GET /reconciliation/discrepancies` directly.
- `ix_payment_events_transaction_ts` and `ix_payment_events_merchant_ts` — composite indexes so a transaction's or merchant's event history can be fetched pre-sorted, without a separate sort step.

## 6. Event Processing

1. A `POST /events` request is validated against `EventCreate` (amount > 0 with max 2 decimal places, ISO-3 currency code, timezone-aware timestamp).
2. `ingest_event` (`app/services/events.py`) takes a Postgres advisory transaction lock keyed on `transaction_id`, so concurrent events for the same transaction are serialized even before any row exists to lock.
3. The merchant is upserted by `merchant_code` (`get_or_create_merchant_id`).
4. The transaction's prior state (if any) is loaded, and `_apply_event` — a pure function, no I/O — computes the new state and whether the event is applied:
   - First event for a transaction: creates the row as `INITIATED`, unless the first event isn't `payment_initiated` (flagged `initiated_missing`).
   - `payment_initiated` after the transaction already exists: not applied (already initiated).
   - `payment_processed` / `payment_failed`: applied only if the transaction is still `INITIATED`; the same terminal status arriving again is not applied; the *other* terminal status arriving after one is already set is flagged `conflicting_transitions` and not applied.
   - `settled`: applied unless already settled. If it arrives while the transaction is `FAILED` or still `INITIATED`, the settlement is still applied but flagged (`settled_after_failure` / `settled_before_processed`).
   - Any event older than the transaction's `last_event_at` is treated as a late/out-of-order arrival and not applied — history is never rewritten backwards.
5. The `transactions` row is upserted (insert-or-update in one statement) with the computed state.
6. The raw event is always inserted into `payment_events` — including events that didn't change state — with `is_applied` recording the outcome of step 4.
7. Both writes commit together; the response reports the transaction's resulting state.

Because `payment_events` is append-only and every event is stored regardless of outcome, the full history behind any transaction's current state is always available via `GET /transactions/{id}`, and the same state machine can be replayed from scratch against the raw event log (as `scripts/load_sample_events.py` does for bulk loading).

## 7. Idempotency Strategy

- **Detection**: `payment_events.event_id` has a `UNIQUE` constraint. Ingestion uses `INSERT ... ON CONFLICT (event_id) DO NOTHING ... RETURNING id`; a submission is classified as `"duplicate"` (`HTTP 200`) if zero rows come back, `"accepted"` (`HTTP 201`) otherwise. This is a single round trip with no separate `SELECT`-then-check race window.
- **Constraint enforcement**: the uniqueness guarantee lives in the database, not application logic — even two requests racing past any application-level check would still be serialized correctly by the constraint (and further serialized per-`transaction_id` by the advisory lock in step 2 of ingestion).
- **Repeated submission behavior**: resubmitting the exact same `event_id` is a true no-op — no new row, no state change, same response shape (`status: "duplicate"`). A *different* `event_id` describing an already-reflected outcome (e.g. a second `settled` event with a new UUID) is stored as a new history row but marked `is_applied: false`, since it carries no new information for the state machine — this is a distinct, business-level notion of duplication from the DB-level `event_id` check, and both are reported separately in `EventIngestResponse`.

## 8. Reconciliation

**`GET /reconciliation/summary`** groups transactions by `merchant`, `date` (day-granularity, UTC, derived from `first_event_at`), or `status`, and returns one row per group with a single aggregation query (`COUNT`/`SUM` with `FILTER`, no Python-side aggregation): totals, and counts/amounts broken out by `payment_status` and `settlement_status`, plus a discrepant count/amount.

**`GET /reconciliation/discrepancies`** combines three independent detection mechanisms with `UNION ALL` (not `UNION` — a transaction matching two mechanisms is reported as two distinct findings, not deduplicated):

| Reason | Detected when |
|---|---|
| `initiated_missing` | A transaction's first observed event was not `payment_initiated`. |
| `conflicting_transitions` | Both `payment_processed` and `payment_failed` were received for the same transaction. |
| `settled_after_failure` | A `settled` event arrived after the transaction was already `FAILED`. |
| `settled_before_processed` | A `settled` event arrived while the transaction was still `INITIATED`. |
| `processed_not_settled` | Status is `PROCESSED` + `UNSETTLED`, and `last_event_at` is older than `stale_after_hours` (default 24). |
| `stuck_initiated` | Status is still `INITIATED`, and `first_event_at` is older than `stale_after_hours`. |

The first four are computed and stored (`is_discrepant`/`discrepancy_reason`) at ingestion time by the state machine; the last two are computed at query time against the `stale_after_hours` threshold, since "staleness" is relative to the current time, not a fact about the event stream itself.

## 9. API Documentation

### `POST /events`

Ingests one payment lifecycle event.

**Request body:**
```json
{
  "event_id": "b768e3a7-9eb3-4603-b21c-a54cc95661bc",
  "event_type": "payment_initiated",
  "transaction_id": "2f86e94c-239c-4302-9874-75f28e3474ee",
  "merchant_id": "merchant_2",
  "merchant_name": "FreshBasket",
  "amount": 15248.29,
  "currency": "INR",
  "timestamp": "2026-01-08T12:11:58.085567+00:00"
}
```

**Response** (`201` new event / `200` exact resend):
```json
{
  "event_id": "b768e3a7-9eb3-4603-b21c-a54cc95661bc",
  "status": "accepted",
  "is_applied": true,
  "transaction_id": "2f86e94c-239c-4302-9874-75f28e3474ee",
  "payment_status": "INITIATED",
  "settlement_status": "UNSETTLED",
  "discrepancy_reason": null
}
```

### `GET /transactions`

Lists transactions with filtering, sorting, and cursor pagination.

**Query parameters:** `merchant_id` (merchant code), `payment_status`, `status` (alias for `payment_status`), `settlement_status`, `is_discrepant`, `from_date` / `to_date` (bound `first_event_at`), `sort_by` (`created_at` \| `first_event_at` \| `amount`, default `created_at`), `sort_dir` (`asc` \| `desc`, default `desc`), `cursor` (opaque, from a previous response), `limit` (1–100, default 20).

**Response:**
```json
{
  "items": [
    {
      "id": "2f86e94c-239c-4302-9874-75f28e3474ee",
      "merchant_id": "merchant_2",
      "amount": 15248.29,
      "currency": "INR",
      "payment_status": "FAILED",
      "settlement_status": "UNSETTLED",
      "first_event_at": "2026-01-08T12:11:58.085567+00:00",
      "last_event_at": "2026-01-08T12:38:58.085567+00:00",
      "last_event_type": "payment_failed",
      "settled_at": null,
      "is_discrepant": false,
      "discrepancy_reason": null
    }
  ],
  "next_cursor": "MjAyNi0wMS0wOFQxMjozODo1OC4wODU1NjcrMDA6MDB8MmY4NmU5NGMtMjM5Yy00MzAyLTk4NzQtNzVmMjhlMzQ3NGVl",
  "limit": 20
}
```

### `GET /transactions/{transaction_id}`

Returns full detail for one transaction, including its merchant and complete ordered event history (both applied and non-applied events).

**Response:**
```json
{
  "transaction": {
    "id": "2f86e94c-239c-4302-9874-75f28e3474ee",
    "amount": 15248.29,
    "currency": "INR",
    "payment_status": "FAILED",
    "settlement_status": "UNSETTLED",
    "first_event_at": "2026-01-08T12:11:58.085567+00:00",
    "last_event_at": "2026-01-08T12:38:58.085567+00:00",
    "last_event_type": "payment_failed",
    "settled_at": null,
    "is_discrepant": false,
    "discrepancy_reason": null
  },
  "merchant": { "merchant_id": "merchant_2", "merchant_name": "FreshBasket" },
  "events": [
    { "event_id": "b768e3a7-...", "event_type": "payment_initiated", "amount": 15248.29, "currency": "INR", "event_timestamp": "2026-01-08T12:11:58.085567+00:00", "is_applied": true },
    { "event_id": "da46895f-...", "event_type": "payment_failed", "amount": 15248.29, "currency": "INR", "event_timestamp": "2026-01-08T12:38:58.085567+00:00", "is_applied": true }
  ]
}
```

`404` if the transaction does not exist.

### `GET /reconciliation/summary`

**Query parameters:** `group_by` (required: `merchant` \| `date` \| `status`), `merchant_id`, `from_date` / `to_date`.

**Response** (one row per group):
```json
[
  {
    "group_key": "PROCESSED",
    "total_transactions": 412,
    "total_amount": 5218904.10,
    "initiated_count": 0, "initiated_amount": 0,
    "processed_count": 412, "processed_amount": 5218904.10,
    "failed_count": 0, "failed_amount": 0,
    "settled_count": 390, "settled_amount": 4950123.55,
    "unsettled_count": 22, "unsettled_amount": 268780.55,
    "discrepant_count": 3, "discrepant_amount": 45211.20
  }
]
```

### `GET /reconciliation/discrepancies`

**Query parameters:** `merchant_id`, `stale_after_hours` (default 24, min 1), `limit` (1–500, default 100).

**Response:**
```json
[
  {
    "transaction_id": "2f86e94c-239c-4302-9874-75f28e3474ee",
    "merchant_id": "merchant_2",
    "discrepancy_reason": "settled_after_failure",
    "payment_status": "FAILED",
    "settlement_status": "SETTLED",
    "amount": 15248.29,
    "currency": "INR",
    "first_event_at": "2026-01-08T12:11:58.085567+00:00",
    "last_event_at": "2026-01-08T14:02:11.000000+00:00"
  }
]
```

## 10. Local Setup

```bash
git clone <repository-url>
cd payment-service

cp .env.example .env
# Edit .env to match docker-compose's credentials (.env.example ships placeholder
# user/password values, not the docker-compose default):
# DATABASE_URL=postgresql+asyncpg://postgres:postgres@localhost:5432/payments

python3.13 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt   # includes requirements.txt + test deps

docker compose -f docker/docker-compose.yml up -d   # starts Postgres 16 on :5432

alembic upgrade head

python scripts/load_sample_events.py   # loads data/sample_events.json (10k+ events)

uvicorn app.main:app --reload
```

The API is then available at `http://localhost:8000`.

## 11. Docker Setup

`docker/docker-compose.yml` provisions the **database only** — a single `postgres:16` service (`payment-db`), exposing `5432`, with a named volume (`postgres_data`) for persistence and credentials `postgres` / `postgres`, database `payments` (note: `.env.example` ships different placeholder credentials — see Section 10).

```bash
docker compose -f docker/docker-compose.yml up -d
```

A root-level `Dockerfile` builds the FastAPI app itself (`python:3.13-slim`, installs `requirements.txt`, runs `uvicorn app.main:app` on port `8000`):

```bash
docker build -t payment-service .
```

This image is not wired into `docker-compose.yml` (which provisions only Postgres), and no deployment/orchestration configuration (Render, ECS, Kubernetes, etc.) exists in this repository — see Section 13.

## 12. Testing

- **Framework:** pytest with `pytest-asyncio` (`asyncio_mode = "auto"`, session-scoped event loop) and `httpx.AsyncClient` against the FastAPI app via `ASGITransport` — no mocking of the database.
- **Test database:** a separate real Postgres database (`payments_test`), created on demand, with schema built directly from `app.models` (`Base.metadata.create_all`) rather than via Alembic, and truncated between tests (not transaction rollback, since the code under test commits itself).
- **Unit tests** (`tests/unit/`): the event state machine (`_apply_event`) in isolation — first-event handling, valid transitions, invalid/conflicting transitions — with no database involved.
- **Integration tests** (`tests/integration/`), organized by resource:
  - `events/`: request validation (amount, currency, timestamp timezone, malformed payloads), duplicate `event_id` handling, concurrent submissions (including two concurrent first-events for the same new transaction), and the full set of transition/discrepancy responses.
  - `transactions/`: cursor pagination correctness (full coverage, no overlap, tie-breaking), all filter/sort combinations, limit boundaries.
  - `reconciliation/`: each discrepancy detection branch individually, `UNION ALL` dual-appearance behavior, summary aggregation correctness per grouping dimension, merchant/date-range filtering.

Run with:
```bash
pytest
```

## 13. Assumptions and Tradeoffs

- **No rate limiting or request-size limits** on `POST /events`.
- **Cursor pagination has no total count.** `GET /transactions` deliberately omits a total row count to avoid a separate, expensive `COUNT(*)` query on every page — a standard keyset-pagination tradeoff, but it means UIs can't render "page N of M."
- **Cursor tokens are unsigned.** They are base64-encoded, not HMAC-signed. A forged cursor can only reposition within the same filtered/authorized result set (nothing a filter wouldn't otherwise return), so this was accepted as-is; signing would be the hardening step if that stopped being true (e.g. once per-caller authorization is introduced).
- **`merchant_id` denormalization on `payment_events`** is kept consistent only by application logic (always written from the same event payload as `transaction_id`), not by a database constraint. Acceptable for a single ingestion code path; would need revisiting if a second write path were added.
- **No outbox/webhook mechanism.** Reconciliation results are pull-only (`GET` endpoints); there's no push notification when a transaction becomes discrepant.
- **Staleness thresholds (`stale_after_hours`) are query-time parameters, not configurable per merchant** — a single global default (24h) applies unless overridden per request.
- **No deployment configuration is tracked in this repository.** The service is live on Render at https://payment-service-setu.onrender.com/, but the build/start commands and environment variables for that deployment are configured in the Render dashboard, not committed here as infrastructure-as-code.
- **`amount`/`currency` are stored per-event and per-transaction independently**, with no cross-currency handling — reconciliation aggregates assume a single currency is being summed per group; mixed-currency merchants would need explicit currency-aware aggregation.
- **No Postman collection.** The `postman/` directory has been removed; `/docs` (FastAPI's generated OpenAPI UI) is the practical option for manual API exploration.
