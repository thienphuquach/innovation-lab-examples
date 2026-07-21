"""Stage 2/2.5 - trip intake (form + free text) and geocoding resolution."""

from __future__ import annotations

import json

import pytest
from uagents_core.contrib.protocols.chat import ChatMessage, MetadataContent

import chat_proto
from ai import TripExtraction, extract_trip
from models import GeocodeCandidate
from session_state import INTAKE, SHOWING_ROUTES, get_state

pytestmark = pytest.mark.asyncio


def _paid_intake_state(ctx, sender):
    state = get_state(ctx, sender)
    state["paid"] = True
    state["stage"] = INTAKE
    return state


def _cards(ctx, kind: str | None = None):
    out = []
    for _, m in ctx.sent:
        if isinstance(m, ChatMessage):
            for c in m.content:
                if isinstance(c, MetadataContent):
                    if kind is None or c.metadata.get("card_kind") == kind:
                        out.append(c.metadata)
    return out


async def test_form_submit_resolves_and_finalizes(ctx, sender, monkeypatch):
    monkeypatch.setattr(
        chat_proto,
        "resolve_place",
        lambda text: _resolved(text),
    )
    state = _paid_intake_state(ctx, sender)
    sel = json.dumps(
        {
            "action": "submit_trip",
            "origin": "Berkeley",
            "destination": "Mission",
            "depart_option": "now",
            "priority": "cheapest",
        }
    )
    await chat_proto._handle_intake(ctx, sender, sel, state)

    saved = get_state(ctx, sender)
    assert saved["stage"] == SHOWING_ROUTES
    assert saved["trip"]["origin_coords"] and saved["trip"]["destination_coords"]
    assert saved["trip"]["priority"] == "cheapest"
    assert saved["pending_trip"] is None


async def test_form_same_origin_destination_reshows_form(ctx, sender):
    state = _paid_intake_state(ctx, sender)
    sel = json.dumps(
        {
            "action": "submit_trip",
            "origin": "SF",
            "destination": "sf",
            "depart_option": "now",
            "priority": "fastest",
        }
    )
    await chat_proto._handle_intake(ctx, sender, sel, state)
    # Re-rendered the form, did not advance.
    assert _cards(ctx, "form")
    assert get_state(ctx, sender)["stage"] == INTAKE


async def test_free_text_path_uses_extraction(ctx, sender, monkeypatch):
    async def fake_extract(text):
        return TripExtraction(
            origin="Berkeley", destination="Mission", depart_time_iso=None, priority="fastest"
        )

    monkeypatch.setattr(chat_proto, "extract_trip", fake_extract)
    monkeypatch.setattr(chat_proto, "resolve_place", lambda text: _resolved(text))
    state = _paid_intake_state(ctx, sender)

    await chat_proto._handle_intake(ctx, sender, "get me from berkeley to the mission", state)

    saved = get_state(ctx, sender)
    assert saved["stage"] == SHOWING_ROUTES
    assert saved["trip"]["origin_text"] and saved["trip"]["destination_text"]


async def test_ambiguous_origin_shows_carousel_then_pick(ctx, sender, monkeypatch):
    async def resolve(text):
        if "berkeley" in text.lower():
            return "ambiguous", [
                GeocodeCandidate("Berkeley, CA", 37.87, -122.27),
                GeocodeCandidate("Berkeley, MO", 38.7, -90.3),
            ]
        return "resolved", [GeocodeCandidate("Mission, SF", 37.76, -122.41)]

    monkeypatch.setattr(chat_proto, "resolve_place", resolve)
    state = _paid_intake_state(ctx, sender)
    sel = json.dumps(
        {
            "action": "submit_trip",
            "origin": "Berkeley",
            "destination": "Mission",
            "depart_option": "now",
            "priority": "fastest",
        }
    )
    await chat_proto._handle_intake(ctx, sender, sel, state)
    assert _cards(ctx, "carousel"), "expected a disambiguation carousel"
    saved = get_state(ctx, sender)
    assert saved["pending_trip"]["origin_coords"] is None

    # User picks the CA Berkeley.
    ctx.sent.clear()
    pick = json.dumps(
        {"action": "pick_place", "field": "origin", "lat": 37.87, "lon": -122.27, "label": "Berkeley, CA"}
    )
    state = get_state(ctx, sender)
    await chat_proto._handle_intake(ctx, sender, pick, state)
    saved = get_state(ctx, sender)
    assert saved["stage"] == SHOWING_ROUTES
    assert saved["trip"]["origin_coords"] == [37.87, -122.27]


async def test_not_found_shows_terminal_and_reshows_form(ctx, sender, monkeypatch):
    async def resolve(text):
        return "not_found", []

    monkeypatch.setattr(chat_proto, "resolve_place", resolve)
    state = _paid_intake_state(ctx, sender)
    sel = json.dumps(
        {
            "action": "submit_trip",
            "origin": "asdkfjqwoeixyz",
            "destination": "Mission",
            "depart_option": "now",
            "priority": "fastest",
        }
    )
    await chat_proto._handle_intake(ctx, sender, sel, state)
    assert _cards(ctx, "detail"), "expected a terminal not-found card"
    assert _cards(ctx, "form"), "expected the form to be re-shown"
    assert get_state(ctx, sender)["stage"] == INTAKE


async def test_extract_trip_wiring_with_testmodel():
    """The Pydantic AI extraction agent returns a typed TripExtraction."""
    from pydantic_ai.models.test import TestModel

    tm = TestModel(
        custom_output_args={
            "origin": "Berkeley",
            "destination": "Mission",
            "depart_time_iso": None,
            "priority": "cheapest",
        }
    )
    out = await extract_trip("berkeley to the mission, cheapest", model=tm)
    assert out.origin == "Berkeley" and out.destination == "Mission"
    assert out.priority == "cheapest"


# ── helpers ──────────────────────────────────────────────────────────────────
async def _resolved(text):
    """A single confident candidate keyed loosely off the query text."""
    lat = 37.87 if "berkeley" in text.lower() else 37.76
    lon = -122.27 if "berkeley" in text.lower() else -122.41
    return "resolved", [GeocodeCandidate(text, lat, lon)]
