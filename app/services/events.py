from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import EventType, PaymentStatus, SettlementStatus, Transaction
from app.repositories import events as events_repo
from app.schemas import EventCreate, EventIngestResponse

_TERMINAL_STATUS = {
    EventType.PAYMENT_PROCESSED: PaymentStatus.PROCESSED,
    EventType.PAYMENT_FAILED: PaymentStatus.FAILED,
}


def _apply_event(prior: Transaction | None, event: EventCreate) -> tuple[bool, dict[str, Any]]:
    """Pure state-machine step: given a transaction's current state (None if
    this transaction_id has never been seen) and one incoming event, decide
    whether it moves state forward and what the row should look like after.

    Nothing here ever rejects an event outright -- invalid, late, or
    already-reflected events are still stored (payment_events history is
    untouched by this function), just marked is_applied=False, and flagged
    is_discrepant when the mismatch is a genuine data problem rather than a
    harmless resend.
    """
    ts = event.timestamp
    et = event.event_type

    if prior is None:
        if et != EventType.PAYMENT_INITIATED:
            # Defensive: a well-behaved producer always sends payment_initiated
            # first. Still create the row (payment_events' FK requires it).
            return True, {
                "id": event.transaction_id,
                "merchant_code": event.merchant_id,
                "amount": event.amount,
                "currency": event.currency,
                "payment_status": _TERMINAL_STATUS.get(et, PaymentStatus.INITIATED),
                "settlement_status": SettlementStatus.UNSETTLED,
                "first_event_at": ts,
                "last_event_at": ts,
                "last_event_type": et,
                "settled_at": None,
                "is_discrepant": True,
                "discrepancy_reason": "initiated_missing",
            }
        return True, {
            "id": event.transaction_id,
            "merchant_code": event.merchant_id,
            "amount": event.amount,
            "currency": event.currency,
            "payment_status": PaymentStatus.INITIATED,
            "settlement_status": SettlementStatus.UNSETTLED,
            "first_event_at": ts,
            "last_event_at": ts,
            "last_event_type": et,
            "settled_at": None,
            "is_discrepant": False,
            "discrepancy_reason": None,
        }

    state: dict[str, Any] = {
        "id": prior.id,
        "merchant_code": event.merchant_id,
        "amount": prior.amount,
        "currency": prior.currency,
        "payment_status": prior.payment_status,
        "settlement_status": prior.settlement_status,
        "first_event_at": prior.first_event_at,
        "last_event_at": prior.last_event_at,
        "last_event_type": prior.last_event_type,
        "settled_at": prior.settled_at,
        "is_discrepant": prior.is_discrepant,
        "discrepancy_reason": prior.discrepancy_reason,
    }

    # Out-of-order arrival: can't rewrite history with something older than
    # what's already been applied.
    if ts < state["last_event_at"]:
        return False, state

    if et == EventType.PAYMENT_INITIATED:
        # Already initiated; a resend that reached us after other progress.
        return False, state

    if et in _TERMINAL_STATUS:
        new_status = _TERMINAL_STATUS[et]
        if state["payment_status"] == PaymentStatus.INITIATED:
            state["payment_status"] = new_status
            state["last_event_at"] = ts
            state["last_event_type"] = et
            return True, state
        if state["payment_status"] == new_status:
            return False, state
        # e.g. payment_processed arriving after payment_failed, or vice versa.
        state["is_discrepant"] = True
        state["discrepancy_reason"] = "conflicting_transitions"
        return False, state

    if et == EventType.SETTLED:
        if state["settlement_status"] == SettlementStatus.SETTLED:
            return False, state
        state["settlement_status"] = SettlementStatus.SETTLED
        state["settled_at"] = ts
        state["last_event_at"] = ts
        state["last_event_type"] = et
        if state["payment_status"] == PaymentStatus.FAILED:
            state["is_discrepant"] = True
            state["discrepancy_reason"] = "settled_after_failure"
        elif state["payment_status"] == PaymentStatus.INITIATED:
            state["is_discrepant"] = True
            state["discrepancy_reason"] = "settled_before_processed"
        return True, state

    raise ValueError(f"unhandled event_type: {et!r}")


async def ingest_event(session: AsyncSession, payload: EventCreate) -> EventIngestResponse:
    await events_repo.lock_transaction(session, payload.transaction_id)

    merchant_id = await events_repo.get_or_create_merchant_id(session, payload.merchant_id, payload.merchant_name)
    prior = await events_repo.get_transaction(session, payload.transaction_id)

    is_applied, state = _apply_event(prior, payload)
    state["merchant_id"] = merchant_id
    del state["merchant_code"]

    # transactions must be written before payment_events, which has an FK to
    # it -- on a transaction's very first event there is no row yet for the
    # event to reference. When this event turns out to be an exact resend,
    # recomputing the transition against a prior state that already reflects
    # it is always a no-op (append-only, forward-only state machine), so this
    # upsert is idempotent either way -- no special-casing needed here.
    txn_id, payment_status, settlement_status, discrepancy_reason = await events_repo.upsert_transaction(
        session, state
    )

    inserted = await events_repo.insert_payment_event(
        session,
        event_id=payload.event_id,
        event_type=payload.event_type,
        transaction_id=payload.transaction_id,
        merchant_id=merchant_id,
        amount=payload.amount,
        currency=payload.currency,
        event_timestamp=payload.timestamp,
        is_applied=is_applied,
    )
    await session.commit()

    return EventIngestResponse(
        event_id=payload.event_id,
        status="accepted" if inserted else "duplicate",
        is_applied=is_applied,
        transaction_id=txn_id,
        payment_status=payment_status,
        settlement_status=settlement_status,
        discrepancy_reason=discrepancy_reason,
    )
