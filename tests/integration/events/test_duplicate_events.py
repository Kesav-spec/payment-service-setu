"""Integration tests for POST /events duplicate detection, against a real
Postgres DB -- the unique constraint on payment_events.event_id is a DB-level
guarantee, not something a mock can meaningfully stand in for.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select

from app.models import PaymentEvent


def event_payload(**overrides) -> dict:
    payload = {
        "event_id": str(uuid.uuid4()),
        "event_type": "payment_initiated",
        "transaction_id": str(uuid.uuid4()),
        "merchant_id": "merchant_dup_test",
        "merchant_name": "Duplicate Test Merchant",
        "amount": "100.00",
        "currency": "INR",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    payload.update(overrides)
    return payload


async def test_new_event_is_accepted_with_201(client):
    resp = await client.post("/events", json=event_payload())
    assert resp.status_code == 201
    body = resp.json()
    assert body["status"] == "accepted"
    assert body["is_applied"] is True


async def test_exact_duplicate_event_id_returns_200_duplicate(client):
    payload = event_payload()

    first = await client.post("/events", json=payload)
    assert first.status_code == 201

    second = await client.post("/events", json=payload)
    assert second.status_code == 200
    body = second.json()
    assert body["status"] == "duplicate"
    assert body["is_applied"] is False
    # Replaying the exact same event doesn't change the transaction's state.
    assert body["payment_status"] == first.json()["payment_status"]
    assert body["settlement_status"] == first.json()["settlement_status"]


async def test_duplicate_does_not_insert_a_second_row(client, db_session):
    payload = event_payload()
    await client.post("/events", json=payload)
    await client.post("/events", json=payload)
    await client.post("/events", json=payload)  # third resend, still a no-op

    result = await db_session.execute(
        select(func.count()).select_from(PaymentEvent).where(PaymentEvent.event_id == uuid.UUID(payload["event_id"]))
    )
    assert result.scalar_one() == 1


async def test_distinct_event_id_second_settled_is_accepted_but_not_applied(client):
    """Two different event_ids, both semantically 'settled' for the same
    transaction -- not a DB-level duplicate (different event_id), but a
    business-level no-op: settlement_status was already SETTLED.
    """
    txn_id = str(uuid.uuid4())
    t0 = datetime.now(timezone.utc)

    await client.post("/events", json=event_payload(transaction_id=txn_id, event_type="payment_initiated", timestamp=t0.isoformat()))
    await client.post(
        "/events",
        json=event_payload(
            transaction_id=txn_id, event_type="payment_processed", timestamp=(t0 + timedelta(minutes=5)).isoformat()
        ),
    )
    first_settled = await client.post(
        "/events",
        json=event_payload(transaction_id=txn_id, event_type="settled", timestamp=(t0 + timedelta(minutes=10)).isoformat()),
    )
    assert first_settled.status_code == 201
    assert first_settled.json()["is_applied"] is True

    second_settled = await client.post(
        "/events",
        json=event_payload(transaction_id=txn_id, event_type="settled", timestamp=(t0 + timedelta(minutes=15)).isoformat()),
    )
    assert second_settled.status_code == 201  # genuinely new event_id, not a DB conflict
    assert second_settled.json()["status"] == "accepted"
    assert second_settled.json()["is_applied"] is False  # but changed nothing
