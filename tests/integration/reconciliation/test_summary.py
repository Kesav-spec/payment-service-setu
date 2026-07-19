"""Integration tests for GET /reconciliation/summary (grouped aggregation)."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone


async def post_event(client, **overrides) -> str:
    txn_id = overrides.pop("transaction_id", None) or str(uuid.uuid4())
    payload = {
        "event_id": str(uuid.uuid4()),
        "event_type": "payment_initiated",
        "transaction_id": txn_id,
        "merchant_id": "merchant_recon",
        "merchant_name": "Reconciliation Test Merchant",
        "amount": "100.00",
        "currency": "INR",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    payload.update(overrides)
    resp = await client.post("/events", json=payload)
    assert resp.status_code in (200, 201), resp.text
    return txn_id


async def test_group_by_status_totals_match_seeded_data(client):
    merchant_code = "merchant_recon_status"
    await post_event(client, merchant_id=merchant_code, event_type="payment_initiated", amount="10.00")
    await post_event(client, merchant_id=merchant_code, event_type="payment_initiated", amount="20.00")
    txn = await post_event(client, merchant_id=merchant_code, event_type="payment_initiated", amount="30.00")
    await post_event(client, merchant_id=merchant_code, transaction_id=txn, event_type="payment_processed", amount="30.00")

    resp = await client.get("/reconciliation/summary", params={"group_by": "status", "merchant_id": merchant_code})
    assert resp.status_code == 200
    groups = {g["group_key"]: g for g in resp.json()}

    assert groups["INITIATED"]["total_transactions"] == 2
    assert float(groups["INITIATED"]["total_amount"]) == 30.00
    assert groups["PROCESSED"]["total_transactions"] == 1
    assert float(groups["PROCESSED"]["total_amount"]) == 30.00


async def test_group_by_merchant_scopes_correctly(client):
    m1, m2 = "merchant_recon_a", "merchant_recon_b"
    await post_event(client, merchant_id=m1, amount="10.00")
    await post_event(client, merchant_id=m2, amount="20.00")
    await post_event(client, merchant_id=m2, amount="25.00")

    resp = await client.get("/reconciliation/summary", params={"group_by": "merchant"})
    assert resp.status_code == 200
    groups = {g["group_key"]: g for g in resp.json()}

    assert groups[m1]["total_transactions"] == 1
    assert groups[m2]["total_transactions"] == 2
    assert float(groups[m2]["total_amount"]) == 45.00


async def test_group_by_date_buckets_by_day(client):
    merchant_code = "merchant_recon_date"
    await post_event(client, merchant_id=merchant_code, timestamp="2026-01-08T10:00:00+00:00")
    await post_event(client, merchant_id=merchant_code, timestamp="2026-01-08T18:00:00+00:00")
    await post_event(client, merchant_id=merchant_code, timestamp="2026-01-09T10:00:00+00:00")

    resp = await client.get("/reconciliation/summary", params={"group_by": "date", "merchant_id": merchant_code})
    assert resp.status_code == 200
    groups = {g["group_key"]: g for g in resp.json()}

    assert groups["2026-01-08"]["total_transactions"] == 2
    assert groups["2026-01-09"]["total_transactions"] == 1


async def test_group_by_is_required(client):
    resp = await client.get("/reconciliation/summary")
    assert resp.status_code == 422


async def test_invalid_group_by_is_rejected(client):
    resp = await client.get("/reconciliation/summary", params={"group_by": "bogus"})
    assert resp.status_code == 422


async def test_unknown_merchant_returns_empty_list_not_error(client):
    resp = await client.get(
        "/reconciliation/summary", params={"group_by": "status", "merchant_id": "does-not-exist"}
    )
    assert resp.status_code == 200
    assert resp.json() == []
