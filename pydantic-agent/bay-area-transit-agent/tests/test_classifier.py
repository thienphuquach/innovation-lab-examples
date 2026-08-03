"""Cross-cutting interrupt classifier - fast path + override/escalate/side/clarify."""

from __future__ import annotations

import json
from typing import Any

import pytest

pytest.importorskip("pydantic_ai")

from pydantic_ai.models.test import TestModel
from uagents_core.contrib.protocols.chat import (
    ChatMessage,
    MetadataContent,
    TextContent,
)

import chat_proto
from ai import _FARE_KNOWLEDGE, IntentResult, classify_intent
from session_state import (
    AWAITING_CONFIRM,
    DONE,
    INTAKE,
    SHOWING_DETAIL,
    SHOWING_ROUTES,
    get_state,
)

pytestmark = pytest.mark.asyncio


async def test_fare_knowledge_covers_the_questions_a_first_time_rider_actually_asks():
    """Guard against silently dropping a verified fact (research-notes.md §11) in
    a future edit - these are the questions a first-timer is most likely to ask,
    not just the ones already visible on the fare card."""
    facts = _FARE_KNOWLEDGE.lower()
    for must_mention in (
        "not required",  # a Clipper card isn't mandatory - a bank card works too
        "$3",  # physical card fee
        "free",  # mobile wallet is free
        "lost",  # lost/stolen card replacement
        "$5",  # replacement fee
        "discount",  # youth/senior/disability/income-qualified cards
        "7.55",  # BART's missed-tap penalty
    ):
        assert must_mention in facts, f"missing verified fact: {must_mention!r}"


def _cards(ctx, kind=None):
    out = []
    for _, m in ctx.sent:
        if isinstance(m, ChatMessage):
            for c in m.content:
                if isinstance(c, MetadataContent) and (kind is None or c.metadata["card_kind"] == kind):
                    out.append(c.metadata)
    return out


def _texts(ctx):
    out = []
    for _, m in ctx.sent:
        if isinstance(m, ChatMessage):
            out += [c.text for c in m.content if isinstance(c, TextContent)]
    return out


def _routes_state(ctx, sender, stage=SHOWING_ROUTES):
    state = get_state(ctx, sender)
    state["paid"] = True
    state["stage"] = stage
    state["trip"] = {
        "origin_text": "Berkeley", "origin_coords": [37.87, -122.27],
        "destination_text": "SF", "destination_coords": [37.78, -122.41],
        "priority": "fastest",
    }
    state["last_itineraries"] = {
        "itineraries": [
            {"duration": 1611, "transfers": 0, "startTime": 1_714_784_876_000,
             "endTime": 1_714_786_487_000,
             "legs": [{"mode": "RAIL", "transitLeg": True, "routeShortName": "R", "routeId": "BA:R"}]},
            {"duration": 3000, "transfers": 2, "startTime": 1_714_784_876_000,
             "endTime": 1_714_787_876_000,
             "legs": [{"mode": "BUS", "transitLeg": True, "routeShortName": "51", "routeId": "AC:51"}]},
        ]
    }
    state["selected_route_id"] = "0"
    state["fare_options"] = [{"id": "clipper", "label": "Clipper", "amount": 3.25, "estimated": False}]
    state["selected_fare_option"] = "clipper"
    return state


# ── ai-level classification (TestModel, no network) ──────────────────────────
async def test_classify_intent_returns_typed_intent():
    tm = TestModel(custom_output_args={"intent": "escalate", "reply": ""})
    result = await classify_intent("I'm stuck, need the fastest now", stage=SHOWING_ROUTES, model=tm)
    assert result.intent == "escalate"
    assert result.history_json  # history is serialized for the next turn


# ── dispatch routing (classifier stubbed) ────────────────────────────────────
async def test_fast_path_skips_classifier(ctx, sender, monkeypatch):
    async def boom(*a, **k):
        raise AssertionError("classifier must not run for a structured selection")

    monkeypatch.setattr(chat_proto, "classify_intent", boom)
    state = _routes_state(ctx, sender, stage=SHOWING_DETAIL)
    await chat_proto._dispatch_paid(ctx, sender, json.dumps({"action": "back_to_routes"}), state)
    assert _cards(ctx, "carousel")
    assert get_state(ctx, sender)["stage"] == SHOWING_ROUTES


async def test_done_stage_question_is_answered_not_treated_as_broken_intake(ctx, sender, monkeypatch):
    """Regression guard for the live root cause behind every "can't ask a
    question" report so far (research-notes.md §18): DONE was excluded from
    the classifier's stages on the theory that "next message is always a new
    trip", so a rider who just confirmed a trip and immediately asked a
    follow-up question ("is Clipper cheaper than a card?") had that question
    run through plain trip extraction, which correctly found no origin/
    destination in a question and bounced them to "I need both a starting
    point and a destination" - a confusing non-answer to something they
    explicitly asked, and it then left `stage` at INTAKE so the *next* message
    hit the identical bug again regardless of what it said.
    """
    async def fake(*a, **k):
        return IntentResult(
            intent="side_question",
            reply="No, Clipper and a contactless card cost the same.",
            history_json="[]",
        )

    monkeypatch.setattr(chat_proto, "classify_intent", fake)
    state = get_state(ctx, sender)
    state["paid"] = True
    state["stage"] = DONE
    chat_proto.save_state(ctx, sender, state)
    await chat_proto._dispatch_paid(
        ctx, sender, "Is this clipper will be more cheaper than credit card ?", state
    )
    assert "No, Clipper and a contactless card cost the same." in _texts(ctx)
    assert get_state(ctx, sender)["stage"] == DONE  # not bumped to intake by a question


async def test_done_stage_real_trip_request_still_starts_a_new_trip(ctx, sender, monkeypatch):
    """A genuine new trip request right after DONE must still work - only the
    "question gets misrouted as a broken trip" case is being fixed."""
    async def fake(*a, **k):
        return IntentResult(intent="override", reply="", history_json="[]")

    async def berkeley_extract(text):
        from ai import TripExtraction
        return TripExtraction(origin="Berkeley", destination="the Mission", priority="fastest")

    async def resolve(text):
        from models import GeocodeCandidate
        lat, lon = (37.87, -122.27) if "berkeley" in text.lower() else (37.76, -122.41)
        return "resolved", [GeocodeCandidate(text, lat, lon)]

    monkeypatch.setattr(chat_proto, "classify_intent", fake)
    monkeypatch.setattr(chat_proto, "extract_trip", berkeley_extract)
    monkeypatch.setattr(chat_proto, "resolve_place", resolve)
    state = get_state(ctx, sender)
    state["paid"] = True
    state["stage"] = DONE
    chat_proto.save_state(ctx, sender, state)
    await chat_proto._dispatch_paid(ctx, sender, "Berkeley to the Mission", state)
    saved = get_state(ctx, sender)
    # _handle_override -> _handle_intake proceeded past the empty-fields check
    # (the DONE-stage bug this guards against) all the way through geocoding -
    # never falling back to the "I need both a starting point" error.
    assert saved["stage"] != DONE
    assert not _texts(ctx) or "I need both a starting point" not in _texts(ctx)[-1]


async def test_escalate_gives_fastest_terminal(ctx, sender, monkeypatch):
    async def fake(*a, **k):
        return IntentResult(intent="escalate", reply="", history_json="[]")

    monkeypatch.setattr(chat_proto, "classify_intent", fake)
    state = _routes_state(ctx, sender)
    await chat_proto._dispatch_paid(ctx, sender, "my train got cancelled, I need the fastest now", state)
    metas = _cards(ctx, "custom")  # final_itinerary_card is now a custom list-sequence card
    assert metas and metas[0].get("is_terminal") == "true"
    assert get_state(ctx, sender)["stage"] == DONE


async def test_side_question_answers_and_reshows_card(ctx, sender, monkeypatch):
    async def fake(*a, **k):
        return IntentResult(intent="side_question", reply="Clipper is $3.25.", history_json="[]")

    monkeypatch.setattr(chat_proto, "classify_intent", fake)
    state = _routes_state(ctx, sender)
    await chat_proto._dispatch_paid(ctx, sender, "what was that fare again?", state)
    assert "Clipper is $3.25." in _texts(ctx)
    assert _cards(ctx, "carousel"), "the same carousel should be re-shown"
    assert get_state(ctx, sender)["stage"] == SHOWING_ROUTES  # place preserved


async def test_confused_question_is_classified_as_side_question_not_override(ctx, sender, monkeypatch):
    """Regression guard for two live misclassifications ("I still don't know
    anything about Clipper, can you explain" and "does I need to tab in and tab
    off ... I don't understand please give me full detail" both got classified
    as "override", wiping the trip in progress). This only verifies the dispatch
    wiring honors "side_question" without touching the trip - actual classifier
    judgment against the real ASI:One model needs live/manual verification, not
    something a mocked-model unit test can confirm."""
    async def fake(*a, **k):
        return IntentResult(
            intent="side_question",
            reply="Tap your card/phone in and out on distance-priced legs.",
            history_json="[]",
        )

    monkeypatch.setattr(chat_proto, "classify_intent", fake)
    state = _routes_state(ctx, sender, stage=AWAITING_CONFIRM)
    await chat_proto._dispatch_paid(
        ctx, sender, "does I need to tab in and tab off when I travel I don't understand", state
    )
    assert "Tap your card/phone in and out" in _texts(ctx)[-1]
    assert get_state(ctx, sender)["stage"] == AWAITING_CONFIRM  # trip preserved, not wiped


async def test_override_never_wipes_a_trip_when_the_text_names_no_place(ctx, sender, monkeypatch):
    """Regression guard for the live failure this backstops: a rider asked "can
    you tell me more about clipper? and are there anything I need to pay
    attention when traveling?" mid-flow and lost their trip, with no exception
    logged - meaning the classifier itself (not a crash) returned "override"
    for a message that is plainly a question, not a restated trip. Because LLM
    classification isn't 100% deterministic, the fix does not depend on the
    classifier always getting this right: _handle_override independently
    re-checks that the text actually names a place before destroying anything,
    regardless of what the classifier decided.
    """
    async def fake(*a, **k):
        return IntentResult(intent="override", reply="", history_json="[]")

    async def no_place_extract(text):
        from ai import TripExtraction
        return TripExtraction(origin="", destination="", priority="fastest")

    monkeypatch.setattr(chat_proto, "classify_intent", fake)
    monkeypatch.setattr(chat_proto, "extract_trip", no_place_extract)
    state = _routes_state(ctx, sender, stage=SHOWING_DETAIL)
    chat_proto.save_state(ctx, sender, state)  # the guard path saves nothing itself
    text = (
        "can you tell me more about clipper ? and are there anything I need "
        "to pay attention when traveling ?"
    )
    await chat_proto._dispatch_paid(ctx, sender, text, state)
    saved = get_state(ctx, sender)
    assert saved["stage"] == SHOWING_DETAIL  # trip preserved, not wiped
    assert saved["trip"] is not None
    assert "didn't catch a new starting point" in _texts(ctx)[-1]


async def test_side_question_at_review_does_not_collide_with_finalize_history(ctx, sender, monkeypatch):
    """Regression guard: the Stage 5 review card opens a deferred-tool gate
    (_finalize_agent) whose own pending-tool-call history was previously stored
    under the *same* state key ("message_history") that classify_intent reuses
    for its own, unrelated conversation. Asking a question while the review
    card is open then fed the finalize agent's dangling tool call into
    classify_intent, which Pydantic AI correctly refused ("Cannot provide a new
    user prompt when the message history contains unprocessed tool calls."),
    raising an exception that the broad except-Exception fallback silently
    turned into "override" - wiping the trip. The two histories must be kept
    in separate state keys."""
    captured: dict[str, Any] = {}

    async def fake(text, *, history_json=None, stage=None, model=None):
        captured["history_json"] = history_json
        return IntentResult(
            intent="side_question", reply="Clipper isn't required.", history_json="[updated]"
        )

    monkeypatch.setattr(chat_proto, "classify_intent", fake)
    state = _routes_state(ctx, sender, stage=AWAITING_CONFIRM)
    state["pending_approval"] = {"tool_call_id": "t1"}
    state["finalize_history"] = "[finalize agent's own dangling tool-call history]"
    state["message_history"] = "[intent classifier's own prior history]"

    await chat_proto._dispatch_paid(ctx, sender, "can you tell me more about clipper?", state)

    assert captured["history_json"] == "[intent classifier's own prior history]"
    assert "Clipper isn't required." in _texts(ctx)[-1]
    saved = get_state(ctx, sender)
    assert saved["stage"] == AWAITING_CONFIRM  # review card preserved, not wiped
    assert saved["pending_approval"] == {"tool_call_id": "t1"}  # still resumable
    assert saved["finalize_history"] == "[finalize agent's own dangling tool-call history]"


async def test_clarify_asks_one_question_and_stays(ctx, sender, monkeypatch):
    async def fake(*a, **k):
        return IntentResult(intent="clarify", reply="Do you mean depart or arrive by 6?", history_json="[]")

    monkeypatch.setattr(chat_proto, "classify_intent", fake)
    state = _routes_state(ctx, sender)
    await chat_proto._dispatch_paid(ctx, sender, "around 6 maybe", state)
    assert "Do you mean depart or arrive by 6?" in _texts(ctx)
    assert not _cards(ctx)  # nothing advanced
    assert get_state(ctx, sender)["stage"] == SHOWING_ROUTES


async def test_override_clears_trip_and_reintakes(ctx, sender, monkeypatch):
    async def fake(*a, **k):
        return IntentResult(intent="override", reply="", history_json="[]")

    async def oakland_extract(text):
        from ai import TripExtraction
        # A real override always names a place - "Oakland" here - which is what
        # lets _handle_override's guard (never wipe on an empty extraction)
        # trust this one and proceed.
        return TripExtraction(origin="Oakland", destination="", priority="fastest")

    monkeypatch.setattr(chat_proto, "classify_intent", fake)
    monkeypatch.setattr(chat_proto, "extract_trip", oakland_extract)
    state = _routes_state(ctx, sender)
    await chat_proto._dispatch_paid(ctx, sender, "actually take me from Oakland instead", state)
    saved = get_state(ctx, sender)
    assert saved["stage"] == INTAKE
    assert saved["trip"] is None  # old trip dropped
    assert saved["paid"] is True  # unlock preserved, no re-charge
    assert _cards(ctx, "form")


# ── accept_default: "just pick for me" hands the decision to the agent ──────
# (ux-diagnosis.md issue E - a low-effort default path, not a forced
# self-serve comparison, at whichever stage the user is currently on).
async def test_accept_default_at_routes_picks_the_fastest_itinerary(ctx, sender, monkeypatch):
    async def fake(*a, **k):
        return IntentResult(intent="accept_default", reply="", history_json="[]")

    monkeypatch.setattr(chat_proto, "classify_intent", fake)
    state = _routes_state(ctx, sender, stage=SHOWING_ROUTES)
    await chat_proto._dispatch_paid(ctx, sender, "you decide, I don't mind", state)
    saved = get_state(ctx, sender)
    assert saved["stage"] == SHOWING_DETAIL
    assert saved["selected_route_id"] == "0"  # the 1611s itinerary, not the 3000s one


async def test_accept_default_at_detail_confirms_with_the_precomputed_cheapest_fare(
    ctx, sender, monkeypatch
):
    async def fake(*a, **k):
        return IntentResult(intent="accept_default", reply="", history_json="[]")

    monkeypatch.setattr(chat_proto, "classify_intent", fake)
    state = _routes_state(ctx, sender, stage=SHOWING_DETAIL)
    await chat_proto._dispatch_paid(ctx, sender, "just pick for me", state)
    saved = get_state(ctx, sender)
    assert saved["stage"] == AWAITING_CONFIRM
    assert saved["selected_fare_option"] == "clipper"  # untouched - the precomputed default
    assert _cards(ctx, "review")


async def test_accept_default_at_awaiting_confirm_just_confirms(ctx, sender, monkeypatch):
    async def fake(*a, **k):
        return IntentResult(intent="accept_default", reply="", history_json="[]")

    monkeypatch.setattr(chat_proto, "classify_intent", fake)
    state = _routes_state(ctx, sender, stage=AWAITING_CONFIRM)
    await chat_proto._dispatch_paid(ctx, sender, "whatever, you choose", state)
    saved = get_state(ctx, sender)
    assert saved["stage"] == DONE
    assert _cards(ctx, "custom")  # the confirmed-trip recap


async def test_classifier_failure_with_a_real_place_still_overrides(ctx, sender, monkeypatch):
    async def boom(*a, **k):
        raise RuntimeError("LLM down")

    async def oakland_extract(text):
        from ai import TripExtraction
        return TripExtraction(origin="Oakland", destination="", priority="fastest")

    monkeypatch.setattr(chat_proto, "classify_intent", boom)
    monkeypatch.setattr(chat_proto, "extract_trip", oakland_extract)
    state = _routes_state(ctx, sender)
    await chat_proto._dispatch_paid(ctx, sender, "actually from Oakland instead", state)
    assert get_state(ctx, sender)["stage"] == INTAKE  # degraded gracefully


async def test_classifier_failure_without_a_place_preserves_the_trip(ctx, sender, monkeypatch):
    """When the classifier is unreachable *and* the text names no place, the
    old "default to override" fallback used to wipe the trip on pure guesswork
    - worse than doing nothing. The same extraction-based guard that protects
    a misclassified "override" (see the test above) applies here too."""
    async def boom(*a, **k):
        raise RuntimeError("LLM down")

    async def empty_extract(text):
        from ai import TripExtraction
        return TripExtraction(origin="", destination="", priority="fastest")

    monkeypatch.setattr(chat_proto, "classify_intent", boom)
    monkeypatch.setattr(chat_proto, "extract_trip", empty_extract)
    state = _routes_state(ctx, sender)
    chat_proto.save_state(ctx, sender, state)  # the guard path saves nothing itself
    await chat_proto._dispatch_paid(ctx, sender, "some free text", state)
    saved = get_state(ctx, sender)
    assert saved["stage"] == SHOWING_ROUTES  # trip preserved, not wiped
    assert saved["trip"] is not None
