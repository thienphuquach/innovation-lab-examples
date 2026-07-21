"""Stage 4 - fare engine + route/fare detail card dispatch."""

from __future__ import annotations

import json

import pytest
from uagents_core.contrib.protocols.chat import ChatMessage, MetadataContent

import chat_proto
from clients.five11 import FareData, LegRule, Product, TransferRule
from fares import compute_fare_options
from session_state import AWAITING_CONFIRM, SHOWING_DETAIL, SHOWING_ROUTES, get_state


def _p(amount, media, *, is_pass=False, rider="adult"):
    return Product(amount=amount, currency="USD", rider=rider, media=media, is_pass=is_pass, name="p")


def _two_network_fd() -> FareData:
    """Muni (flat) + AC Transit, with a Clipper-only Muni->AC transfer discount."""
    return FareData(
        route_network={"SF:1": "muni", "AC:51": "actransit"},
        leg_rules={
            "muni": [LegRule("", "", "muni_grp", "muni-single", False)],
            "actransit": [LegRule("", "", "ac_grp", "ac-single", False)],
        },
        products={
            "muni-single": [_p(2.50, "clipper"), _p(3.00, "cash")],
            "ac-single": [_p(2.50, "clipper"), _p(2.50, "cash")],
            "xfer-ac": [_p(0.75, "clipper")],
        },
        transfer_rules=[TransferRule("muni_grp", "ac_grp", "xfer-ac")],
    )


def _legs(*route_ids):
    return [{"mode": "WALK", "transitLeg": False}] + [
        {"mode": "BUS", "transitLeg": True, "routeId": rid, "agencyId": rid.split(":")[0]}
        for rid in route_ids
    ]


def test_clipper_beats_cash_via_transfer_discount():
    fd = _two_network_fd()
    opts = compute_fare_options(_legs("SF:1", "AC:51"), fd)
    by_id = {o.id: o for o in opts}
    assert opts[0].id == "clipper"  # cheapest first
    assert by_id["clipper"].amount == 3.25  # 2.50 + 0.75 transfer
    assert by_id["cash"].amount == 5.50  # 3.00 + 2.50, no cash transfer rule
    assert by_id["clipper"].estimated is True  # a transfer discount was applied


def test_walking_only_has_no_fare_options():
    assert compute_fare_options([{"mode": "WALK", "transitLeg": False}], _two_network_fd()) == []


def test_single_network_offers_day_pass():
    fd = _two_network_fd()
    fd.leg_rules["muni"].append(LegRule("", "", "muni_grp", "muni-1-day", False))
    fd.products["muni-1-day"] = [_p(5.00, "clipper", is_pass=True)]
    opts = compute_fare_options(_legs("SF:1"), fd)
    assert "daypass" in {o.id for o in opts}


def test_distance_based_network_is_estimated():
    rules = [LegRule("", "", "bart_grp", f"bart-{i}", False) for i in range(40)]
    products = {f"bart-{i}": [_p(2.0 + i, "clipper")] for i in range(40)}
    fd = FareData(
        route_network={"BA:R": "bart"}, leg_rules={"bart": rules}, products=products
    )
    opts = compute_fare_options(_legs("BA:R"), fd)
    assert opts and opts[0].estimated is True


# ── Stage 4 dispatch ─────────────────────────────────────────────────────────
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
    state["stage"] = SHOWING_ROUTES
    state["trip"] = {"origin_text": "A", "destination_text": "B", "priority": "fastest"}
    state["last_itineraries"] = {
        "itineraries": [
            {
                "duration": 1611,
                "transfers": 1,
                "startTime": 1_714_784_876_000,
                "endTime": 1_714_786_487_000,
                "legs": _legs("SF:1", "AC:51"),
            }
        ]
    }
    state["selected_route_id"] = "0"
    return state


@pytest.mark.asyncio
async def test_show_detail_renders_fares_and_alert(ctx, sender, monkeypatch):
    async def fake_fd():
        return _two_network_fd()

    async def fake_alerts(route_ids, agency_ids):
        return ["Delays on Line 1"]

    monkeypatch.setattr(chat_proto, "load_fare_data", fake_fd)
    monkeypatch.setattr(chat_proto, "alerts_for_routes", fake_alerts)

    state = _detail_state(ctx, sender)
    await chat_proto._show_detail(ctx, sender, state)

    metas = _cards(ctx, "detail")
    assert metas
    payload = json.loads(metas[0]["card_payload"])
    assert payload["sub_options"]["choices"][0]["value"] == "clipper"
    assert any(row["label"] == "⚠ Alert" for row in payload["summary_rows"])
    saved = get_state(ctx, sender)
    assert saved["stage"] == SHOWING_DETAIL
    assert saved["selected_fare_option"] == "clipper"


@pytest.mark.asyncio
async def test_detail_back_returns_to_carousel(ctx, sender):
    state = _detail_state(ctx, sender)
    state["stage"] = SHOWING_DETAIL
    await chat_proto._handle_detail(
        ctx, sender, json.dumps({"action": "back_to_routes"}), state
    )
    assert _cards(ctx, "carousel")
    assert get_state(ctx, sender)["stage"] == SHOWING_ROUTES


@pytest.mark.asyncio
async def test_detail_continue_advances_to_confirm(ctx, sender):
    state = _detail_state(ctx, sender)
    state["stage"] = SHOWING_DETAIL
    await chat_proto._handle_detail(
        ctx, sender, json.dumps({"action": "continue_review", "fare": "cash"}), state
    )
    saved = get_state(ctx, sender)
    assert saved["stage"] == AWAITING_CONFIRM
    assert saved["selected_fare_option"] == "cash"
