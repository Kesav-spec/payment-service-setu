"""Integration tests for POST /events request validation (422s) -- the
custom timestamp-timezone validator and the Field-level constraints on
EventCreate, exercised through the actual HTTP layer.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone


def event_payload(**overrides) -> dict:
    payload = {
        "event_id": str(uuid.uuid4()),
        "event_type": "payment_initiated",
        "transaction_id": str(uuid.uuid4()),
        "merchant_id": "merchant_validation_test",
        "merchant_name": "Validation Test Merchant",
        "amount": "100.00",
        "currency": "INR",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    payload.update(overrides)
    return payload


async def test_naive_timestamp_is_rejected(client):
    resp = await client.post("/events", json=event_payload(timestamp="2026-01-01T10:00:00"))
    assert resp.status_code == 422


async def test_zero_amount_is_rejected(client):
    resp = await client.post("/events", json=event_payload(amount="0.00"))
    assert resp.status_code == 422


async def test_negative_amount_is_rejected(client):
    resp = await client.post("/events", json=event_payload(amount="-5.00"))
    assert resp.status_code == 422


async def test_amount_with_too_many_decimal_places_is_rejected(client):
    resp = await client.post("/events", json=event_payload(amount="100.123"))
    assert resp.status_code == 422


async def test_invalid_currency_code_is_rejected(client):
    resp = await client.post("/events", json=event_payload(currency="usd"))
    assert resp.status_code == 422


async def test_invalid_event_type_is_rejected(client):
    resp = await client.post("/events", json=event_payload(event_type="refunded"))
    assert resp.status_code == 422


async def test_malformed_event_id_is_rejected(client):
    resp = await client.post("/events", json=event_payload(event_id="not-a-uuid"))
    assert resp.status_code == 422


async def test_missing_required_field_is_rejected(client):
    payload = event_payload()
    del payload["merchant_name"]
    resp = await client.post("/events", json=payload)
    assert resp.status_code == 422


async def test_missing_merchant_id_is_rejected(client):
    payload = event_payload()
    del payload["merchant_id"]
    resp = await client.post("/events", json=payload)
    assert resp.status_code == 422


async def test_malformed_json_body_is_rejected(client):
    resp = await client.post(
        "/events",
        content=b"{not valid json",
        headers={"content-type": "application/json"},
    )
    assert resp.status_code == 422
