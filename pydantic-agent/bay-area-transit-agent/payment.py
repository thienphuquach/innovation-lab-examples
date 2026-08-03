"""Stripe **test-mode** payment gate (Agent Payment Protocol, seller role).

Behaviour is mirrored from ``shipping-label-agent/payment.py`` per the hard
product requirement that this gate behave exactly like that reference:

* The gate uses the native ``uagents_core.contrib.protocols.payment`` protocol,
  **not** a hand-rolled card. Sending a bare ``RequestPayment`` (with no text in
  the same handler call) is what makes ASI:One render its own native "Pay with
  Stripe / Reject" sheet and drive the embedded Stripe checkout inline. ASI:One
  does **not** reliably send ``CommitPayment`` after that sheet's "Confirm
  Payment", so the unlock does not depend on one: :func:`poll_pending` watches
  Stripe directly, and :func:`settle_or_request_payment` re-checks on every
  gated message as a backstop.
* Everything is test-mode only: :func:`assert_stripe_test_keys` refuses to run
  unless the secret key starts with ``sk_test_`` (and the publishable key, if
  set, with ``pk_test_``).

The one difference from the shipping-label reference: this agent charges **once
per chat session** (a single unlock fee), so there is only the ``"gate"`` purpose
- no second per-item charge.
"""

from __future__ import annotations

import asyncio
import os
import time
import uuid
from datetime import datetime, timezone
from typing import Any

from uagents import Context, Protocol
from uagents_core.contrib.protocols.payment import (
    CommitPayment,
    CompletePayment,
    Funds,
    RejectPayment,
    RequestPayment,
    payment_protocol_spec,
)

from session_state import AWAITING_PAYMENT, get_state, save_state

STRIPE_TEST_SECRET_PREFIX = "sk_test_"
STRIPE_TEST_PUBLISHABLE_PREFIX = "pk_test_"

# Stripe's documented test cards (https://docs.stripe.com/testing).
TEST_SUCCESS_CARD = "4242424242424242"
TEST_DECLINE_CARD = "4000000000000002"  # generic card_declined

payment_proto = Protocol(spec=payment_protocol_spec, role="seller")


def config() -> dict[str, Any]:
    """Read Stripe config from the environment (test defaults)."""
    return {
        "secret_key": (os.getenv("STRIPE_SECRET_KEY") or "").strip(),
        "publishable_key": (os.getenv("STRIPE_PUBLISHABLE_KEY") or "").strip(),
        "amount_cents": int(os.getenv("STRIPE_AMOUNT_CENTS", "100")),  # $1.00 default
        "currency": (os.getenv("STRIPE_CURRENCY", "usd") or "usd").lower(),
        "success_url": (
            os.getenv("STRIPE_SUCCESS_URL", "https://agentverse.ai") or "https://agentverse.ai"
        ).rstrip("/"),
    }


def assert_stripe_test_keys() -> None:
    """Fail loudly unless the configured Stripe keys are test keys."""
    c = config()
    if not c["secret_key"].startswith(STRIPE_TEST_SECRET_PREFIX):
        raise RuntimeError(
            "STRIPE_SECRET_KEY must be a test key starting with "
            f"'{STRIPE_TEST_SECRET_PREFIX}'. This agent is test-mode-only."
        )
    pub = c["publishable_key"]
    if pub and not pub.startswith(STRIPE_TEST_PUBLISHABLE_PREFIX):
        raise RuntimeError(
            "STRIPE_PUBLISHABLE_KEY must be a test key starting with "
            f"'{STRIPE_TEST_PUBLISHABLE_PREFIX}'."
        )


def _stripe() -> Any:
    """Return the configured Stripe SDK module (indirection eases testing)."""
    import stripe as _s

    _s.api_key = config()["secret_key"]
    return _s


def _expires_at() -> int:
    """Checkout expiry, clamped to Stripe's 30 min - 24 h window."""
    sec = int(os.getenv("STRIPE_CHECKOUT_EXPIRES_SECONDS", "1800"))
    return int(time.time()) + max(1800, min(24 * 3600, sec))


def amount_str(amount_cents: int | None = None) -> str:
    cents = config()["amount_cents"] if amount_cents is None else amount_cents
    return f"{cents / 100:.2f}"


def _checkout_payload(session: Any) -> dict[str, str]:
    """Shape a Stripe Session into the ``metadata["stripe"]`` ASI:One expects.

    Field names and values mirror ``shipping-label-agent/payment.py`` exactly,
    including ``ui_mode="embedded_page"``.
    """
    c = config()
    return {
        "client_secret": getattr(session, "client_secret", "") or "",
        "id": session.id,
        "checkout_session_id": session.id,
        "publishable_key": c["publishable_key"],
        "currency": c["currency"],
        "amount_cents": str(c["amount_cents"]),
        "ui_mode": "embedded_page",
    }


def create_checkout_session(sender: str, chat_session_id: str) -> dict[str, Any]:
    """Create an **embedded** Stripe Checkout session for the one-time unlock.

    ``ui_mode="embedded_page"`` gives back a ``client_secret`` (and no hosted
    ``url``), which ASI:One mounts in-place when the user taps "Pay with Stripe"
    rather than redirecting them out to a Stripe-hosted page.
    """
    c = config()
    s = _stripe()
    return_url = (
        f"{c['success_url']}?session_id={{CHECKOUT_SESSION_ID}}"
        f"&chat_session_id={chat_session_id}&user={sender}"
    )
    session = s.checkout.Session.create(
        ui_mode="embedded_page",
        redirect_on_completion="if_required",
        payment_method_types=["card"],
        mode="payment",
        return_url=return_url,
        expires_at=_expires_at(),
        line_items=[
            {
                "price_data": {
                    "currency": c["currency"],
                    "product_data": {"name": "Bay Area Transit & Fare Concierge - unlock"},
                    "unit_amount": c["amount_cents"],
                },
                "quantity": 1,
            }
        ],
        metadata={
            "user_address": sender,
            "session_id": chat_session_id,
            "service": "bay_area_transit",
        },
    )
    return _checkout_payload(session)


def retrieve_checkout(checkout_session_id: str) -> Any | None:
    """Fetch a checkout session, or None if it is unknown/unreachable."""
    if not checkout_session_id:
        return None
    try:
        return _stripe().checkout.Session.retrieve(checkout_session_id)
    except Exception:
        return None


def verify_paid(checkout_session_id: str) -> bool:
    """Return True if the Stripe checkout session is fully paid."""
    session = retrieve_checkout(checkout_session_id)
    return session is not None and getattr(session, "payment_status", None) == "paid"


def _verify_with_retries(checkout_id: str, *, attempts: int = 3, delay: float = 1.0) -> bool:
    """Verify payment, retrying a few times.

    The UI can send ``CommitPayment`` a beat before Stripe's own status has
    settled to ``paid``, so a single check can spuriously fail (brief Stage 1).
    """
    for i in range(attempts):
        if verify_paid(checkout_id):
            return True
        if i < attempts - 1:
            time.sleep(delay)
    return False


# ── Watch list: checkouts we are waiting on ──────────────────────────────────
# Tapping "Confirm Payment" does not reliably produce a ``CommitPayment``, so a
# sender can be paid on Stripe while the agent still believes they are gated.
# Every outstanding checkout is parked here and re-read by :func:`poll_pending`
# until Stripe settles it, which is what lets the trip form arrive on its own
# rather than only on the user's next message.
_PENDING_KEY = "pending_checkouts"
_POLL_GRACE_SECONDS = int(os.getenv("STRIPE_POLL_GRACE_SECONDS", "600"))

# One lock per sender so the chat handler and the Stripe poller cannot interleave
# read-modify-writes of ``session:{sender}``. Without it, a poller unlock could
# be overwritten by a still-running ``request_payment`` and the trip form would
# race ahead of (or land beside) the payment card.
_sender_locks: dict[str, asyncio.Lock] = {}


def _lock_for(sender: str) -> asyncio.Lock:
    lock = _sender_locks.get(sender)
    if lock is None:
        lock = asyncio.Lock()
        _sender_locks[sender] = lock
    return lock


def _pending(ctx: Context) -> dict[str, Any]:
    raw = ctx.storage.get(_PENDING_KEY)
    return raw if isinstance(raw, dict) else {}


def _watch(ctx: Context, sender: str, checkout_id: str) -> None:
    pending = _pending(ctx)
    # The chat session is recorded because an interval tick gets a brand-new
    # random one, and ASI:One routes chat messages by session - a reply stamped
    # with the wrong session never reaches the user's conversation.
    pending[sender] = {
        "id": checkout_id,
        "since": time.time(),
        "chat_session": str(ctx.session),
    }
    ctx.storage.set(_PENDING_KEY, pending)


def _unwatch(ctx: Context, sender: str) -> None:
    pending = _pending(ctx)
    if pending.pop(sender, None) is not None:
        ctx.storage.set(_PENDING_KEY, pending)


async def clear_watch(ctx: Context, sender: str) -> None:
    """Drop any outstanding checkout watch for ``sender``.

    Called when a new chat window starts so a previous window's paid checkout
    cannot unlock this one for free (and cannot push the trip form ahead of the
    payment card). Lock-protected like every other mutator of ``_pending``/
    ``session:{sender}`` state: :func:`poll_pending` holds ``_lock_for(sender)``
    across its own Stripe call while validating a watch, and without this lock
    a new window's clear could interleave with that in-flight cycle - the old
    watch's ``_grant_access`` can still fire (and send the trip form, addressed
    to the *old* window's chat session) at nearly the same moment this window's
    own fresh payment card goes out, which is what let both appear together.
    """
    async with _lock_for(sender):
        _unwatch(ctx, sender)


def _adopt_chat_session(ctx: Context, chat_session: Any) -> None:
    """Address the reply to the user's chat window rather than this tick's own id.

    ``Context`` exposes ``session`` read-only and the interval loop builds a
    fresh context (with a fresh ``uuid4``) every tick, so overwriting the
    private field is safe and cannot leak into another sender's turn.
    """
    try:
        ctx._session = uuid.UUID(str(chat_session))
    except (TypeError, ValueError):
        ctx.logger.warning(f"[payment] no usable chat session recorded: {chat_session!r}")


async def poll_pending(ctx: Context) -> None:
    """Unlock any sender whose *current-window* checkout has settled on Stripe.

    Driven by an ``on_interval`` in :mod:`agent`. Does nothing (and costs no
    Stripe call) while no checkout is outstanding. Refuses to unlock when the
    watched id no longer matches the sender's stored session (a new window has
    taken over).
    """
    now = time.time()
    for sender, record in list(_pending(ctx).items()):
        async with _lock_for(sender):
            # Re-read under the lock: another path may have cleared or replaced
            # this watch while we were waiting.
            live = _pending(ctx).get(sender)
            if not live or live.get("id") != record.get("id"):
                continue
            if now - float(live.get("since", now)) > _POLL_GRACE_SECONDS:
                _unwatch(ctx, sender)
                continue
            state = get_state(ctx, sender)
            watched_id = str(live.get("id") or "")
            if (
                state.get("paid")
                or state.get("stage") != AWAITING_PAYMENT
                or str(state.get("stripe_session_id") or "") != watched_id
            ):
                _unwatch(ctx, sender)
                continue
            session = await asyncio.to_thread(retrieve_checkout, watched_id)
            if session is None:
                continue
            if getattr(session, "payment_status", None) == "paid":
                ctx.logger.info(f"[payment] poll: {session.id} settled - unlocking {sender}")
                _adopt_chat_session(ctx, live.get("chat_session"))
                await _grant_access(ctx, sender, get_state(ctx, sender))
            elif getattr(session, "status", None) != "open":
                _unwatch(ctx, sender)


async def _send_request_payment(
    ctx: Context, sender: str, checkout: dict[str, Any], state_data: dict[str, Any]
) -> None:
    """Store the checkout and send a bare ``RequestPayment``.

    Caller must hold :func:`_lock_for` for ``sender``. Sends ONLY
    ``RequestPayment`` - no text before/after - so ASI:One renders the native
    payment sheet from this message alone.
    """
    state_data["stage"] = AWAITING_PAYMENT
    state_data["paid"] = False
    state_data["stripe_session_id"] = checkout["checkout_session_id"]
    save_state(ctx, sender, state_data)
    _watch(ctx, sender, checkout["checkout_session_id"])

    amount = amount_str()
    await ctx.send(
        sender,
        RequestPayment(
            accepted_funds=[Funds(currency="USD", amount=amount, payment_method="stripe")],
            recipient=str(ctx.agent.address),
            deadline_seconds=int(os.getenv("STRIPE_CHECKOUT_EXPIRES_SECONDS", "1800")),
            reference=str(ctx.session),
            description=f"Unlock the Bay Area Transit & Fare Concierge for ${amount}.",
            metadata={"stripe": checkout, "service": "bay_area_transit", "purpose": "gate"},
        ),
    )
    ctx.logger.info(
        f"[payment] RequestPayment -> {sender} | "
        f"checkout={checkout['checkout_session_id']} | ${amount}"
    )


async def request_payment(ctx: Context, sender: str, state_data: dict[str, Any]) -> None:
    """Mint a fresh Stripe checkout and gate the sender behind it."""
    async with _lock_for(sender):
        # Always re-read: a concurrent poller may already have unlocked.
        state_data = get_state(ctx, sender)
        if state_data.get("paid"):
            return
        checkout = await asyncio.to_thread(create_checkout_session, sender, str(ctx.session))
        await _send_request_payment(ctx, sender, checkout, state_data)


async def settle_or_request_payment(
    ctx: Context, sender: str, state_data: dict[str, Any]
) -> None:
    """Unlock if the stored checkout already paid; otherwise re-gate the sender.

    ASI:One only emits ``CommitPayment`` when *its own* record of the checkout
    reads complete, so a user whose money Stripe has already taken can still
    arrive here locked out. Asking Stripe directly on every gated message is the
    recovery path. Reusing a still-open session also stops each message minting
    a new one, which is what let a stuck user pay several times over.
    """
    async with _lock_for(sender):
        state_data = get_state(ctx, sender)
        if state_data.get("paid"):
            return
        session = await asyncio.to_thread(
            retrieve_checkout, str(state_data.get("stripe_session_id") or "")
        )
        if session is not None:
            if getattr(session, "payment_status", None) == "paid":
                ctx.logger.info(
                    f"[payment] {session.id} already paid on Stripe - unlocking {sender}"
                )
                await _grant_access(ctx, sender, state_data)
                return
            if getattr(session, "status", None) == "open":
                ctx.logger.info(f"[payment] reusing open checkout {session.id} for {sender}")
                await _send_request_payment(ctx, sender, _checkout_payload(session), state_data)
                return
        checkout = await asyncio.to_thread(create_checkout_session, sender, str(ctx.session))
        await _send_request_payment(ctx, sender, checkout, state_data)


async def _grant_access(ctx: Context, sender: str, state_data: dict[str, Any]) -> None:
    """Mark the session paid and immediately hand off to Stage 2 intake.

    Caller must hold :func:`_lock_for` for ``sender``.
    """
    _unwatch(ctx, sender)
    # Fresh read under the lock: another path may have already granted.
    state_data = get_state(ctx, sender)
    if state_data.get("paid"):
        return
    state_data["paid"] = True
    state_data["paid_at"] = datetime.now(timezone.utc).isoformat()
    save_state(ctx, sender, state_data)

    # Lazy import to avoid a circular import (chat_proto imports payment_proto).
    from chat_proto import start_intake

    await start_intake(ctx, sender, state_data, welcome=True)


@payment_proto.on_message(CommitPayment)
async def on_commit(ctx: Context, sender: str, msg: CommitPayment) -> None:
    """Verify the Stripe payment, complete it, and unlock the agent."""
    ctx.logger.info(f"[payment] CommitPayment from {sender} | txn={msg.transaction_id}")
    async with _lock_for(sender):
        state_data = get_state(ctx, sender)

        # Idempotency: a double-click can resend CommitPayment. If already paid,
        # ack politely and stop - never re-verify or re-grant.
        if state_data.get("paid"):
            await ctx.send(sender, CompletePayment(transaction_id=msg.transaction_id))
            return

        # Reject unsupported method / missing transaction id up front.
        if msg.funds.payment_method != "stripe" or not msg.transaction_id:
            await ctx.send(
                sender, RejectPayment(reason="Unsupported payment method (expected Stripe).")
            )
            return

        # ASI:One sends the Checkout Session id as the transaction id. Prefer it
        # over our stored copy, which a restart may have wiped.
        checkout_id = str(state_data.get("stripe_session_id") or "")
        if msg.transaction_id.startswith("cs_"):
            checkout_id = msg.transaction_id
        paid = (
            await asyncio.to_thread(_verify_with_retries, checkout_id) if checkout_id else False
        )
        if not paid:
            ctx.logger.error(f"[payment] Stripe verification FAILED: {checkout_id}")
            await ctx.send(
                sender,
                RejectPayment(reason="Stripe payment not confirmed yet. Please finish checkout."),
            )
            return

        await ctx.send(sender, CompletePayment(transaction_id=msg.transaction_id))
        ctx.logger.info(f"[payment] Verified | sender={sender} | checkout={checkout_id}")
        await _grant_access(ctx, sender, state_data)


@payment_proto.on_message(RejectPayment)
async def on_reject(ctx: Context, sender: str, msg: RejectPayment) -> None:
    """The buyer cancelled payment - remain gated, invite a retry."""
    ctx.logger.info(f"[payment] Rejected by {sender}: {msg.reason}")
    async with _lock_for(sender):
        _unwatch(ctx, sender)
        state_data = get_state(ctx, sender)
        state_data["stage"] = AWAITING_PAYMENT
        state_data["paid"] = False
        save_state(ctx, sender, state_data)

    from cards import send_text

    await send_text(
        ctx, sender, "Payment cancelled. Send any message when you're ready to try again."
    )
