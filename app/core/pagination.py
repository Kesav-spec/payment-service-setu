"""Opaque cursor encoding for keyset pagination.

This is encoding, not a signature: base64 hides the raw (sort_value, id)
pair from casual inspection but a client can still decode and hand-craft a
cursor. That's an accepted simplification here -- id is already visible on
every response row, and a forged cursor can only reposition within the same
filtered result set a request is already authorized to see, never reach data
a filter wouldn't otherwise return. HMAC-signing the token would be the
hardening step if that stopped being true.

sort_value is encoded as a plain str(...) regardless of its underlying type
(datetime or Decimal, depending on which column GET /transactions is sorted
by) -- this module doesn't know or care which; the caller re-parses the
decoded string into the right type for whichever column is active.
"""

from __future__ import annotations

import base64
import uuid
from datetime import datetime
from decimal import Decimal

_SEPARATOR = "|"


def encode_cursor(sort_value: datetime | Decimal, id: uuid.UUID) -> str:
    value_str = sort_value.isoformat() if isinstance(sort_value, datetime) else str(sort_value)
    raw = f"{value_str}{_SEPARATOR}{id}"
    return base64.urlsafe_b64encode(raw.encode()).decode()


def decode_cursor(token: str) -> tuple[str, uuid.UUID]:
    try:
        raw = base64.urlsafe_b64decode(token.encode()).decode()
        value_str, id_str = raw.split(_SEPARATOR)
        return value_str, uuid.UUID(id_str)
    except Exception as exc:
        raise ValueError("invalid cursor") from exc
