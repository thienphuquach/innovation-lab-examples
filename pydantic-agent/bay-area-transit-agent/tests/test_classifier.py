"""Cross-cutting interrupt classifier - fast path + override/escalate/side/clarify."""

from __future__ import annotations

import json

import pytest

pytest.importorskip("pydantic_ai")

from pydantic_ai.models.test import TestModel
from uagents_core.contrib.protocols.chat import ChatMessage, MetadataContent, TextContent

import chat_proto
from ai import IntentResult, classify_intent
from session_state import DONE, INTAKE, SHOWING_DETAIL, SHOWING_ROUTES, get_state

pytestmark = pytest.mark.asyncio


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
        "depart_time": None, "priority": "fastest",
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


async def test_escalate_gives_fastest_terminal(ctx, sender, monkeypatch):
    async def fake(*a, **k):
        return IntentResult(intent="escalate", reply="", history_json="[]")

    monkeypatch.setattr(chat_proto, "classify_intent", fake)
    state = _routes_state(ctx, sender)
    await chat_proto._dispatch_paid(ctx, sender, "my train got cancelled, I need the fastest now", state)
    metas = _cards(ctx, "detail")
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

    async def empty_extract(text):
        from ai import TripExtraction
        return TripExtraction(origin="", destination="", depart_time_iso=None, priority="fastest")

    monkeypatch.setattr(chat_proto, "classify_intent", fake)
    monkeypatch.setattr(chat_proto, "extract_trip", empty_extract)
    state = _routes_state(ctx, sender)
    await chat_proto._dispatch_paid(ctx, sender, "actually take me from Oakland instead", state)
    saved = get_state(ctx, sender)
    assert saved["stage"] == INTAKE
    assert saved["trip"] is None  # old trip dropped
    assert saved["paid"] is True  # unlock preserved, no re-charge
    assert _cards(ctx, "form")


async def test_classifier_failure_defaults_to_override(ctx, sender, monkeypatch):
    async def boom(*a, **k):
        raise RuntimeError("LLM down")

    async def empty_extract(text):
        from ai import TripExtraction
        return TripExtraction(origin="", destination="", depart_time_iso=None, priority="fastest")

    monkeypatch.setattr(chat_proto, "classify_intent", boom)
    monkeypatch.setattr(chat_proto, "extract_trip", empty_extract)
    state = _routes_state(ctx, sender)
    await chat_proto._dispatch_paid(ctx, sender, "some free text", state)
    assert get_state(ctx, sender)["stage"] == INTAKE  # degraded gracefully
