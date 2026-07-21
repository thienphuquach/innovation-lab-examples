"""Per-sender session state, persisted in ``ctx.storage``.

This is the single source of truth for "where is this user in the flow". It is
shared by the payment protocol handlers (:mod:`payment`) and the chat state
machine (:mod:`chat_proto`); keeping it in its own module avoids a circular
import between the two.

State is stored as a JSON string keyed by ``sender``. The schema mirrors the
project brief's session-state schema, plus a couple of internal payment keys.
``message_history`` holds a Pydantic AI ``ModelMessagesTypeAdapter.dump_json``
string (NOT a plain ``json.dumps`` of the message objects - see
``research-notes.md`` §5), so it round-trips through this JSON blob as an opaque
string.
"""

from __future__ import annotations

import json
from typing import Any

from uagents import Context

# ── Stage constants (the "stage" field) ──────────────────────────────────────
UNINITIALIZED = "uninitialized"  # brand-new sender, nothing sent yet
AWAITING_PAYMENT = "awaiting_payment"  # RequestPayment sent, waiting on Stripe
INTAKE = "intake"  # Stage 2 - collecting the trip request
SHOWING_ROUTES = "showing_routes"  # Stage 3 - carousel of itineraries shown
SHOWING_DETAIL = "showing_detail"  # Stage 4 - one itinerary + fare detail shown
AWAITING_CONFIRM = "awaiting_confirm"  # Stage 5 - review card, deferred-tool gate
DONE = "done"  # Stage 6 - finished; next message re-enters intake

_SESSION_KEY = "session:{}"
_WINDOW_KEY = "window:{}"


def default_state() -> dict[str, Any]:
    """A fresh, unpaid session record."""
    return {
        "paid": False,
        "paid_at": None,  # ISO timestamp once paid
        "stage": UNINITIALIZED,
        "message_history": None,  # ModelMessagesTypeAdapter.dump_json string
        "trip": None,  # normalized TripRequest dict (Stage 2), fully geocoded
        "pending_trip": None,  # trip mid-geocoding (Stage 2.5), coords being resolved
        "last_itineraries": None,  # cached Transitland plan (Stage 3), enables Back
        "selected_route_id": None,
        "selected_fare_option": None,
        "pending_approval": None,  # deferred-tool handle (Stage 5)
        # ── internal payment bookkeeping ──
        "stripe_session_id": None,
    }


def get_state(ctx: Context, sender: str) -> dict[str, Any]:
    raw = ctx.storage.get(_SESSION_KEY.format(sender))
    if not raw:
        return default_state()
    try:
        data: dict[str, Any] = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return default_state()
    # Backfill any keys added since this record was written.
    base = default_state()
    base.update(data)
    return base


def save_state(ctx: Context, sender: str, data: dict[str, Any]) -> None:
    ctx.storage.set(_SESSION_KEY.format(sender), json.dumps(data))


def clear_trip_state(state: dict[str, Any]) -> None:
    """Wipe trip-specific keys while preserving ``paid``/``message_history``.

    Used by the ``override`` interrupt (a user restating a different trip mid-flow)
    so we return to intake with the new values without forcing a re-payment.
    """
    state["trip"] = None
    state["pending_trip"] = None
    state["last_itineraries"] = None
    state["selected_route_id"] = None
    state["selected_fare_option"] = None
    state["pending_approval"] = None


def check_new_window_and_reset(ctx: Context, sender: str) -> None:
    """Force a full reset when a message arrives on a new chat window.

    ``ctx.storage`` is keyed by ``sender``, and ASI:One reuses the same
    ``sender`` across a user's separate chat conversations, while ``ctx.session``
    changes per window. The product decision for this agent is a per-chat-window
    unlock: each new conversation must pay again. Comparing the stored window id
    against the current ``ctx.session`` is what enforces that - without it a new
    chat would silently resume an already-paid session. Mirrors the identical fix
    in ``shipping-label-agent``/``quiz-agent``.
    """
    current_window = str(ctx.session)
    stored_window = ctx.storage.get(_WINDOW_KEY.format(sender))
    if stored_window and stored_window != current_window:
        save_state(ctx, sender, default_state())
    ctx.storage.set(_WINDOW_KEY.format(sender), current_window)
