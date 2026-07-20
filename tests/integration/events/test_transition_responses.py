"""Integration tests asserting the actual POST /events response body
(EventIngestResponse) for each discrepancy-producing state transition.

app.services.events._apply_event is already exhaustively unit-tested as a
pure function (tests/unit/events/test_state_machine.py). These tests cover
the same transitions end-to-end through the real HTTP + DB path, verifying
the response fields a caller actually sees -- status code, is_applied,
payment_status, settlement_status, discrepancy_reason -- which the unit
tests cannot, since they never touch the endpoint or persistence layer.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone


async def post_event(client, **overrides) -> dict:
    payload = {
        "event_id": str(uuid.uuid4()),
        "merchant_id": "merchant_transition_test",
        "merchant_name": "Transition Test Merchant",
        "amount": "40.00",
        "currency": "INR",
    }
    payload.update(overrides)
    resp = await client.post("/events", json=payload)
    assert resp.status_code in (200, 201), resp.text
    return resp.json()


async def test_non_initiated_first_event_flags_initiated_missing(client):
    txn_id = str(uuid.uuid4())
    t0 = datetime.now(timezone.utc)

    body = await post_event(
        client, event_type="payment_processed", transaction_id=txn_id, timestamp=t0.isoformat()
    )

    assert body["is_applied"] is True
    assert body["payment_status"] == "PROCESSED"
    assert body["discrepancy_reason"] == "initiated_missing"


async def test_processed_after_failed_is_conflicting_and_not_applied(client):
    txn_id = str(uuid.uuid4())
    t0 = datetime.now(timezone.utc)

    await post_event(client, event_type="payment_initiated", transaction_id=txn_id, timestamp=t0.isoformat())
    await post_event(
        client,
        event_type="payment_failed",
        transaction_id=txn_id,
        timestamp=(t0 + timedelta(minutes=5)).isoformat(),
    )
    body = await post_event(
        client,
        event_type="payment_processed",
        transaction_id=txn_id,
        timestamp=(t0 + timedelta(minutes=10)).isoformat(),
    )

    assert body["is_applied"] is False
    assert body["payment_status"] == "FAILED"  # unchanged
    assert body["discrepancy_reason"] == "conflicting_transitions"


async def test_failed_after_processed_is_conflicting_and_not_applied(client):
    txn_id = str(uuid.uuid4())
    t0 = datetime.now(timezone.utc)

    await post_event(client, event_type="payment_initiated", transaction_id=txn_id, timestamp=t0.isoformat())
    await post_event(
        client,
        event_type="payment_processed",
        transaction_id=txn_id,
        timestamp=(t0 + timedelta(minutes=5)).isoformat(),
    )
    body = await post_event(
        client,
        event_type="payment_failed",
        transaction_id=txn_id,
        timestamp=(t0 + timedelta(minutes=10)).isoformat(),
    )

    assert body["is_applied"] is False
    assert body["payment_status"] == "PROCESSED"  # unchanged
    assert body["discrepancy_reason"] == "conflicting_transitions"


async def test_settled_after_failure_flags_discrepancy(client):
    txn_id = str(uuid.uuid4())
    t0 = datetime.now(timezone.utc)

    await post_event(client, event_type="payment_initiated", transaction_id=txn_id, timestamp=t0.isoformat())
    await post_event(
        client,
        event_type="payment_failed",
        transaction_id=txn_id,
        timestamp=(t0 + timedelta(minutes=5)).isoformat(),
    )
    body = await post_event(
        client, event_type="settled", transaction_id=txn_id, timestamp=(t0 + timedelta(minutes=10)).isoformat()
    )

    assert body["is_applied"] is True  # settlement_status genuinely changes
    assert body["settlement_status"] == "SETTLED"
    assert body["discrepancy_reason"] == "settled_after_failure"


async def test_settled_before_processed_flags_discrepancy(client):
    txn_id = str(uuid.uuid4())
    t0 = datetime.now(timezone.utc)

    await post_event(client, event_type="payment_initiated", transaction_id=txn_id, timestamp=t0.isoformat())
    body = await post_event(
        client, event_type="settled", transaction_id=txn_id, timestamp=(t0 + timedelta(minutes=5)).isoformat()
    )

    assert body["is_applied"] is True
    assert body["settlement_status"] == "SETTLED"
    assert body["payment_status"] == "INITIATED"
    assert body["discrepancy_reason"] == "settled_before_processed"


async def test_late_out_of_order_event_is_not_applied(client):
    txn_id = str(uuid.uuid4())
    t0 = datetime.now(timezone.utc)

    await post_event(client, event_type="payment_initiated", transaction_id=txn_id, timestamp=t0.isoformat())
    await post_event(
        client,
        event_type="payment_processed",
        transaction_id=txn_id,
        timestamp=(t0 + timedelta(minutes=5)).isoformat(),
    )
    # Arrives after payment_processed but with an earlier business timestamp.
    body = await post_event(
        client,
        event_type="payment_initiated",
        transaction_id=txn_id,
        timestamp=(t0 + timedelta(minutes=1)).isoformat(),
    )

    assert body["is_applied"] is False
    assert body["payment_status"] == "PROCESSED"  # unchanged
    assert body["discrepancy_reason"] is None  # staleness alone isn't a data problem
