"""Stage 3 - route search carousel + selection dispatch."""

from __future__ import annotations

import json

import pytest
from uagents_core.contrib.protocols.chat import ChatMessage, MetadataContent

import chat_proto
from cards import route_carousel_card
from clients.transitland import RoutingError
from session_state import INTAKE, SHOWING_DETAIL, SHOWING_ROUTES, get_state

pytestmark = pytest.mark.asyncio


def _fake_plan(n_transit=1, wait=0):
    legs = [{"mode": "WALK", "transitLeg": False}]
    for j in range(n_transit):
        legs.append(
            {"mode": "RAIL", "transitLeg": True, "routeShortName": f"R{j}", "agencyName": "BART"}
        )
    return {
        "itineraries": [
            {
                "duration": 1611,
                "transfers": 0,
                "waitingTime": 0,
                "startTime": 1_714_784_876_000,
                "endTime": 1_714_786_487_000,
                "legs": legs,
            },
            {
                "duration": 2331,
                "transfers": 2,
                "waitingTime": wait,
                "startTime": 1_714_784_876_000,
                "endTime": 1_714_787_207_000,
                "legs": legs + [{"mode": "BUS", "transitLeg": True, "routeShortName": "51A"}],
            },
        ]
    }


def _routes_state(ctx, sender):
    state = get_state(ctx, sender)
    state["paid"] = True
    state["stage"] = SHOWING_ROUTES
    state["trip"] = {
        "origin_text": "Berkeley",
        "origin_coords": [37.87, -122.27],
        "destination_text": "Powell St",
        "destination_coords": [37.78, -122.41],
        "depart_time": None,
        "priority": "fastest",
    }
    return state


def _cards(ctx, kind=None):
    out = []
    for _, m in ctx.sent:
        if isinstance(m, ChatMessage):
            for c in m.content:
                if isinstance(c, MetadataContent) and (kind is None or c.metadata["card_kind"] == kind):
                    out.append(c.metadata)
    return out


async def test_search_routes_renders_carousel_and_caches(ctx, sender, monkeypatch):
    async def fake_plan(o, d, t, **k):
        return _fake_plan()

    monkeypatch.setattr(chat_proto, "plan", fake_plan)
    state = _routes_state(ctx, sender)
    await chat_proto._search_routes(ctx, sender, state)

    assert _cards(ctx, "carousel")
    saved = get_state(ctx, sender)
    assert saved["stage"] == SHOWING_ROUTES
    assert saved["last_itineraries"]["itineraries"]


async def test_routing_error_shows_error_card(ctx, sender, monkeypatch):
    async def boom(o, d, t, **k):
        raise RoutingError("timeout")

    monkeypatch.setattr(chat_proto, "plan", boom)
    state = _routes_state(ctx, sender)
    await chat_proto._search_routes(ctx, sender, state)

    metas = _cards(ctx, "detail")
    assert metas, "expected a routing-error detail card"
    payload = json.loads(metas[0]["card_payload"])
    assert any(cta["selection"]["action"] == "retry_routing" for cta in payload.get("ctas", []))


async def test_zero_itineraries_terminal_and_reshow(ctx, sender, monkeypatch):
    async def empty(o, d, t, **k):
        return {"itineraries": []}

    monkeypatch.setattr(chat_proto, "plan", empty)
    state = _routes_state(ctx, sender)
    await chat_proto._search_routes(ctx, sender, state)

    assert _cards(ctx, "detail"), "expected terminal no-routes card"
    assert _cards(ctx, "form"), "expected the intake form to be re-shown"
    assert get_state(ctx, sender)["stage"] == INTAKE


async def test_pick_route_advances_to_detail(ctx, sender, monkeypatch):
    async def fake_plan(o, d, t, **k):
        return _fake_plan()

    async def fake_alerts(route_ids, agency_ids):
        return []

    monkeypatch.setattr(chat_proto, "plan", fake_plan)
    # Stage 4 now runs on selection; stub its network boundaries.
    monkeypatch.setattr(chat_proto, "load_fare_data", _boom_fare_data)
    monkeypatch.setattr(chat_proto, "alerts_for_routes", fake_alerts)
    state = _routes_state(ctx, sender)
    await chat_proto._search_routes(ctx, sender, state)
    ctx.sent.clear()

    state = get_state(ctx, sender)
    await chat_proto._handle_routes(
        ctx, sender, json.dumps({"action": "pick_route", "route_index": 1}), state
    )
    saved = get_state(ctx, sender)
    assert saved["stage"] == SHOWING_DETAIL
    assert saved["selected_route_id"] == "1"


async def _boom_fare_data():
    raise RuntimeError("no network in tests")


async def test_long_wait_earns_warning_badge():
    plan = _fake_plan(wait=90 * 60)  # 90-minute wait on itinerary #1
    meta = route_carousel_card(plan["itineraries"], "fastest")
    payload = json.loads(meta["card_payload"])
    badges_1 = [b["variant"] for b in payload["items"][1]["badges"]]
    assert "warning" in badges_1
    # The fast, no-wait option is badged Fastest, not warning.
    assert any(b["label"] == "Fastest" for b in payload["items"][0]["badges"])
