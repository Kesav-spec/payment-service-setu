"""Pure unit tests of the ingestion state machine (app.services.events._apply_event).

No DB, no HTTP -- _apply_event takes a plain Transaction ORM instance (never
persisted, just constructed in memory) and an EventCreate, and returns
(is_applied, new_state) synchronously. This is the cheapest, most direct way
to cover state-transition edge cases exhaustively; testing the underscored
function directly is a deliberate choice for that reason.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from app.models import EventType, PaymentStatus, SettlementStatus, Transaction
from app.schemas import EventCreate
from app.services.events import _apply_event

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


def make_event(event_type: EventType, ts: datetime, **overrides) -> EventCreate:
    defaults = dict(
        event_id=uuid.uuid4(),
        event_type=event_type,
        transaction_id=uuid.uuid4(),
        merchant_id="merchant_1",
        merchant_name="Test Merchant",
        amount=Decimal("100.00"),
        currency="INR",
        timestamp=ts,
    )
    defaults.update(overrides)
    return EventCreate(**defaults)


def make_prior(**overrides) -> Transaction:
    defaults = dict(
        id=uuid.uuid4(),
        merchant_id=uuid.uuid4(),
        amount=Decimal("100.00"),
        currency="INR",
        payment_status=PaymentStatus.INITIATED,
        settlement_status=SettlementStatus.UNSETTLED,
        first_event_at=T0,
        last_event_at=T0,
        last_event_type=EventType.PAYMENT_INITIATED,
        settled_at=None,
        is_discrepant=False,
        discrepancy_reason=None,
    )
    defaults.update(overrides)
    return Transaction(**defaults)


class TestFirstEventForATransaction:
    def test_initiated_first_creates_clean_state(self):
        applied, state = _apply_event(None, make_event(EventType.PAYMENT_INITIATED, T0))
        assert applied is True
        assert state["payment_status"] == PaymentStatus.INITIATED
        assert state["settlement_status"] == SettlementStatus.UNSETTLED
        assert state["is_discrepant"] is False

    def test_non_initiated_first_flags_initiated_missing(self):
        applied, state = _apply_event(None, make_event(EventType.PAYMENT_PROCESSED, T0))
        assert applied is True
        assert state["payment_status"] == PaymentStatus.PROCESSED
        assert state["is_discrepant"] is True
        assert state["discrepancy_reason"] == "initiated_missing"


class TestInvalidTransitions:
    def test_processed_after_failed_is_conflicting_and_not_applied(self):
        prior = make_prior(payment_status=PaymentStatus.FAILED, last_event_type=EventType.PAYMENT_FAILED)
        applied, state = _apply_event(prior, make_event(EventType.PAYMENT_PROCESSED, T0 + timedelta(minutes=5)))
        assert applied is False
        assert state["payment_status"] == PaymentStatus.FAILED  # unchanged
        assert state["is_discrepant"] is True
        assert state["discrepancy_reason"] == "conflicting_transitions"

    def test_failed_after_processed_is_conflicting_and_not_applied(self):
        prior = make_prior(payment_status=PaymentStatus.PROCESSED, last_event_type=EventType.PAYMENT_PROCESSED)
        applied, state = _apply_event(prior, make_event(EventType.PAYMENT_FAILED, T0 + timedelta(minutes=5)))
        assert applied is False
        assert state["payment_status"] == PaymentStatus.PROCESSED  # unchanged
        assert state["discrepancy_reason"] == "conflicting_transitions"

    def test_settled_after_failure_applies_but_flags_discrepancy(self):
        prior = make_prior(payment_status=PaymentStatus.FAILED, last_event_type=EventType.PAYMENT_FAILED)
        applied, state = _apply_event(prior, make_event(EventType.SETTLED, T0 + timedelta(minutes=5)))
        assert applied is True  # settlement_status genuinely changes
        assert state["settlement_status"] == SettlementStatus.SETTLED
        assert state["is_discrepant"] is True
        assert state["discrepancy_reason"] == "settled_after_failure"

    def test_settled_before_processed_applies_but_flags_discrepancy(self):
        prior = make_prior(payment_status=PaymentStatus.INITIATED, last_event_type=EventType.PAYMENT_INITIATED)
        applied, state = _apply_event(prior, make_event(EventType.SETTLED, T0 + timedelta(minutes=5)))
        assert applied is True
        assert state["is_discrepant"] is True
        assert state["discrepancy_reason"] == "settled_before_processed"

    def test_late_out_of_order_event_does_not_rewrite_history(self):
        prior = make_prior(
            payment_status=PaymentStatus.PROCESSED,
            last_event_at=T0,
            last_event_type=EventType.PAYMENT_PROCESSED,
        )
        stale_event = make_event(EventType.PAYMENT_INITIATED, T0 - timedelta(hours=1))
        applied, state = _apply_event(prior, stale_event)
        assert applied is False
        assert state["payment_status"] == PaymentStatus.PROCESSED
        assert state["last_event_at"] == T0
        assert state["is_discrepant"] is False  # staleness alone isn't a data problem

    def test_initiated_resend_after_processed_is_noop(self):
        prior = make_prior(payment_status=PaymentStatus.PROCESSED, last_event_type=EventType.PAYMENT_PROCESSED)
        applied, state = _apply_event(prior, make_event(EventType.PAYMENT_INITIATED, T0 + timedelta(minutes=1)))
        assert applied is False
        assert state["payment_status"] == PaymentStatus.PROCESSED

    def test_duplicate_terminal_event_is_noop_not_discrepant(self):
        prior = make_prior(payment_status=PaymentStatus.PROCESSED, last_event_type=EventType.PAYMENT_PROCESSED)
        applied, state = _apply_event(prior, make_event(EventType.PAYMENT_PROCESSED, T0 + timedelta(minutes=5)))
        assert applied is False
        assert state["is_discrepant"] is False  # same outcome twice is harmless, not conflicting

    def test_duplicate_settled_event_is_noop(self):
        prior = make_prior(
            payment_status=PaymentStatus.PROCESSED,
            settlement_status=SettlementStatus.SETTLED,
            last_event_type=EventType.SETTLED,
            settled_at=T0,
        )
        applied, state = _apply_event(prior, make_event(EventType.SETTLED, T0 + timedelta(minutes=5)))
        assert applied is False


class TestValidTransitions:
    def test_initiated_to_processed(self):
        prior = make_prior(payment_status=PaymentStatus.INITIATED, last_event_type=EventType.PAYMENT_INITIATED)
        applied, state = _apply_event(prior, make_event(EventType.PAYMENT_PROCESSED, T0 + timedelta(minutes=5)))
        assert applied is True
        assert state["payment_status"] == PaymentStatus.PROCESSED
        assert state["is_discrepant"] is False

    def test_initiated_to_failed(self):
        prior = make_prior(payment_status=PaymentStatus.INITIATED, last_event_type=EventType.PAYMENT_INITIATED)
        applied, state = _apply_event(prior, make_event(EventType.PAYMENT_FAILED, T0 + timedelta(minutes=5)))
        assert applied is True
        assert state["payment_status"] == PaymentStatus.FAILED
        assert state["is_discrepant"] is False

    def test_processed_to_settled(self):
        prior = make_prior(payment_status=PaymentStatus.PROCESSED, last_event_type=EventType.PAYMENT_PROCESSED)
        applied, state = _apply_event(prior, make_event(EventType.SETTLED, T0 + timedelta(minutes=5)))
        assert applied is True
        assert state["settlement_status"] == SettlementStatus.SETTLED
        assert state["is_discrepant"] is False
