"""Stripe **test-mode** payment gate (Agent Payment Protocol, seller role).

Behaviour is mirrored from ``shipping-label-agent/payment.py`` per the hard
product requirement that this gate behave exactly like that reference:

* The gate uses the native ``uagents_core.contrib.protocols.payment`` protocol,
  **not** a hand-rolled card. Sending a bare ``RequestPayment`` (with no text in
  the same handler call) is what makes ASI:One render its own native "Pay with
  Stripe / Reject" sheet and drive the embedded Stripe checkout inline. ASI:One
  sends ``CommitPayment`` back on its own once the user pays; "paid"/"done" text
  is kept only as a manual re-check fallback.
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


def create_checkout_session(sender: str, chat_session_id: str) -> dict[str, Any]:
    """Create an **embedded** Stripe Checkout session for the one-time unlock.

    ``ui_mode="embedded_page"`` is what ASI:One's native payment card renderer
    expects (mirrored from shipping-label-agent): it uses ``client_secret`` +
    ``publishable_key`` to mount the Stripe form in-place when the user taps
    "Pay with Stripe", rather than redirecting to a hosted checkout page.
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
    return {
        "client_secret": getattr(session, "client_secret", "") or "",
        "id": session.id,
        "checkout_session_id": session.id,
        "publishable_key": c["publishable_key"],
        "currency": c["currency"],
        "amount_cents": str(c["amount_cents"]),
        "ui_mode": "embedded_page",
    }


def verify_paid(checkout_session_id: str) -> bool:
    """Return True if the Stripe checkout session is fully paid."""
    if not checkout_session_id:
        return False
    try:
        session = _stripe().checkout.Session.retrieve(checkout_session_id)
        return getattr(session, "payment_status", None) == "paid"
    except Exception:
        return False


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


async def request_payment(ctx: Context, sender: str, state_data: dict[str, Any]) -> None:
    """Create a fresh Stripe checkout, store it, and send a bare ``RequestPayment``.

    Sends ONLY ``RequestPayment`` - no text before/after in this call - so ASI:One
    renders the native payment sheet from this message alone. A stale
    ``client_secret`` is never reused: every call mints a new Checkout Session.
    """
    checkout = await asyncio.to_thread(create_checkout_session, sender, str(ctx.session))

    state_data["stage"] = AWAITING_PAYMENT
    state_data["stripe_session_id"] = checkout["checkout_session_id"]
    save_state(ctx, sender, state_data)

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


async def confirm_payment_via_text(ctx: Context, sender: str) -> bool:
    """Re-verify the stored checkout when the user types 'paid'/'done' (fallback)."""
    state_data = get_state(ctx, sender)
    checkout_id = state_data.get("stripe_session_id")
    if not checkout_id:
        return False
    paid = await asyncio.to_thread(_verify_with_retries, str(checkout_id))
    if not paid:
        return False
    await _grant_access(ctx, sender, state_data)
    return True


async def _grant_access(ctx: Context, sender: str, state_data: dict[str, Any]) -> None:
    """Mark the session paid and immediately hand off to Stage 2 intake."""
    if state_data.get("paid"):
        # Idempotency: never re-grant / re-send intake on a duplicate confirm.
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

    checkout_id = str(state_data.get("stripe_session_id") or "")
    paid = await asyncio.to_thread(_verify_with_retries, checkout_id) if checkout_id else False
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
    state_data = get_state(ctx, sender)
    state_data["stage"] = AWAITING_PAYMENT
    state_data["paid"] = False
    save_state(ctx, sender, state_data)

    from cards import send_text

    await send_text(
        ctx, sender, "Payment cancelled. Send any message when you're ready to try again."
    )
