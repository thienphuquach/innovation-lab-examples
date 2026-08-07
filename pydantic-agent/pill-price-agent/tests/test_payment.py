"""The Stripe gate: the agent must stay locked for a payment that didn't clear."""

from __future__ import annotations

from typing import Any

import pytest

import chat_proto
import payment
import session_state
from session_state import SessionState


class TestTestKeyGuard:
    def test_a_live_secret_key_refuses_to_start(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_live_should_never_run")
        with pytest.raises(RuntimeError, match="test key"):
            payment.assert_stripe_test_keys()

    def test_a_live_publishable_key_refuses_to_start(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_fine")
        monkeypatch.setenv("STRIPE_PUBLISHABLE_KEY", "pk_live_should_never_run")
        with pytest.raises(RuntimeError, match="test key"):
            payment.assert_stripe_test_keys()

    def test_test_keys_pass(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_fine")
        monkeypatch.setenv("STRIPE_PUBLISHABLE_KEY", "pk_test_fine")
        payment.assert_stripe_test_keys()

    def test_unverified_checkout_is_not_paid(self, monkeypatch: pytest.MonkeyPatch) -> None:
        assert payment.verify_paid("") is False


class _FakeStorage:
    def __init__(self) -> None:
        self._data: dict[str, str] = {}

    def get(self, key: str) -> str | None:
        return self._data.get(key)

    def set(self, key: str, value: str) -> None:
        self._data[key] = value


class _FakeLogger:
    def info(self, *args: Any, **kwargs: Any) -> None:
        pass

    def debug(self, *args: Any, **kwargs: Any) -> None:
        pass


class _FakeContext:
    """Just enough of ``uagents.Context`` for the payment flow to run offline."""

    def __init__(self) -> None:
        self.storage = _FakeStorage()
        self.logger = _FakeLogger()
        self.sent: list[tuple[str, Any]] = []

    async def send(self, destination: str, message: Any) -> None:
        self.sent.append((destination, message))

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"this offline test should not need ctx.{name}")


class TestBackgroundPoller:
    """The fix for 'why do I have to type paid': poll Stripe, don't wait on ASI:One."""

    async def test_unlocks_automatically_once_stripe_confirms(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("STRIPE_POLL_INTERVAL_SECONDS", "0.01")
        monkeypatch.setenv("STRIPE_CHECKOUT_EXPIRES_SECONDS", "1")
        monkeypatch.setattr(payment, "verify_paid", lambda checkout_id: True)

        ctx = _FakeContext()
        sender = "user1"
        checkout_id = "cs_test_abc"
        session_state.save_state(ctx, sender, SessionState(stripe_session_id=checkout_id))

        delivered: dict[str, Any] = {}

        async def fake_deliver(
            ctx: Any, sender: str, state: SessionState, *, approved: bool
        ) -> None:
            delivered["approved"] = approved

        monkeypatch.setattr(chat_proto, "deliver_access_payment", fake_deliver)

        await payment._poll_until_paid(ctx, sender, checkout_id)

        assert delivered == {"approved": True}

    async def test_stops_without_delivering_if_resolved_another_way(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A manual 'paid' (or a rejection) clears stripe_session_id; the poller must back off."""
        monkeypatch.setenv("STRIPE_POLL_INTERVAL_SECONDS", "0.01")
        monkeypatch.setenv("STRIPE_CHECKOUT_EXPIRES_SECONDS", "1")
        monkeypatch.setattr(payment, "verify_paid", lambda checkout_id: True)

        ctx = _FakeContext()
        sender = "user1"
        checkout_id = "cs_test_abc"
        # Already resolved by another path before the poller gets its first tick.
        session_state.save_state(ctx, sender, SessionState(stripe_session_id=None))

        called = False

        async def fake_deliver(*args: Any, **kwargs: Any) -> None:
            nonlocal called
            called = True

        monkeypatch.setattr(chat_proto, "deliver_access_payment", fake_deliver)

        await payment._poll_until_paid(ctx, sender, checkout_id)

        assert called is False


class TestDeliverAccessPayment:
    """Resolving the upfront charge either unlocks the agent or hard-stops it."""

    async def test_approved_unlocks_and_shows_the_welcome_card(self) -> None:
        ctx = _FakeContext()
        state = SessionState(stripe_session_id="cs_test_abc")

        await chat_proto.deliver_access_payment(ctx, "user1", state, approved=True)

        assert state.stripe_paid is True
        assert state.node == "Start"
        assert state.stripe_session_id is None
        assert len(ctx.sent) == 1
        card = ctx.sent[0][1].content[-1]
        assert card.metadata["card_kind"] == "custom"
        assert "You're in" in card.metadata["card_payload"]

    async def test_declined_hard_stops_and_re_arms_the_paywall(self) -> None:
        ctx = _FakeContext()
        state = SessionState(stripe_session_id="cs_test_abc")

        await chat_proto.deliver_access_payment(ctx, "user1", state, approved=False)

        assert state.stripe_paid is False
        assert state.node == "Paywall"
        assert state.stripe_session_id is None
        text = ctx.sent[0][1].content[0].text
        assert "nothing is unlocked" in text


class TestSelectionParsing:
    def test_json_selection(self) -> None:
        parsed = chat_proto.parse_selection('{"action": "pick_group", "group_id": "A|1 MG|TAB"}')
        assert parsed["action"] == "pick_group"
        assert parsed["group_id"] == "A|1 MG|TAB"

    def test_prose_selection_from_the_planner(self) -> None:
        parsed = chat_proto.parse_selection("user selected back_to_prices")
        assert parsed["action"] == "back_to_prices"

    def test_ordinary_english_is_not_mistaken_for_a_selection(self) -> None:
        """'check the price' must stay free text so intent classification runs."""
        assert chat_proto.parse_selection("can you check the price of metformin") == {}
        assert chat_proto.parse_selection("what does the drug info say") == {}

    def test_malformed_json_falls_through(self) -> None:
        assert chat_proto.parse_selection("{not json}") == {}
