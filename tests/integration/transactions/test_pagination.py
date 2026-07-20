"""Integration tests for GET /transactions cursor pagination and sorting."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from app.models import EventType, Merchant, PaymentStatus, SettlementStatus, Transaction


async def post_event(client, **overrides) -> dict:
    payload = {
        "event_id": str(uuid.uuid4()),
        "merchant_id": "merchant_page_filter_test",
        "merchant_name": "Pagination Filter Test Merchant",
        "amount": "60.00",
        "currency": "INR",
    }
    payload.update(overrides)
    resp = await client.post("/events", json=payload)
    assert resp.status_code in (200, 201), resp.text
    return resp.json()


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


async def test_settlement_status_filter(client):
    merchant_code = "merchant_page_settlement"
    t0 = datetime.now(timezone.utc)

    unsettled_txn = str(uuid.uuid4())
    await post_event(
        client, event_type="payment_initiated", transaction_id=unsettled_txn, merchant_id=merchant_code, timestamp=t0.isoformat()
    )

    settled_txn = str(uuid.uuid4())
    await post_event(
        client, event_type="payment_initiated", transaction_id=settled_txn, merchant_id=merchant_code, timestamp=t0.isoformat()
    )
    await post_event(
        client,
        event_type="payment_processed",
        transaction_id=settled_txn,
        merchant_id=merchant_code,
        timestamp=(t0 + timedelta(minutes=1)).isoformat(),
    )
    await post_event(
        client,
        event_type="settled",
        transaction_id=settled_txn,
        merchant_id=merchant_code,
        timestamp=(t0 + timedelta(minutes=2)).isoformat(),
    )

    resp = await client.get(
        "/transactions", params={"merchant_id": merchant_code, "settlement_status": "SETTLED"}
    )
    assert resp.status_code == 200
    ids = [item["id"] for item in resp.json()["items"]]
    assert ids == [settled_txn]


async def test_is_discrepant_filter(client):
    merchant_code = "merchant_page_discrepant"
    t0 = datetime.now(timezone.utc)

    clean_txn = str(uuid.uuid4())
    await post_event(
        client, event_type="payment_initiated", transaction_id=clean_txn, merchant_id=merchant_code, timestamp=t0.isoformat()
    )

    discrepant_txn = str(uuid.uuid4())
    await post_event(
        client,
        event_type="payment_initiated",
        transaction_id=discrepant_txn,
        merchant_id=merchant_code,
        timestamp=t0.isoformat(),
    )
    await post_event(
        client,
        event_type="settled",
        transaction_id=discrepant_txn,
        merchant_id=merchant_code,
        timestamp=(t0 + timedelta(minutes=1)).isoformat(),
    )  # settled_before_processed -> is_discrepant

    resp = await client.get("/transactions", params={"merchant_id": merchant_code, "is_discrepant": True})
    assert resp.status_code == 200
    ids = [item["id"] for item in resp.json()["items"]]
    assert ids == [discrepant_txn]


async def test_from_date_to_date_filters_bound_first_event_at(client):
    merchant_code = "merchant_page_daterange"
    base = datetime(2026, 2, 1, tzinfo=timezone.utc)

    early_txn = str(uuid.uuid4())
    await post_event(
        client, event_type="payment_initiated", transaction_id=early_txn, merchant_id=merchant_code, timestamp=base.isoformat()
    )
    mid_txn = str(uuid.uuid4())
    await post_event(
        client,
        event_type="payment_initiated",
        transaction_id=mid_txn,
        merchant_id=merchant_code,
        timestamp=(base + timedelta(days=2)).isoformat(),
    )
    late_txn = str(uuid.uuid4())
    await post_event(
        client,
        event_type="payment_initiated",
        transaction_id=late_txn,
        merchant_id=merchant_code,
        timestamp=(base + timedelta(days=4)).isoformat(),
    )

    resp = await client.get(
        "/transactions",
        params={
            "merchant_id": merchant_code,
            "from_date": (base + timedelta(days=1)).isoformat(),
            "to_date": (base + timedelta(days=3)).isoformat(),
        },
    )
    assert resp.status_code == 200
    ids = [item["id"] for item in resp.json()["items"]]
    assert ids == [mid_txn]


async def test_from_date_filter_alone_is_a_lower_bound(client):
    merchant_code = "merchant_page_fromdate"
    base = datetime(2026, 4, 1, tzinfo=timezone.utc)

    old_txn = str(uuid.uuid4())
    await post_event(
        client, event_type="payment_initiated", transaction_id=old_txn, merchant_id=merchant_code, timestamp=base.isoformat()
    )
    new_txn = str(uuid.uuid4())
    await post_event(
        client,
        event_type="payment_initiated",
        transaction_id=new_txn,
        merchant_id=merchant_code,
        timestamp=(base + timedelta(days=5)).isoformat(),
    )

    resp = await client.get(
        "/transactions",
        params={"merchant_id": merchant_code, "from_date": (base + timedelta(days=2)).isoformat()},
    )
    assert resp.status_code == 200
    ids = [item["id"] for item in resp.json()["items"]]
    assert ids == [new_txn]


async def test_to_date_filter_alone_is_an_upper_bound(client):
    merchant_code = "merchant_page_todate"
    base = datetime(2026, 4, 1, tzinfo=timezone.utc)

    early_txn = str(uuid.uuid4())
    await post_event(
        client, event_type="payment_initiated", transaction_id=early_txn, merchant_id=merchant_code, timestamp=base.isoformat()
    )
    late_txn = str(uuid.uuid4())
    await post_event(
        client,
        event_type="payment_initiated",
        transaction_id=late_txn,
        merchant_id=merchant_code,
        timestamp=(base + timedelta(days=5)).isoformat(),
    )

    resp = await client.get(
        "/transactions",
        params={"merchant_id": merchant_code, "to_date": (base + timedelta(days=2)).isoformat()},
    )
    assert resp.status_code == 200
    ids = [item["id"] for item in resp.json()["items"]]
    assert ids == [early_txn]


async def test_combined_filters_apply_as_intersection(client):
    """merchant_id + payment_status + settlement_status together, not just
    each individually -- two near-miss transactions each satisfy all but one
    of the three filters.
    """
    merchant_code = "merchant_page_combined"
    other_merchant_code = "merchant_page_combined_other"
    t0 = datetime.now(timezone.utc)

    async def make_settled(txn_id: str, merchant_id: str) -> None:
        await post_event(client, event_type="payment_initiated", transaction_id=txn_id, merchant_id=merchant_id, timestamp=t0.isoformat())
        await post_event(
            client,
            event_type="payment_processed",
            transaction_id=txn_id,
            merchant_id=merchant_id,
            timestamp=(t0 + timedelta(minutes=1)).isoformat(),
        )
        await post_event(
            client,
            event_type="settled",
            transaction_id=txn_id,
            merchant_id=merchant_id,
            timestamp=(t0 + timedelta(minutes=2)).isoformat(),
        )

    target_txn = str(uuid.uuid4())
    await make_settled(target_txn, merchant_code)

    # Right merchant, right payment_status, but not settled.
    unsettled_txn = str(uuid.uuid4())
    await post_event(client, event_type="payment_initiated", transaction_id=unsettled_txn, merchant_id=merchant_code, timestamp=t0.isoformat())
    await post_event(
        client,
        event_type="payment_processed",
        transaction_id=unsettled_txn,
        merchant_id=merchant_code,
        timestamp=(t0 + timedelta(minutes=1)).isoformat(),
    )

    # Right payment_status and settlement_status, but wrong merchant.
    other_merchant_txn = str(uuid.uuid4())
    await make_settled(other_merchant_txn, other_merchant_code)

    resp = await client.get(
        "/transactions",
        params={"merchant_id": merchant_code, "payment_status": "PROCESSED", "settlement_status": "SETTLED"},
    )
    assert resp.status_code == 200
    ids = [item["id"] for item in resp.json()["items"]]
    assert ids == [target_txn]


async def test_sort_by_first_event_at_orders_correctly(client):
    merchant_code = "merchant_page_first_event"
    base = datetime(2026, 5, 1, tzinfo=timezone.utc)

    # Inserted in descending first_event_at order, so a correct result here
    # can't be explained by insertion (created_at) order coinciding with it.
    txn_ids = []
    for i in range(4):
        txn_id = str(uuid.uuid4())
        txn_ids.append(txn_id)
        await post_event(
            client,
            event_type="payment_initiated",
            transaction_id=txn_id,
            merchant_id=merchant_code,
            timestamp=(base + timedelta(days=3 - i)).isoformat(),
        )

    resp = await client.get(
        "/transactions",
        params={"merchant_id": merchant_code, "sort_by": "first_event_at", "sort_dir": "asc", "limit": 10},
    )
    assert resp.status_code == 200
    ids = [item["id"] for item in resp.json()["items"]]
    assert ids == list(reversed(txn_ids))


async def test_sort_by_amount_desc_orders_correctly(client):
    merchant_code = "merchant_page_amount_desc"
    await seed_transactions(client, 5, merchant_code)

    resp = await client.get(
        "/transactions",
        params={"merchant_id": merchant_code, "sort_by": "amount", "sort_dir": "desc", "limit": 10},
    )
    assert resp.status_code == 200
    amounts = [float(item["amount"]) for item in resp.json()["items"]]
    assert amounts == sorted(amounts, reverse=True)
    assert len(amounts) == 5


async def test_created_at_ties_are_broken_by_id(client, db_session):
    """created_at is a server-generated timestamp that the API gives callers
    no way to control, so a genuine tie can't be produced by posting events
    through the normal flow -- insert directly via the DB session (as the
    model docstring notes, bulk-loaded rows can plausibly share a created_at)
    and verify id serves as the secondary sort key so cursor pagination
    doesn't skip or duplicate either row.
    """
    merchant = Merchant(merchant_code="merchant_page_tiebreak", merchant_name="Tie-break Test Merchant")
    db_session.add(merchant)
    await db_session.flush()

    tied_at = datetime.now(timezone.utc)
    txn_ids = sorted(uuid.uuid4() for _ in range(2))
    for txn_id in txn_ids:
        db_session.add(
            Transaction(
                id=txn_id,
                merchant_id=merchant.id,
                amount=Decimal("10.00"),
                currency="INR",
                payment_status=PaymentStatus.INITIATED,
                settlement_status=SettlementStatus.UNSETTLED,
                first_event_at=tied_at,
                last_event_at=tied_at,
                last_event_type=EventType.PAYMENT_INITIATED,
                created_at=tied_at,
            )
        )
    await db_session.commit()

    seen: list[str] = []
    cursor = None
    for _ in range(5):
        params = {"merchant_id": "merchant_page_tiebreak", "sort_by": "created_at", "sort_dir": "asc", "limit": 1}
        if cursor:
            params["cursor"] = cursor
        resp = await client.get("/transactions", params=params)
        assert resp.status_code == 200
        body = resp.json()
        seen.extend(item["id"] for item in body["items"])
        cursor = body["next_cursor"]
        if cursor is None:
            break

    assert seen == [str(t) for t in txn_ids]


async def test_limit_below_minimum_is_rejected(client):
    resp = await client.get("/transactions", params={"limit": 0})
    assert resp.status_code == 422


async def test_limit_above_maximum_is_rejected(client):
    resp = await client.get("/transactions", params={"limit": 101})
    assert resp.status_code == 422


async def test_limit_at_maximum_boundary_is_accepted(client):
    resp = await client.get("/transactions", params={"limit": 100})
    assert resp.status_code == 200
