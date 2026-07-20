"""Integration tests for concurrent POST /events requests -- the
pg_advisory_xact_lock in events_repo.lock_transaction exists specifically to
serialize racing writers for the same transaction_id, so these exercise it
with real concurrent requests rather than sequential ones.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select

from app.models import PaymentEvent, SettlementStatus, Transaction


def event_payload(**overrides) -> dict:
    payload = {
        "event_id": str(uuid.uuid4()),
        "event_type": "payment_initiated",
        "transaction_id": str(uuid.uuid4()),
        "merchant_id": "merchant_concurrency_test",
        "merchant_name": "Concurrency Test Merchant",
        "amount": "50.00",
        "currency": "INR",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    payload.update(overrides)
    return payload


async def test_concurrent_initiated_events_for_same_new_transaction_do_not_error(client):
    """N concurrent payment_initiated events, distinct event_ids, same
    brand-new transaction_id. The advisory lock should serialize them so
    exactly one sees prior=None (is_applied=True) and the rest see an
    already-initiated transaction (is_applied=False) -- regardless of which
    request happens to acquire the lock first.
    """
    txn_id = str(uuid.uuid4())
    n = 5
    payloads = [event_payload(transaction_id=txn_id) for _ in range(n)]

    responses = await asyncio.gather(*[client.post("/events", json=p) for p in payloads])

    assert all(r.status_code == 201 for r in responses)
    applied_flags = [r.json()["is_applied"] for r in responses]
    assert applied_flags.count(True) == 1
    assert applied_flags.count(False) == n - 1


async def test_concurrent_duplicate_event_id_posts_only_insert_one_row(client, db_session):
    """The same event_id fired concurrently (a true race, not a sequential
    resend) must still only ever produce one payment_events row -- the
    unique constraint is the actual enforcement mechanism, not app-level
    coordination.
    """
    payload = event_payload()
    n = 5

    responses = await asyncio.gather(*[client.post("/events", json=payload) for _ in range(n)])

    assert all(r.status_code in (200, 201) for r in responses)
    statuses = [r.json()["status"] for r in responses]
    assert statuses.count("accepted") == 1
    assert statuses.count("duplicate") == n - 1

    result = await db_session.execute(
        select(func.count()).select_from(PaymentEvent).where(PaymentEvent.event_id == uuid.UUID(payload["event_id"]))
    )
    assert result.scalar_one() == 1


async def test_concurrent_settlement_events_for_existing_transaction_leave_no_duplicate_rows(client, db_session):
    """The realistic race: several concurrent, distinct-event_id requests
    mutating an *already-existing* transaction, not just competing to create
    it. Settling is idempotent (settlement_status == SETTLED short-circuits
    to a no-op) so the end state is deterministic regardless of which
    request's advisory lock wins -- unlike racing two genuinely conflicting
    terminal transitions, whose outcome is order-dependent by design and
    isn't a "duplicate rows / inconsistent state" question.
    """
    txn_id = str(uuid.uuid4())
    t0 = datetime.now(timezone.utc)

    await client.post("/events", json=event_payload(transaction_id=txn_id, timestamp=t0.isoformat()))
    await client.post(
        "/events",
        json=event_payload(
            transaction_id=txn_id,
            event_type="payment_processed",
            timestamp=(t0 + timedelta(minutes=1)).isoformat(),
        ),
    )

    n = 5
    settle_ts = (t0 + timedelta(minutes=2)).isoformat()
    payloads = [
        event_payload(transaction_id=txn_id, event_type="settled", timestamp=settle_ts) for _ in range(n)
    ]

    responses = await asyncio.gather(*[client.post("/events", json=p) for p in payloads])

    assert all(r.status_code == 201 for r in responses)
    applied_flags = [r.json()["is_applied"] for r in responses]
    assert applied_flags.count(True) == 1
    assert applied_flags.count(False) == n - 1
    assert all(r.json()["settlement_status"] == "SETTLED" for r in responses)
    assert all(r.json()["discrepancy_reason"] is None for r in responses)

    # No duplicate transaction rows regardless of race outcome.
    txn_count = await db_session.execute(
        select(func.count()).select_from(Transaction).where(Transaction.id == uuid.UUID(txn_id))
    )
    assert txn_count.scalar_one() == 1

    # Final state is consistent: settled exactly once, not corrupted by the race.
    txn = (await db_session.execute(select(Transaction).where(Transaction.id == uuid.UUID(txn_id)))).scalar_one()
    assert txn.settlement_status == SettlementStatus.SETTLED
    assert txn.settled_at is not None
    assert txn.is_discrepant is False

    # All N distinct settlement events were stored (append-only history is intact).
    event_count = await db_session.execute(
        select(func.count()).select_from(PaymentEvent).where(
            PaymentEvent.transaction_id == uuid.UUID(txn_id), PaymentEvent.event_type == "settled"
        )
    )
    assert event_count.scalar_one() == n
