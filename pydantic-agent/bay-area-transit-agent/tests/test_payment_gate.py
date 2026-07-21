"""Stage 0/1 - the Stripe payment gate and verification."""

from __future__ import annotations

import pytest
from uagents_core.contrib.protocols.chat import ChatMessage, MetadataContent
from uagents_core.contrib.protocols.payment import (
    CommitPayment,
    CompletePayment,
    Funds,
    RejectPayment,
    RequestPayment,
)

import payment
from session_state import AWAITING_PAYMENT, get_state

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _fake_stripe(monkeypatch):
    """Replace the Stripe network calls with in-memory fakes."""
    monkeypatch.setattr(
        payment,
        "create_checkout_session",
        lambda sender, chat_session_id: {
            "client_secret": "cs_test_secret",
            "id": "cs_test_123",
            "checkout_session_id": "cs_test_123",
            "publishable_key": "pk_test_x",
            "currency": "usd",
            "amount_cents": "100",
            "ui_mode": "embedded_page",
        },
    )


def _commit(txn: str = "cs_test_123", method: str = "stripe") -> CommitPayment:
    return CommitPayment(
        funds=Funds(currency="USD", amount="1.00", payment_method=method),
        recipient="agent1qtestselleraddressxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
        transaction_id=txn,
    )


async def test_request_payment_sends_bare_request_and_stores_session(ctx, sender):
    state = get_state(ctx, sender)
    await payment.request_payment(ctx, sender, state)

    reqs = ctx.messages_of(RequestPayment)
    assert len(reqs) == 1
    req = reqs[0]
    assert req.accepted_funds[0].payment_method == "stripe"
    assert req.accepted_funds[0].amount == "1.00"
    assert req.metadata["stripe"]["checkout_session_id"] == "cs_test_123"
    # Bare RequestPayment: nothing else sent alongside it.
    assert len(ctx.sent) == 1

    saved = get_state(ctx, sender)
    assert saved["stage"] == AWAITING_PAYMENT
    assert saved["stripe_session_id"] == "cs_test_123"
    assert saved["paid"] is False


async def test_commit_success_grants_access_and_sends_intake(ctx, sender, monkeypatch):
    monkeypatch.setattr(payment, "verify_paid", lambda cid: True)
    state = get_state(ctx, sender)
    await payment.request_payment(ctx, sender, state)
    ctx.sent.clear()

    await payment.on_commit(ctx, sender, _commit())

    assert len(ctx.messages_of(CompletePayment)) == 1
    saved = get_state(ctx, sender)
    assert saved["paid"] is True and saved["paid_at"]
    assert saved["stage"] == "intake"
    # The Stage 2 intake form is sent in the same turn.
    cards_sent = [
        m
        for _, m in ctx.sent
        if isinstance(m, ChatMessage)
        and any(isinstance(c, MetadataContent) for c in m.content)
    ]
    assert len(cards_sent) == 1
    meta = next(c for c in cards_sent[0].content if isinstance(c, MetadataContent)).metadata
    assert meta["card_kind"] == "form"
    assert meta["card_protocol_version"] == "1"


async def test_commit_unverified_rejects(ctx, sender, monkeypatch):
    monkeypatch.setattr(payment, "verify_paid", lambda cid: False)
    state = get_state(ctx, sender)
    await payment.request_payment(ctx, sender, state)
    ctx.sent.clear()

    await payment.on_commit(ctx, sender, _commit())

    assert len(ctx.messages_of(RejectPayment)) == 1
    assert get_state(ctx, sender)["paid"] is False


async def test_commit_wrong_method_rejects(ctx, sender, monkeypatch):
    monkeypatch.setattr(payment, "verify_paid", lambda cid: True)
    state = get_state(ctx, sender)
    await payment.request_payment(ctx, sender, state)
    ctx.sent.clear()

    await payment.on_commit(ctx, sender, _commit(method="skyfire"))

    assert len(ctx.messages_of(RejectPayment)) == 1
    assert get_state(ctx, sender)["paid"] is False


async def test_duplicate_commit_is_idempotent(ctx, sender, monkeypatch):
    monkeypatch.setattr(payment, "verify_paid", lambda cid: True)
    state = get_state(ctx, sender)
    await payment.request_payment(ctx, sender, state)
    await payment.on_commit(ctx, sender, _commit())
    ctx.sent.clear()

    # Second CommitPayment (double-click): acked, but no re-grant / no new intake.
    await payment.on_commit(ctx, sender, _commit())

    assert len(ctx.messages_of(CompletePayment)) == 1
    cards_sent = [
        m
        for _, m in ctx.sent
        if isinstance(m, ChatMessage)
        and any(isinstance(c, MetadataContent) for c in m.content)
    ]
    assert cards_sent == []
