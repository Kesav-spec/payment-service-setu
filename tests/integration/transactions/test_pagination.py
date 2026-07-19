"""Integration tests for GET /transactions cursor pagination and sorting."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone


async def seed_transactions(client, count: int, merchant_code: str) -> list[str]:
    t0 = datetime.now(timezone.utc)
    txn_ids = []
    for i in range(count):
        txn_id = str(uuid.uuid4())
        txn_ids.append(txn_id)
        resp = await client.post(
            "/events",
            json={
                "event_id": str(uuid.uuid4()),
                "event_type": "payment_initiated",
                "transaction_id": txn_id,
                "merchant_id": merchant_code,
                "merchant_name": "Pagination Test Merchant",
                "amount": f"{100 + i}.00",
                "currency": "INR",
                "timestamp": (t0 + timedelta(seconds=i)).isoformat(),
            },
        )
        assert resp.status_code == 201
    return txn_ids


async def test_cursor_pagination_covers_all_rows_with_no_overlap(client):
    merchant_code = "merchant_page_1"
    txn_ids = await seed_transactions(client, 25, merchant_code)

    seen: list[str] = []
    cursor = None
    for _ in range(20):  # safety cap so a bug can't spin this loop forever
        params = {"merchant_id": merchant_code, "limit": 10}
        if cursor:
            params["cursor"] = cursor
        resp = await client.get("/transactions", params=params)
        assert resp.status_code == 200
        body = resp.json()
        seen.extend(item["id"] for item in body["items"])
        cursor = body["next_cursor"]
        if cursor is None:
            break

    assert len(seen) == len(txn_ids)
    assert len(set(seen)) == len(seen), "cursor pages overlapped"
    assert set(seen) == set(txn_ids)


async def test_next_cursor_is_null_on_the_last_page(client):
    merchant_code = "merchant_page_2"
    await seed_transactions(client, 3, merchant_code)

    resp = await client.get("/transactions", params={"merchant_id": merchant_code, "limit": 10})
    body = resp.json()
    assert len(body["items"]) == 3
    assert body["next_cursor"] is None


async def test_invalid_cursor_returns_400(client):
    resp = await client.get("/transactions", params={"cursor": "not-valid-base64!!!"})
    assert resp.status_code == 400


async def test_sort_by_amount_asc_orders_correctly(client):
    merchant_code = "merchant_page_3"
    await seed_transactions(client, 5, merchant_code)

    resp = await client.get(
        "/transactions",
        params={"merchant_id": merchant_code, "sort_by": "amount", "sort_dir": "asc", "limit": 10},
    )
    amounts = [float(item["amount"]) for item in resp.json()["items"]]
    assert amounts == sorted(amounts)
    assert len(amounts) == 5


async def test_sort_by_created_at_desc_is_the_default(client):
    merchant_code = "merchant_page_4"
    txn_ids = await seed_transactions(client, 5, merchant_code)

    resp = await client.get("/transactions", params={"merchant_id": merchant_code, "limit": 10})
    ids = [item["id"] for item in resp.json()["items"]]
    # Inserted in order 0..4 -- default (created_at desc) should come back reversed.
    assert ids == list(reversed(txn_ids))


async def test_invalid_sort_by_is_rejected(client):
    resp = await client.get("/transactions", params={"sort_by": "not_a_real_column"})
    assert resp.status_code == 422


async def test_status_alias_matches_payment_status_filter(client):
    merchant_code = "merchant_page_5"
    await seed_transactions(client, 1, merchant_code)

    via_status = await client.get("/transactions", params={"merchant_id": merchant_code, "status": "INITIATED"})
    via_payment_status = await client.get(
        "/transactions", params={"merchant_id": merchant_code, "payment_status": "INITIATED"}
    )
    assert via_status.status_code == via_payment_status.status_code == 200
    assert via_status.json()["items"] == via_payment_status.json()["items"]
    assert len(via_status.json()["items"]) == 1


async def test_unknown_merchant_returns_empty_page_not_error(client):
    resp = await client.get("/transactions", params={"merchant_id": "does-not-exist"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["items"] == []
    assert body["next_cursor"] is None
