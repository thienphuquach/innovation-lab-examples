"""Stage 5/6 - review card + deferred-tool finalize gate + repeat use."""

from __future__ import annotations

import json

import pytest

pytest.importorskip("pydantic_ai")

from pydantic_ai import ModelResponse, TextPart, ToolCallPart
from pydantic_ai.messages import ModelMessage
from pydantic_ai.models.function import AgentInfo, FunctionModel
from uagents_core.contrib.protocols.chat import ChatMessage, MetadataContent

import chat_proto
from ai import FinalizeStart, resume_finalize, start_finalize
from session_state import AWAITING_CONFIRM, DONE, INTAKE, SHOWING_DETAIL, get_state


def _finalize_script(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
    """Call the gated tool first; once it has returned, emit a final text reply."""
    for message in messages:
        for part in getattr(message, "parts", []):
            if part.__class__.__name__ == "ToolReturnPart":
                return ModelResponse(parts=[TextPart("Confirmed.")])
    return ModelResponse(parts=[ToolCallPart("finalize_itinerary", {"summary": "trip"})])


# ── ai-level deferred gate (no network, FunctionModel) ───────────────────────
@pytest.mark.asyncio
async def test_finalize_defers_before_approval():
    start = await start_finalize("BART → Muni | Clipper $3.25", model=FunctionModel(_finalize_script))
    assert start.deferred is True
    assert start.tool_call_id


@pytest.mark.asyncio
async def test_finalize_runs_on_approval():
    model = FunctionModel(_finalize_script)
    start = await start_finalize("trip", model=model)
    confirmed = await resume_finalize(
        history_json=start.history_json, tool_call_id=start.tool_call_id, approved=True, model=model
    )
    assert confirmed is True


@pytest.mark.asyncio
async def test_finalize_skipped_on_denial():
    model = FunctionModel(_finalize_script)
    start = await start_finalize("trip", model=model)
    confirmed = await resume_finalize(
        history_json=start.history_json, tool_call_id=start.tool_call_id, approved=False, model=model
    )
    assert confirmed is False


# ── Stage 5/6 dispatch (gate stubbed) ────────────────────────────────────────
def _cards(ctx, kind=None):
    out = []
    for _, m in ctx.sent:
        if isinstance(m, ChatMessage):
            for c in m.content:
                if isinstance(c, MetadataContent) and (kind is None or c.metadata["card_kind"] == kind):
                    out.append(c.metadata)
    return out


def _detail_state(ctx, sender):
    state = get_state(ctx, sender)
    state["paid"] = True
    state["stage"] = SHOWING_DETAIL
    state["trip"] = {"origin_text": "A", "destination_text": "B", "priority": "fastest"}
    state["last_itineraries"] = {
        "itineraries": [
            {
                "duration": 1611,
                "transfers": 1,
                "startTime": 1_714_784_876_000,
                "endTime": 1_714_786_487_000,
                "legs": [
                    {"mode": "WALK", "transitLeg": False},
                    {"mode": "RAIL", "transitLeg": True, "routeShortName": "R", "routeId": "BA:R"},
                ],
            }
        ]
    }
    state["selected_route_id"] = "0"
    state["fare_options"] = [
        {"id": "clipper", "label": "Clipper", "amount": 3.25, "estimated": False}
    ]
    state["selected_fare_option"] = "clipper"
    return state


@pytest.mark.asyncio
async def test_show_review_opens_gate_and_renders_review(ctx, sender, monkeypatch):
    async def fake_start(summary, **k):
        return FinalizeStart(deferred=True, history_json="[]", tool_call_id="t1")

    monkeypatch.setattr(chat_proto, "start_finalize", fake_start)
    state = _detail_state(ctx, sender)
    await chat_proto._show_review(ctx, sender, state)

    metas = _cards(ctx, "review")
    assert metas
    payload = json.loads(metas[0]["card_payload"])
    assert payload["approve_cta"]["selection"]["action"] == "confirm"
    assert payload["reject_cta"]["selection"]["action"] == "cancel"
    saved = get_state(ctx, sender)
    assert saved["stage"] == AWAITING_CONFIRM
    assert saved["pending_approval"] == {"tool_call_id": "t1"}


@pytest.mark.asyncio
async def test_confirm_finalizes_and_enters_done(ctx, sender, monkeypatch):
    async def fake_resume(**k):
        assert k["approved"] is True
        return True

    monkeypatch.setattr(chat_proto, "resume_finalize", fake_resume)
    state = _detail_state(ctx, sender)
    state["stage"] = AWAITING_CONFIRM
    state["pending_approval"] = {"tool_call_id": "t1"}
    state["message_history"] = "[]"

    await chat_proto._handle_confirm(ctx, sender, json.dumps({"action": "confirm"}), state)

    metas = _cards(ctx, "detail")
    assert metas and metas[0].get("is_terminal") == "true"
    saved = get_state(ctx, sender)
    assert saved["stage"] == DONE
    assert saved["trip"] is None  # trip state cleared for the next request
    assert saved["paid"] is True  # but the unlock persists (no re-charge)


@pytest.mark.asyncio
async def test_cancel_returns_to_intake_form(ctx, sender, monkeypatch):
    async def fake_resume(**k):
        assert k["approved"] is False
        return False

    monkeypatch.setattr(chat_proto, "resume_finalize", fake_resume)
    state = _detail_state(ctx, sender)
    state["stage"] = AWAITING_CONFIRM
    state["pending_approval"] = {"tool_call_id": "t1"}
    state["message_history"] = "[]"

    await chat_proto._handle_confirm(ctx, sender, json.dumps({"action": "cancel"}), state)

    assert _cards(ctx, "form"), "cancel should re-show the intake form"
    assert get_state(ctx, sender)["stage"] == INTAKE
