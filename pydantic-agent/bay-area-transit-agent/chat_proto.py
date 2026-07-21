"""Agent Chat Protocol handlers - the session/stage dispatcher.

Every inbound ``ChatMessage`` is acknowledged first, then run through:

1. a per-window reset (new chat conversation => pay again, per product decision),
2. the payment gate (Stage 0): any message from an unpaid sender gets the identical
   Stripe gate and nothing else,
3. once paid, the stage dispatcher (Stages 2-6) - filled in stage by stage.

The interrupt classifier (cross-cutting) will sit in front of the paid-stage
dispatch once the conversational layer lands; for now the paid path routes into
Stage 2 intake.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from uagents import Context, Protocol
from uagents_core.contrib.protocols.chat import (
    ChatAcknowledgement,
    ChatMessage,
    StartSessionContent,
    TextContent,
    chat_protocol_spec,
)

from cards import extract_text, intake_form_card, send_card, send_text
from payment import confirm_payment_via_text, request_payment
from session_state import (
    INTAKE,
    check_new_window_and_reset,
    get_state,
    save_state,
)

chat_proto = Protocol(spec=chat_protocol_spec)

# Text a user might type to force a manual payment re-check (fallback path only;
# normally ASI:One sends CommitPayment automatically once Stripe settles).
_PAID_WORDS = {"paid", "done", "i paid", "paid!", "finished", "complete", "completed"}


# ── Stage 2 entry (called from here and from payment._grant_access) ───────────
async def start_intake(
    ctx: Context, sender: str, state: dict[str, Any], *, welcome: bool = False
) -> None:
    """Move the session into Stage 2 and send the trip-intake form."""
    state["stage"] = INTAKE
    save_state(ctx, sender, state)
    narration = (
        "Payment confirmed - you're unlocked! Where are you headed? Fill in the trip "
        "form, or just tell me in your own words (e.g. \"Berkeley to the Mission around 6pm\")."
        if welcome
        else "Let's plan a trip. Fill in the form, or just describe it in a sentence."
    )
    await send_card(ctx, sender, narration, intake_form_card())


# ── Stage 0 - payment gate ───────────────────────────────────────────────────
async def _handle_unpaid(ctx: Context, sender: str, text: str, state: dict[str, Any]) -> None:
    """Gate every unpaid message. No functionality is reachable before payment."""
    if text.lower().strip() in _PAID_WORDS:
        if await confirm_payment_via_text(ctx, sender):
            return
        await send_text(
            ctx,
            sender,
            "I don't see a completed payment yet. Please finish the Stripe checkout above.",
        )
        return
    # Any other message => (re)issue a fresh Stripe checkout gate.
    await request_payment(ctx, sender, state)


# ── Paid-stage dispatch (Stages 2-6) ─────────────────────────────────────────
async def _dispatch_paid(ctx: Context, sender: str, text: str, state: dict[str, Any]) -> None:
    """Route a paid user's message by current stage.

    Stages 2.5-6 are added in their own commits; until then a paid user is kept in
    the Stage 2 intake loop so the gate remains demonstrably end-to-end.
    """
    # TODO(stage2+): interrupt classifier, then per-stage handling.
    await start_intake(ctx, sender, state)


# ── Protocol handlers ────────────────────────────────────────────────────────
@chat_proto.on_message(ChatMessage)
async def handle_message(ctx: Context, sender: str, msg: ChatMessage) -> None:
    await ctx.send(
        sender,
        ChatAcknowledgement(
            timestamp=datetime.now(timezone.utc), acknowledged_msg_id=msg.msg_id
        ),
    )

    check_new_window_and_reset(ctx, sender)
    state = get_state(ctx, sender)

    # A brand-new conversation (StartSession) from an unpaid sender => gate.
    if any(isinstance(c, StartSessionContent) for c in msg.content) and not state["paid"]:
        await request_payment(ctx, sender, state)
        return

    has_text = any(isinstance(c, TextContent) for c in msg.content)
    if not has_text:
        return

    text = extract_text(msg)
    if not text:
        return

    if not state["paid"]:
        await _handle_unpaid(ctx, sender, text, state)
    else:
        await _dispatch_paid(ctx, sender, text, state)


@chat_proto.on_message(ChatAcknowledgement)
async def handle_ack(ctx: Context, sender: str, msg: ChatAcknowledgement) -> None:
    ctx.logger.debug(f"[chat] ack from {sender} for {msg.acknowledged_msg_id}")
