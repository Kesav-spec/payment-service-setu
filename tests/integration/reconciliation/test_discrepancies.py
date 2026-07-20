"""Integration tests for GET /reconciliation/discrepancies -- both the
write-time-flagged reasons (is_discrepant column) and the read-time,
staleness-based ones (processed_not_settled, stuck_initiated).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone


async def post_event(client, **overrides) -> None:
    payload = {
        "event_id": str(uuid.uuid4()),
        "merchant_name": "Discrepancy Test Merchant",
        "currency": "INR",
    }
    payload.update(overrides)
    resp = await client.post("/events", json=payload)
    assert resp.status_code in (200, 201), resp.text


async def test_settled_after_failure_appears_in_discrepancies(client):
    merchant_code = "merchant_disc_failure"
    txn_id = str(uuid.uuid4())
    t0 = datetime.now(timezone.utc)

    await post_event(client, event_type="payment_initiated", transaction_id=txn_id, merchant_id=merchant_code, amount="50.00", timestamp=t0.isoformat())
    await post_event(client, event_type="payment_failed", transaction_id=txn_id, merchant_id=merchant_code, amount="50.00", timestamp=(t0 + timedelta(minutes=5)).isoformat())
    await post_event(client, event_type="settled", transaction_id=txn_id, merchant_id=merchant_code, amount="50.00", timestamp=(t0 + timedelta(minutes=10)).isoformat())

    resp = await client.get("/reconciliation/discrepancies", params={"merchant_id": merchant_code})
    assert resp.status_code == 200
    reasons = {row["transaction_id"]: row["discrepancy_reason"] for row in resp.json()}
    assert reasons[txn_id] == "settled_after_failure"


async def test_processed_not_settled_appears_after_stale_threshold(client):
    merchant_code = "merchant_disc_processed"
    txn_id = str(uuid.uuid4())
    old = datetime.now(timezone.utc) - timedelta(hours=48)

    await post_event(client, event_type="payment_initiated", transaction_id=txn_id, merchant_id=merchant_code, amount="75.00", timestamp=old.isoformat())
    await post_event(client, event_type="payment_processed", transaction_id=txn_id, merchant_id=merchant_code, amount="75.00", timestamp=(old + timedelta(minutes=5)).isoformat())

    resp = await client.get("/reconciliation/discrepancies", params={"merchant_id": merchant_code, "stale_after_hours": 24})
    reasons = {row["transaction_id"]: row["discrepancy_reason"] for row in resp.json()}
    assert reasons[txn_id] == "processed_not_settled"


async def test_stuck_initiated_appears_after_stale_threshold(client):
    merchant_code = "merchant_disc_stuck"
    txn_id = str(uuid.uuid4())
    old = datetime.now(timezone.utc) - timedelta(hours=48)

    await post_event(client, event_type="payment_initiated", transaction_id=txn_id, merchant_id=merchant_code, amount="20.00", timestamp=old.isoformat())

    resp = await client.get("/reconciliation/discrepancies", params={"merchant_id": merchant_code, "stale_after_hours": 24})
    reasons = {row["transaction_id"]: row["discrepancy_reason"] for row in resp.json()}
    assert reasons[txn_id] == "stuck_initiated"


async def test_recent_processed_unsettled_is_not_yet_a_discrepancy(client):
    """Within the SLA window -- normal in-flight state, not a discrepancy."""
    merchant_code = "merchant_disc_recent"
    txn_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc)

    await post_event(client, event_type="payment_initiated", transaction_id=txn_id, merchant_id=merchant_code, amount="20.00", timestamp=now.isoformat())
    await post_event(client, event_type="payment_processed", transaction_id=txn_id, merchant_id=merchant_code, amount="20.00", timestamp=(now + timedelta(minutes=1)).isoformat())

    resp = await client.get("/reconciliation/discrepancies", params={"merchant_id": merchant_code, "stale_after_hours": 24})
    txn_ids = {row["transaction_id"] for row in resp.json()}
    assert txn_id not in txn_ids


async def test_merchant_filter_scopes_discrepancies(client):
    m1, m2 = "merchant_disc_a", "merchant_disc_b"
    old = datetime.now(timezone.utc) - timedelta(hours=48)

    await post_event(client, event_type="payment_initiated", transaction_id=str(uuid.uuid4()), merchant_id=m1, amount="1.00", timestamp=old.isoformat())
    await post_event(client, event_type="payment_initiated", transaction_id=str(uuid.uuid4()), merchant_id=m2, amount="1.00", timestamp=old.isoformat())

    resp = await client.get("/reconciliation/discrepancies", params={"merchant_id": m1, "stale_after_hours": 24})
    assert resp.status_code == 200
    body = resp.json()
    assert all(row["merchant_id"] == m1 for row in body)
    assert len(body) == 1


async def test_stale_after_hours_must_be_at_least_one(client):
    resp = await client.get("/reconciliation/discrepancies", params={"stale_after_hours": 0})
    assert resp.status_code == 422


async def test_conflicting_transitions_appears_in_discrepancies(client):
    merchant_code = "merchant_disc_conflict"
    txn_id = str(uuid.uuid4())
    t0 = datetime.now(timezone.utc)

    await post_event(client, event_type="payment_initiated", transaction_id=txn_id, merchant_id=merchant_code, amount="12.00", timestamp=t0.isoformat())
    await post_event(client, event_type="payment_processed", transaction_id=txn_id, merchant_id=merchant_code, amount="12.00", timestamp=(t0 + timedelta(minutes=5)).isoformat())
    await post_event(client, event_type="payment_failed", transaction_id=txn_id, merchant_id=merchant_code, amount="12.00", timestamp=(t0 + timedelta(minutes=10)).isoformat())

    resp = await client.get("/reconciliation/discrepancies", params={"merchant_id": merchant_code})
    assert resp.status_code == 200
    reasons = {row["transaction_id"]: row["discrepancy_reason"] for row in resp.json()}
    assert reasons[txn_id] == "conflicting_transitions"


async def test_settled_before_processed_appears_in_discrepancies(client):
    merchant_code = "merchant_disc_early_settle"
    txn_id = str(uuid.uuid4())
    t0 = datetime.now(timezone.utc)

    await post_event(client, event_type="payment_initiated", transaction_id=txn_id, merchant_id=merchant_code, amount="8.00", timestamp=t0.isoformat())
    await post_event(client, event_type="settled", transaction_id=txn_id, merchant_id=merchant_code, amount="8.00", timestamp=(t0 + timedelta(minutes=5)).isoformat())

    resp = await client.get("/reconciliation/discrepancies", params={"merchant_id": merchant_code})
    assert resp.status_code == 200
    reasons = {row["transaction_id"]: row["discrepancy_reason"] for row in resp.json()}
    assert reasons[txn_id] == "settled_before_processed"


async def test_initiated_missing_appears_in_discrepancies(client):
    merchant_code = "merchant_disc_missing_init"
    txn_id = str(uuid.uuid4())
    t0 = datetime.now(timezone.utc)

    await post_event(client, event_type="payment_processed", transaction_id=txn_id, merchant_id=merchant_code, amount="9.00", timestamp=t0.isoformat())

    resp = await client.get("/reconciliation/discrepancies", params={"merchant_id": merchant_code})
    assert resp.status_code == 200
    reasons = {row["transaction_id"]: row["discrepancy_reason"] for row in resp.json()}
    assert reasons[txn_id] == "initiated_missing"


async def test_unknown_merchant_returns_empty_list_not_error(client):
    resp = await client.get("/reconciliation/discrepancies", params={"merchant_id": "does-not-exist"})
    assert resp.status_code == 200
    assert resp.json() == []


async def test_limit_caps_number_of_results(client):
    merchant_code = "merchant_disc_limit"
    old = datetime.now(timezone.utc) - timedelta(hours=48)

    for _ in range(3):
        await post_event(
            client, event_type="payment_initiated", transaction_id=str(uuid.uuid4()), merchant_id=merchant_code, amount="1.00", timestamp=old.isoformat()
        )

    resp = await client.get(
        "/reconciliation/discrepancies", params={"merchant_id": merchant_code, "stale_after_hours": 24, "limit": 2}
    )
    assert resp.status_code == 200
    assert len(resp.json()) == 2


async def test_results_are_ordered_by_last_event_at_desc(client):
    merchant_code = "merchant_disc_order"
    old = datetime.now(timezone.utc) - timedelta(hours=48)

    txn_ids = [str(uuid.uuid4()) for _ in range(3)]
    for i, txn_id in enumerate(txn_ids):
        await post_event(
            client,
            event_type="payment_initiated",
            transaction_id=txn_id,
            merchant_id=merchant_code,
            amount="1.00",
            timestamp=(old + timedelta(minutes=i)).isoformat(),
        )

    resp = await client.get(
        "/reconciliation/discrepancies", params={"merchant_id": merchant_code, "stale_after_hours": 24}
    )
    assert resp.status_code == 200
    ordered_ids = [row["transaction_id"] for row in resp.json()]
    assert ordered_ids == list(reversed(txn_ids))
