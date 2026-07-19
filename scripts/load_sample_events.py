"""Idempotent loader for data/sample_events.json.

Populates merchants, transactions (derived current state) and payment_events
(the append-only ledger) from a flat list of raw lifecycle events. Safe to
run repeatedly against the same or a growing file: rerunning with an
unchanged file inserts zero new rows and leaves transaction state untouched.

Usage:
    DATABASE_URL=... python scripts/load_sample_events.py [path/to/events.json]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections import defaultdict
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import AsyncSessionLocal
from app.models import Merchant, PaymentEvent, PaymentStatus, SettlementStatus, Transaction

DEFAULT_PATH = Path(__file__).resolve().parent.parent / "data" / "sample_events.json"
CHUNK_SIZE = 1000

_TERMINAL_STATUS = {
    "payment_processed": PaymentStatus.PROCESSED,
    "payment_failed": PaymentStatus.FAILED,
}


def _chunks(rows: list[dict], size: int):
    for i in range(0, len(rows), size):
        yield rows[i : i + size]


def _load_raw_events(path: Path) -> list[dict]:
    with path.open() as f:
        events = json.load(f)
    for e in events:
        e["timestamp"] = datetime.fromisoformat(e["timestamp"])
    return events


def _new_transaction_state(e: dict) -> dict[str, Any]:
    return {
        "id": e["transaction_id"],
        "merchant_code": e["merchant_id"],
        "amount": Decimal(str(e["amount"])),
        "currency": e["currency"],
        "payment_status": PaymentStatus.INITIATED,
        "settlement_status": SettlementStatus.UNSETTLED,
        "first_event_at": e["timestamp"],
        "last_event_at": e["timestamp"],
        "last_event_type": e["event_type"],
        "settled_at": None,
        "is_discrepant": False,
        "discrepancy_reason": None,
    }


def _apply_event(state: dict | None, e: dict) -> tuple[bool, dict]:
    """Replay one event onto a transaction's running state.

    Returns (is_applied, new_state). is_applied is False when the event was
    stored for history but did not change state: a resend that arrived after
    later progress, a duplicate terminal event, or an event whose timestamp
    is older than the last one already applied (out-of-order arrival can't
    rewrite history).
    """
    event_type = e["event_type"]
    ts = e["timestamp"]

    if state is None:
        if event_type != "payment_initiated":
            # Defensive: a well-behaved producer never emits this, but a
            # transaction row must still exist for the FK on payment_events.
            state = _new_transaction_state(e)
            state["payment_status"] = _TERMINAL_STATUS.get(event_type, PaymentStatus.INITIATED)
            state["is_discrepant"] = True
            state["discrepancy_reason"] = "initiated_missing"
            return True, state
        return True, _new_transaction_state(e)

    if ts < state["last_event_at"]:
        return False, state

    if event_type == "payment_initiated":
        return False, state

    if event_type in _TERMINAL_STATUS:
        new_status = _TERMINAL_STATUS[event_type]
        if state["payment_status"] == PaymentStatus.INITIATED:
            state["payment_status"] = new_status
            state["last_event_at"] = ts
            state["last_event_type"] = event_type
            return True, state
        if state["payment_status"] == new_status:
            return False, state
        # e.g. payment_processed arriving after payment_failed, or vice versa.
        state["is_discrepant"] = True
        state["discrepancy_reason"] = "conflicting_transitions"
        return False, state

    if event_type == "settled":
        if state["settlement_status"] == SettlementStatus.SETTLED:
            return False, state
        state["settlement_status"] = SettlementStatus.SETTLED
        state["settled_at"] = ts
        state["last_event_at"] = ts
        state["last_event_type"] = event_type
        if state["payment_status"] == PaymentStatus.FAILED:
            state["is_discrepant"] = True
            state["discrepancy_reason"] = "settled_after_failure"
        elif state["payment_status"] == PaymentStatus.INITIATED:
            state["is_discrepant"] = True
            state["discrepancy_reason"] = "settled_before_processed"
        return True, state

    raise ValueError(f"unknown event_type: {event_type!r}")


def _run_state_machine(events: list[dict]) -> tuple[dict[str, bool], dict[str, dict]]:
    """events must already be deduplicated by event_id (one entry each)."""
    by_txn: dict[str, list[dict]] = defaultdict(list)
    for e in events:
        by_txn[e["transaction_id"]].append(e)

    is_applied: dict[str, bool] = {}
    transactions: dict[str, dict] = {}

    for txn_id, txn_events in by_txn.items():
        txn_events.sort(key=lambda e: e["timestamp"])
        state: dict | None = None
        for e in txn_events:
            applied, state = _apply_event(state, e)
            is_applied[e["event_id"]] = applied
        transactions[txn_id] = state

    return is_applied, transactions


async def _upsert_merchants(session: AsyncSession, events: list[dict]) -> dict[str, Any]:
    names_by_code = {e["merchant_id"]: e["merchant_name"] for e in events}
    rows = [{"merchant_code": code, "merchant_name": name} for code, name in names_by_code.items()]

    stmt = pg_insert(Merchant).values(rows).on_conflict_do_nothing(index_elements=["merchant_code"])
    await session.execute(stmt)
    await session.commit()

    result = await session.execute(select(Merchant.merchant_code, Merchant.id))
    return dict(result.all())


async def _upsert_transactions(session: AsyncSession, tx_states: dict[str, dict], merchant_map: dict[str, Any]) -> None:
    mutable_cols = (
        "merchant_id", "amount", "currency", "payment_status", "settlement_status",
        "first_event_at", "last_event_at", "last_event_type", "settled_at",
        "is_discrepant", "discrepancy_reason",
    )
    rows = []
    for state in tx_states.values():
        rows.append({
            "id": state["id"],
            "merchant_id": merchant_map[state["merchant_code"]],
            "amount": state["amount"],
            "currency": state["currency"],
            "payment_status": state["payment_status"],
            "settlement_status": state["settlement_status"],
            "first_event_at": state["first_event_at"],
            "last_event_at": state["last_event_at"],
            "last_event_type": state["last_event_type"],
            "settled_at": state["settled_at"],
            "is_discrepant": state["is_discrepant"],
            "discrepancy_reason": state["discrepancy_reason"],
        })

    for chunk in _chunks(rows, CHUNK_SIZE):
        stmt = pg_insert(Transaction).values(chunk)
        stmt = stmt.on_conflict_do_update(
            index_elements=["id"],
            set_={col: getattr(stmt.excluded, col) for col in mutable_cols},
        )
        await session.execute(stmt)
    await session.commit()


async def _insert_payment_events(
    session: AsyncSession,
    raw_events: list[dict],
    merchant_map: dict[str, Any],
    is_applied_map: dict[str, bool],
) -> int:
    """Bulk insert every raw event, duplicates included -- ON CONFLICT (event_id)
    DO NOTHING is the idempotency guard, not a Python-side pre-filter. Re-running
    the loader, or a source file that literally repeats an event, is a no-op here.
    """
    rows = [
        {
            "event_id": e["event_id"],
            "event_type": e["event_type"],
            "transaction_id": e["transaction_id"],
            "merchant_id": merchant_map[e["merchant_id"]],
            "amount": Decimal(str(e["amount"])),
            "currency": e["currency"],
            "event_timestamp": e["timestamp"],
            "is_applied": is_applied_map[e["event_id"]],
        }
        for e in raw_events
    ]

    inserted = 0
    for chunk in _chunks(rows, CHUNK_SIZE):
        stmt = (
            pg_insert(PaymentEvent)
            .values(chunk)
            .on_conflict_do_nothing(index_elements=["event_id"])
            .returning(PaymentEvent.event_id)
        )
        result = await session.execute(stmt)
        inserted += len(result.all())
    await session.commit()
    return inserted


async def load(path: Path) -> None:
    raw_events = _load_raw_events(path)
    # Exact resends (same event_id) carry no new information -- the state
    # machine only needs to see one copy of each. The full, duplicate-laden
    # list is still what gets sent to payment_events below.
    unique_events = list({e["event_id"]: e for e in raw_events}.values())
    is_applied_map, tx_states = _run_state_machine(unique_events)

    async with AsyncSessionLocal() as session:
        merchant_map = await _upsert_merchants(session, raw_events)
        await _upsert_transactions(session, tx_states, merchant_map)
        inserted = await _insert_payment_events(session, raw_events, merchant_map, is_applied_map)

    discrepant = sum(1 for s in tx_states.values() if s["is_discrepant"])
    print(f"events read:        {len(raw_events)}")
    print(f"distinct event_ids: {len(unique_events)}")
    print(f"events inserted:    {inserted}")
    print(f"duplicates skipped: {len(raw_events) - inserted}")
    print(f"merchants:          {len(merchant_map)}")
    print(f"transactions:       {len(tx_states)}")
    print(f"discrepant:         {discrepant}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", nargs="?", type=Path, default=DEFAULT_PATH)
    args = parser.parse_args()
    asyncio.run(load(args.path))
