"""Stage 0/1 - the Stripe payment gate and verification."""

from __future__ import annotations

import pytest
from uagents_core.contrib.protocols.chat import (
    ChatMessage,
    MetadataContent,
    StartSessionContent,
    TextContent,
)
from uagents_core.contrib.protocols.payment import (
    CommitPayment,
    CompletePayment,
    Funds,
    RejectPayment,
    RequestPayment,
)

import chat_proto
import payment
from session_state import AWAITING_PAYMENT, get_state, save_state

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
            "ui_mode": "embedded",
        },
    )


class _FakeSession:
    """Stand-in for a Stripe Checkout Session object."""

    def __init__(self, status: str, payment_status: str, sid: str = "cs_test_123") -> None:
        self.id = sid
        self.status = status
        self.payment_status = payment_status
        self.client_secret = "cs_test_secret"


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


async def test_card_metadata_matches_the_shipping_label_reference(monkeypatch):
    """The merged shipping-label-agent is the contract for this payload shape."""
    monkeypatch.setenv("STRIPE_PUBLISHABLE_KEY", "pk_test_x")
    payload = payment._checkout_payload(_FakeSession("open", "unpaid"))

    assert payload["ui_mode"] == "embedded_page"
    assert payload["checkout_session_id"] == "cs_test_123"
    assert payload["id"] == "cs_test_123"
    assert payload["client_secret"] == "cs_test_secret"


async def test_poll_replies_into_the_users_chat_window(ctx, sender, monkeypatch):
    """An interval tick gets a random session; ASI:One would drop a reply on it."""
    chat_session = "11111111-2222-3333-4444-555555555555"
    ctx.session = chat_session
    monkeypatch.setattr(payment, "retrieve_checkout", lambda cid: _FakeSession("complete", "paid"))
    await payment.request_payment(ctx, sender, get_state(ctx, sender))

    ctx.session = "99999999-8888-7777-6666-555555555555"  # a fresh interval tick
    await payment.poll_pending(ctx)

    assert str(ctx.session) == chat_session
    assert get_state(ctx, sender)["paid"] is True


async def test_already_paid_checkout_unlocks_without_charging_again(ctx, sender, monkeypatch):
    """The recovery path: Stripe took the money but no CommitPayment ever arrived."""
    minted: list[str] = []
    monkeypatch.setattr(payment, "retrieve_checkout", lambda cid: _FakeSession("complete", "paid"))
    monkeypatch.setattr(
        payment,
        "create_checkout_session",
        lambda s, c: minted.append(c) or {"checkout_session_id": "cs_test_new"},
    )
    state = get_state(ctx, sender)
    state["stripe_session_id"] = "cs_test_123"

    await payment.settle_or_request_payment(ctx, sender, state)

    assert minted == [], "a paid user must never be issued another checkout"
    assert ctx.messages_of(RequestPayment) == []
    assert get_state(ctx, sender)["paid"] is True


async def test_open_checkout_is_resent_rather_than_duplicated(ctx, sender, monkeypatch):
    """Each gated message used to mint a new session, so a stuck user paid twice over."""
    minted: list[str] = []
    monkeypatch.setattr(payment, "retrieve_checkout", lambda cid: _FakeSession("open", "unpaid"))
    monkeypatch.setattr(
        payment,
        "create_checkout_session",
        lambda s, c: minted.append(c) or {"checkout_session_id": "cs_test_new"},
    )
    state = get_state(ctx, sender)
    state["stripe_session_id"] = "cs_test_123"

    await payment.settle_or_request_payment(ctx, sender, state)

    assert minted == []
    reqs = ctx.messages_of(RequestPayment)
    assert len(reqs) == 1
    assert reqs[0].metadata["stripe"]["checkout_session_id"] == "cs_test_123"
    assert get_state(ctx, sender)["paid"] is False


async def test_no_stored_checkout_mints_a_fresh_one(ctx, sender, monkeypatch):
    monkeypatch.setattr(payment, "retrieve_checkout", lambda cid: None)
    state = get_state(ctx, sender)

    await payment.settle_or_request_payment(ctx, sender, state)

    assert len(ctx.messages_of(RequestPayment)) == 1
    assert get_state(ctx, sender)["stripe_session_id"] == "cs_test_123"


# ── Pre-payment questions must be answered without breaking the gate ────────
async def test_looks_like_a_fare_question():
    assert chat_proto._looks_like_a_fare_question("What is Clipper?")
    assert chat_proto._looks_like_a_fare_question("do i need a physical card")
    assert chat_proto._looks_like_a_fare_question("How does tap off work")
    assert not chat_proto._looks_like_a_fare_question("hello")
    assert not chat_proto._looks_like_a_fare_question("Berkeley to the Mission")
    # A question-shaped message with no fare/payment topic must NOT reach the
    # LLM - it has no route data and could hallucinate a fake transit answer,
    # which would both violate "no functionality before payment" and mislead.
    assert not chat_proto._looks_like_a_fare_question(
        "How do I get from Berkeley to the Mission?"
    )
    assert not chat_proto._looks_like_a_fare_question("What time does BART open?")


async def test_unpaid_question_gets_answered_without_resending_the_card(ctx, sender, monkeypatch):
    """RequestPayment must never share a turn with text (payment.py) - so a
    pre-payment question is answered on its own, against the card already on
    screen, not by minting/resending a fresh one in the same reply."""

    async def fake_answer(text):
        return "Clipper and contactless bank cards both work, same price."

    monkeypatch.setattr(chat_proto, "answer_fare_question", fake_answer)

    await chat_proto._handle_unpaid(ctx, sender, "what is Clipper?")

    assert ctx.messages_of(RequestPayment) == []
    texts = [c.text for _, m in ctx.sent if isinstance(m, ChatMessage) for c in m.content if isinstance(c, TextContent)]
    assert texts and "Clipper" in texts[0]
    assert "Pay above" in texts[0]


async def test_unpaid_non_question_falls_through_to_the_gate(ctx, sender, monkeypatch):
    async def boom(text):
        raise AssertionError("Q&A must not run for a non-question")

    monkeypatch.setattr(chat_proto, "answer_fare_question", boom)

    await chat_proto._handle_unpaid(ctx, sender, "hello")

    assert len(ctx.messages_of(RequestPayment)) == 1


async def test_qa_failure_falls_through_to_the_gate_rather_than_hanging(ctx, sender, monkeypatch):
    async def boom(text):
        raise RuntimeError("ASI:One unavailable")

    monkeypatch.setattr(chat_proto, "answer_fare_question", boom)

    await chat_proto._handle_unpaid(ctx, sender, "what is Clipper?")

    assert len(ctx.messages_of(RequestPayment)) == 1


async def test_unpaid_handler_rechecks_state_and_dispatches_if_now_paid(ctx, sender, monkeypatch):
    """A background poller tick can grant access between the caller's read and
    this call (e.g. while the previous turn's own LLM call was in flight) - the
    handler must re-read and hand off instead of holding the gate (or a Q&A
    reply) against a rider who is already unlocked."""
    settled = get_state(ctx, sender)
    settled["paid"] = True
    save_state(ctx, sender, settled)

    dispatched: list[str] = []

    async def fake_dispatch(ctx, sender, text, state):
        dispatched.append(text)

    async def boom(text):
        raise AssertionError("must not run once paid - the recheck should have returned first")

    monkeypatch.setattr(chat_proto, "_dispatch_paid", fake_dispatch)
    monkeypatch.setattr(chat_proto, "answer_fare_question", boom)

    await chat_proto._handle_unpaid(ctx, sender, "what is Clipper?")

    assert dispatched == ["what is Clipper?"]
    assert ctx.messages_of(RequestPayment) == []


async def test_empty_qa_reply_falls_through_to_the_gate(ctx, sender, monkeypatch):
    async def empty_answer(text):
        return "   "

    monkeypatch.setattr(chat_proto, "answer_fare_question", empty_answer)

    await chat_proto._handle_unpaid(ctx, sender, "what is Clipper?")

    assert len(ctx.messages_of(RequestPayment)) == 1


async def test_greeting_new_window_sends_only_the_payment_card(ctx, sender, monkeypatch):
    """Greeting must surface Stripe first; the trip form waits until after pay."""
    start_msg = ChatMessage(content=[StartSessionContent(), TextContent(text="hi")])
    await chat_proto.handle_message(ctx, sender, start_msg)

    assert len(ctx.messages_of(RequestPayment)) == 1
    assert get_state(ctx, sender)["paid"] is False
    forms = [
        m
        for _, m in ctx.sent
        if isinstance(m, ChatMessage)
        and any(
            isinstance(c, MetadataContent) and c.metadata.get("card_kind") == "form"
            for c in m.content
        )
    ]
    assert forms == []


async def test_new_window_drops_a_previous_windows_watch(ctx, sender, monkeypatch):
    """A paid checkout from an earlier chat must not unlock the next window."""
    monkeypatch.setattr(
        payment, "retrieve_checkout", lambda cid: _FakeSession("complete", "paid", sid=cid)
    )
    await payment.request_payment(ctx, sender, get_state(ctx, sender))
    await payment.poll_pending(ctx)
    assert get_state(ctx, sender)["paid"] is True
    ctx.sent.clear()

    # Fresh Stripe session for the new window (still unpaid).
    monkeypatch.setattr(
        payment,
        "create_checkout_session",
        lambda s, c: {
            "client_secret": "cs_test_secret",
            "id": "cs_test_new_window",
            "checkout_session_id": "cs_test_new_window",
            "publishable_key": "pk_test_x",
            "currency": "usd",
            "amount_cents": "100",
            "ui_mode": "embedded_page",
        },
    )
    monkeypatch.setattr(
        payment,
        "retrieve_checkout",
        lambda cid: (
            _FakeSession("complete", "paid", sid=cid)
            if cid == "cs_test_123"
            else _FakeSession("open", "unpaid", sid=cid)
        ),
    )

    start_msg = ChatMessage(content=[StartSessionContent(), TextContent(text="hi")])
    await chat_proto.handle_message(ctx, sender, start_msg)

    assert get_state(ctx, sender)["paid"] is False
    assert get_state(ctx, sender)["stripe_session_id"] == "cs_test_new_window"
    assert len(ctx.messages_of(RequestPayment)) == 1
    # Poller must not resurrect the previous window's paid checkout.
    await payment.poll_pending(ctx)
    assert get_state(ctx, sender)["paid"] is False


async def test_poll_ignores_a_watch_that_does_not_match_session_state(ctx, sender, monkeypatch):
    """Safety net if a stale watch somehow survives a window reset."""
    import time

    monkeypatch.setattr(
        payment, "retrieve_checkout", lambda cid: _FakeSession("complete", "paid", sid=cid)
    )
    state = get_state(ctx, sender)
    state["stage"] = AWAITING_PAYMENT
    state["stripe_session_id"] = "cs_test_current"
    state["paid"] = False
    from session_state import save_state

    save_state(ctx, sender, state)
    ctx.storage.set(
        payment._PENDING_KEY,
        {
            sender: {
                "id": "cs_test_old_paid",
                "since": time.time(),
                "chat_session": str(ctx.session),
            }
        },
    )

    await payment.poll_pending(ctx)

    assert get_state(ctx, sender)["paid"] is False
    assert payment._pending(ctx) == {}


async def test_poll_unlocks_as_soon_as_stripe_settles(ctx, sender, monkeypatch):
    """The trip form must arrive on its own, not only on the user's next message."""
    monkeypatch.setattr(payment, "retrieve_checkout", lambda cid: _FakeSession("complete", "paid"))
    state = get_state(ctx, sender)
    await payment.request_payment(ctx, sender, state)
    ctx.sent.clear()

    await payment.poll_pending(ctx)

    assert get_state(ctx, sender)["paid"] is True
    cards = [
        m
        for _, m in ctx.sent
        if isinstance(m, ChatMessage) and any(isinstance(c, MetadataContent) for c in m.content)
    ]
    assert len(cards) == 1
    meta = next(c for c in cards[0].content if isinstance(c, MetadataContent)).metadata
    assert meta["card_kind"] == "form"
    # Stops watching, so a second tick cannot re-send the form.
    await payment.poll_pending(ctx)
    assert len(ctx.messages_of(ChatMessage)) == 1


async def test_poll_costs_nothing_while_no_checkout_is_outstanding(ctx, monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(payment, "retrieve_checkout", lambda cid: calls.append(cid))

    await payment.poll_pending(ctx)

    assert calls == []


async def test_poll_stops_watching_an_expired_checkout(ctx, sender, monkeypatch):
    monkeypatch.setattr(payment, "retrieve_checkout", lambda cid: _FakeSession("expired", "unpaid"))
    state = get_state(ctx, sender)
    await payment.request_payment(ctx, sender, state)

    await payment.poll_pending(ctx)

    assert payment._pending(ctx) == {}
    assert get_state(ctx, sender)["paid"] is False


async def test_commit_falls_back_to_the_transaction_id_after_a_restart(
    ctx, sender, monkeypatch
):
    """RESET_STORAGE_ON_START wipes stripe_session_id; the commit still carries it."""
    checked: list[str] = []
    monkeypatch.setattr(payment, "verify_paid", lambda cid: checked.append(cid) or True)

    await payment.on_commit(ctx, sender, _commit(txn="cs_test_from_asi_one"))

    assert checked == ["cs_test_from_asi_one"]
    assert len(ctx.messages_of(CompletePayment)) == 1
    assert get_state(ctx, sender)["paid"] is True


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


async def test_mid_flow_message_does_not_re_request_payment(ctx, sender, monkeypatch):
    """Regression: a paid, mid-flow message must never re-trigger RequestPayment.

    A previous version reset ``paid`` on any ``ctx.session`` mismatch, which
    fired spuriously on structured card-submission turns (``ctx.session`` isn't
    stable across every turn) and re-charged an already-paid sender. The fix
    keys the reset off ``StartSessionContent`` instead, which only appears on a
    message that truly begins a new window.
    """
    monkeypatch.setattr(payment, "verify_paid", lambda cid: True)
    # Isolate the gate logic in handle_message from the intake dispatch that
    # would otherwise run once paid - not what this regression test is about.
    dispatched: list[str] = []

    async def fake_dispatch(ctx, sender, text, state):
        dispatched.append(text)

    monkeypatch.setattr(chat_proto, "_dispatch_paid", fake_dispatch)

    # A real new window: gate fires once.
    start_msg = ChatMessage(content=[StartSessionContent(), TextContent(text="hi")])
    await chat_proto.handle_message(ctx, sender, start_msg)
    assert len(ctx.messages_of(RequestPayment)) == 1

    # Pay, unlocking the session.
    await payment.on_commit(ctx, sender, _commit())
    assert get_state(ctx, sender)["paid"] is True
    ctx.sent.clear()

    # A later, same-window message (no StartSessionContent) arrives with a
    # *different* ctx.session id than the one recorded at window start - the
    # exact structured-card-submission scenario that triggered the old bug.
    ctx.session = "some-other-session-id"
    followup = ChatMessage(content=[TextContent(text="hello again")])
    await chat_proto.handle_message(ctx, sender, followup)

    assert get_state(ctx, sender)["paid"] is True
    assert ctx.messages_of(RequestPayment) == []
    assert dispatched == ["hello again"]
