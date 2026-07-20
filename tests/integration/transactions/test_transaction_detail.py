"""Integration tests for GET /transactions/{transaction_id} -- previously
completely untested."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone


async def post_event(client, **overrides) -> dict:
    payload = {
        "event_id": str(uuid.uuid4()),
        "merchant_id": "merchant_detail_test",
        "merchant_name": "Detail Test Merchant",
        "amount": "60.00",
        "currency": "INR",
    }
    payload.update(overrides)
    resp = await client.post("/events", json=payload)
    assert resp.status_code in (200, 201), resp.text
    return resp.json()


async def test_get_transaction_returns_full_detail_after_ingestion(client):
    txn_id = str(uuid.uuid4())
    t0 = datetime.now(timezone.utc)

    await post_event(client, event_type="payment_initiated", transaction_id=txn_id, timestamp=t0.isoformat())
    await post_event(
        client,
        event_type="payment_processed",
        transaction_id=txn_id,
        timestamp=(t0 + timedelta(minutes=5)).isoformat(),
    )
    await post_event(
        client, event_type="settled", transaction_id=txn_id, timestamp=(t0 + timedelta(minutes=10)).isoformat()
    )

    resp = await client.get(f"/transactions/{txn_id}")
    assert resp.status_code == 200
    body = resp.json()

    assert body["transaction"]["id"] == txn_id
    assert body["transaction"]["payment_status"] == "PROCESSED"
    assert body["transaction"]["settlement_status"] == "SETTLED"
    assert body["transaction"]["settled_at"] is not None
    assert body["transaction"]["is_discrepant"] is False

    assert body["merchant"]["merchant_id"] == "merchant_detail_test"
    assert body["merchant"]["merchant_name"] == "Detail Test Merchant"

    event_types = [e["event_type"] for e in body["events"]]
    assert event_types == ["payment_initiated", "payment_processed", "settled"]
    assert all(e["is_applied"] for e in body["events"])


async def test_get_transaction_returns_404_for_unknown_id(client):
    resp = await client.get(f"/transactions/{uuid.uuid4()}")
    assert resp.status_code == 404


async def test_get_transaction_with_invalid_uuid_returns_422(client):
    resp = await client.get("/transactions/not-a-uuid")
    assert resp.status_code == 422


async def test_get_transaction_events_include_non_applied_events(client):
    """A rejected/conflicting event is still stored (history is append-only)
    and must still show up in the detail view's events list, just with
    is_applied=False -- it's not silently hidden from history.
    """
    txn_id = str(uuid.uuid4())
    t0 = datetime.now(timezone.utc)

    await post_event(client, event_type="payment_initiated", transaction_id=txn_id, timestamp=t0.isoformat())
    await post_event(
        client,
        event_type="payment_processed",
        transaction_id=txn_id,
        timestamp=(t0 + timedelta(minutes=5)).isoformat(),
    )
    await post_event(
        client,
        event_type="payment_failed",
        transaction_id=txn_id,
        timestamp=(t0 + timedelta(minutes=10)).isoformat(),
    )

    resp = await client.get(f"/transactions/{txn_id}")
    assert resp.status_code == 200
    events = resp.json()["events"]

    assert len(events) == 3
    applied_by_type = {e["event_type"]: e["is_applied"] for e in events}
    assert applied_by_type["payment_initiated"] is True
    assert applied_by_type["payment_processed"] is True
    assert applied_by_type["payment_failed"] is False  # conflicting_transitions, not applied
