"""Stripe test-mode gating for the whole agent, on the uAgent payment protocol.

Structurally this is ``shipping-label-agent/payment.py``: the same embedded
checkout call, the same bare ``RequestPayment`` message, and the same
verify-then-``CompletePayment`` handshake.

What differs, and why:

* Access is gated once, upfront, rather than metered per feature. The very
  first message of a session triggers this charge (see ``conversation.Paywall``);
  once it clears, everything - price checks, drug info, price trend history,
  brand-vs-generic - is free for the rest of the session. There is no deferred
  Pydantic AI tool to resume here: the payment outcome just flips
  ``state.stripe_paid`` and either lets the conversation through or hard-stops it.
* A rejected payment does not reset any conversation progress - there is none
  yet, since the paywall runs before anything else - it just re-arms the
  paywall so the next message asks again.
* A background poller (:func:`_poll_until_paid`), started the moment the charge
  is requested, confirms the checkout directly against Stripe instead of relying
  solely on ASI:One relaying ``CommitPayment`` back to the seller. Confirmed
  live: that platform handshake did not fire even for a fully paid test-mode
  checkout, so waiting on it alone left the agent locked until the user said
  "paid" themselves. The poller removes that requirement; the manual
  ``confirm_payment_via_text`` path stays as a zero-wait option on top of it.
"""

from __future__ import annotations

import asyncio
import os
import time
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

from session_state import SessionState, get_state, save_state

STRIPE_TEST_SECRET_PREFIX = "sk_test_"
STRIPE_TEST_PUBLISHABLE_PREFIX = "pk_test_"

payment_proto = Protocol(spec=payment_protocol_spec, role="seller")


def config() -> dict[str, Any]:
    return {
        "secret_key": (os.getenv("STRIPE_SECRET_KEY") or "").strip(),
        "publishable_key": (os.getenv("STRIPE_PUBLISHABLE_KEY") or "").strip(),
        "amount_cents": int(os.getenv("STRIPE_AMOUNT_CENTS", "200")),
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
            f"STRIPE_SECRET_KEY must be a test key starting with '{STRIPE_TEST_SECRET_PREFIX}'. "
            "This example is test-mode-only and will not run with a live key."
        )
    publishable = c["publishable_key"]
    if publishable and not publishable.startswith(STRIPE_TEST_PUBLISHABLE_PREFIX):
        raise RuntimeError(
            f"STRIPE_PUBLISHABLE_KEY must be a test key starting with "
            f"'{STRIPE_TEST_PUBLISHABLE_PREFIX}'."
        )


def _stripe() -> Any:
    import stripe as _s

    _s.api_key = config()["secret_key"]
    return _s


def _expires_at() -> int:
    seconds = int(os.getenv("STRIPE_CHECKOUT_EXPIRES_SECONDS", "1800"))
    return int(time.time()) + max(1800, min(24 * 3600, seconds))


def create_checkout_session(
    sender: str, chat_session_id: str, amount_cents: int, description: str
) -> dict[str, Any]:
    """Create an embedded Stripe Checkout session.

    ``ui_mode="embedded_page"`` is what ASI:One's native payment card expects: it
    mounts the Stripe form in place from ``client_secret`` + ``publishable_key``
    rather than bouncing the user to a hosted page.
    """
    c = config()
    session = _stripe().checkout.Session.create(
        ui_mode="embedded_page",
        redirect_on_completion="if_required",
        payment_method_types=["card"],
        mode="payment",
        return_url=(
            f"{c['success_url']}?session_id={{CHECKOUT_SESSION_ID}}"
            f"&chat_session_id={chat_session_id}&user={sender}"
        ),
        expires_at=_expires_at(),
        line_items=[
            {
                "price_data": {
                    "currency": c["currency"],
                    "product_data": {"name": f"Pill Price Agent - {description}"},
                    "unit_amount": amount_cents,
                },
                "quantity": 1,
            }
        ],
        metadata={
            "user_address": sender,
            "session_id": chat_session_id,
            "service": "pill_price",
        },
    )
    return {
        "client_secret": getattr(session, "client_secret", "") or "",
        "id": session.id,
        "checkout_session_id": session.id,
        "publishable_key": c["publishable_key"],
        "currency": c["currency"],
        "amount_cents": str(amount_cents),
        "ui_mode": "embedded_page",
    }


def verify_paid(checkout_session_id: str) -> bool:
    """True only when Stripe reports the checkout session as fully paid."""
    if not checkout_session_id:
        return False
    try:
        session = _stripe().checkout.Session.retrieve(checkout_session_id)
        return getattr(session, "payment_status", None) == "paid"
    except Exception:  # noqa: BLE001 - any lookup failure means "not verified as paid"
        return False


async def request_payment(
    ctx: Context, sender: str, state: SessionState, *, amount_cents: int, description: str
) -> None:
    """Create the checkout and send a bare ``RequestPayment``.

    Nothing else may be sent from this handler call. ASI:One renders its own
    "Pay with Stripe / Reject" card from this message alone, and any text sent
    alongside it causes the payment card to be swallowed in favour of the text.
    """
    checkout = await asyncio.to_thread(
        create_checkout_session, sender, str(ctx.session), amount_cents, description
    )
    state.stripe_session_id = checkout["checkout_session_id"]
    save_state(ctx, sender, state)

    amount = f"{amount_cents / 100:.2f}"
    await ctx.send(
        sender,
        RequestPayment(
            accepted_funds=[Funds(currency="USD", amount=amount, payment_method="stripe")],
            recipient=str(ctx.agent.address),
            deadline_seconds=int(os.getenv("STRIPE_CHECKOUT_EXPIRES_SECONDS", "1800")),
            reference=str(ctx.session),
            description=f"${amount} - {description}",
            metadata={"stripe": checkout, "service": "pill_price"},
        ),
    )
    ctx.logger.info(
        f"[payment] RequestPayment -> {sender} | {description} | "
        f"checkout={checkout['checkout_session_id']} | ${amount}"
    )

    asyncio.create_task(_poll_until_paid(ctx, sender, checkout["checkout_session_id"]))


async def _poll_until_paid(ctx: Context, sender: str, checkout_id: str) -> None:
    """Watch Stripe directly so the paid feature runs with no action from the user.

    Confirmed live: ASI:One's payment card does not reliably send ``CommitPayment``
    back to the seller once the embedded checkout completes - a fully paid
    test-mode checkout produced no ``CommitPayment`` at all, and the only way to
    resume was the user typing "paid" to trigger :func:`confirm_payment_via_text`
    manually. That handshake is owned by the ASI:One platform, not this agent, so
    rather than wait on it, this polls the same Stripe endpoint that text fallback
    uses. The moment Stripe reports the charge as paid, the agent delivers the
    result on its own.

    Kept alongside, not instead of, ``on_commit``/``on_reject`` and the manual
    "paid" fallback: if the platform's own confirmation ever does arrive, or the
    user types "paid" first, this loop notices via the ``stripe_session_id``
    check below and exits without double-delivering.
    """
    interval = float(os.getenv("STRIPE_POLL_INTERVAL_SECONDS", "4"))
    timeout = int(os.getenv("STRIPE_CHECKOUT_EXPIRES_SECONDS", "1800"))
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        await asyncio.sleep(interval)
        if get_state(ctx, sender).stripe_session_id != checkout_id:
            return  # resolved another way already: rejected, "paid" typed, or superseded
        try:
            paid = await asyncio.to_thread(verify_paid, checkout_id)
        except Exception as exc:  # noqa: BLE001 - a flaky Stripe call should not kill the poller
            ctx.logger.debug(f"[payment] poll check failed, will retry: {exc}")
            continue
        if not paid:
            continue
        # Re-read after the Stripe round trip: a manual "paid" may have resolved
        # this same checkout while the verification call was in flight.
        state = get_state(ctx, sender)
        if state.stripe_session_id != checkout_id:
            return
        ctx.logger.info(f"[payment] Poller confirmed paid checkout {checkout_id} for {sender}")
        state.stripe_paid = True
        from chat_proto import deliver_access_payment

        await deliver_access_payment(ctx, sender, state, approved=True)
        return


async def confirm_payment_via_text(ctx: Context, sender: str) -> bool:
    """Re-verify the stored checkout when the user types 'paid' or 'done'.

    The background poller started in :func:`request_payment` normally beats this
    to it; this stays as the immediate, zero-wait path for anyone who types
    "paid" before the next poll tick.
    """
    state = get_state(ctx, sender)
    if not state.stripe_session_id:
        return False
    if not await asyncio.to_thread(verify_paid, state.stripe_session_id):
        return False
    from chat_proto import deliver_access_payment

    await deliver_access_payment(ctx, sender, state, approved=True)
    return True


@payment_proto.on_message(CommitPayment)
async def on_commit(ctx: Context, sender: str, msg: CommitPayment) -> None:
    """Verify with Stripe, complete the payment, then unlock the agent."""
    ctx.logger.info(f"[payment] CommitPayment from {sender} | txn={msg.transaction_id}")
    state = get_state(ctx, sender)
    checkout_id = state.stripe_session_id or ""

    paid = await asyncio.to_thread(verify_paid, checkout_id) if checkout_id else False
    if not paid:
        ctx.logger.error(f"[payment] Stripe verification FAILED: {checkout_id}")
        await ctx.send(
            sender,
            RejectPayment(reason="Stripe payment not confirmed yet. Please finish checkout."),
        )
        from chat_proto import deliver_access_payment

        await deliver_access_payment(ctx, sender, state, approved=False)
        return

    await ctx.send(sender, CompletePayment(transaction_id=msg.transaction_id))
    ctx.logger.info(f"[payment] Verified | sender={sender} | checkout={checkout_id}")
    state.stripe_paid = True

    from chat_proto import deliver_access_payment

    await deliver_access_payment(ctx, sender, state, approved=True)


@payment_proto.on_message(RejectPayment)
async def on_reject(ctx: Context, sender: str, msg: RejectPayment) -> None:
    """The buyer cancelled - hard-stop and re-arm the paywall for the next message."""
    ctx.logger.info(f"[payment] Rejected by {sender}: {msg.reason}")
    state = get_state(ctx, sender)

    from chat_proto import deliver_access_payment

    await deliver_access_payment(ctx, sender, state, approved=False)
