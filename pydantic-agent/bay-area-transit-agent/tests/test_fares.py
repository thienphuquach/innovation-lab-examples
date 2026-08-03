"""Stage 4 - fare engine + route/fare detail card dispatch."""

from __future__ import annotations

import json

import pytest
from uagents_core.contrib.protocols.chat import (
    ChatMessage,
    MetadataContent,
    TextContent,
)

import chat_proto
from clients.five11 import FareData, LegRule, Product, TransferRule
from fares import compute_fare_options
from session_state import AWAITING_CONFIRM, SHOWING_DETAIL, SHOWING_ROUTES, get_state


def _p(amount, media, *, is_pass=False, rider="adult"):
    return Product(amount=amount, currency="USD", rider=rider, media=media, is_pass=is_pass, name="p")


def _two_network_fd() -> FareData:
    """Muni (flat) + AC Transit, with a tap-only Muni->AC transfer discount.

    Cash rows are present exactly as the real 511 feed carries them, to prove they
    no longer influence anything: cash isn't a modelled payment method.
    """
    return FareData(
        route_network={"SF:1": "muni", "AC:51": "actransit"},
        leg_rules={
            "muni": [LegRule("", "", "muni_grp", "muni-single", False)],
            "actransit": [LegRule("", "", "ac_grp", "ac-single", False)],
        },
        products={
            "muni-single": [_p(2.50, "clipper"), _p(2.50, "contactless"), _p(3.00, "cash")],
            "ac-single": [_p(2.50, "clipper"), _p(2.50, "contactless"), _p(2.50, "cash")],
            "xfer-ac": [_p(0.75, "clipper")],
        },
        transfer_rules=[TransferRule("muni_grp", "ac_grp", "xfer-ac")],
    )


def _legs(*route_ids):
    return [{"mode": "WALK", "transitLeg": False}] + [
        {"mode": "BUS", "transitLeg": True, "routeId": rid, "agencyId": rid.split(":")[0]}
        for rid in route_ids
    ]


def test_transfer_discount_applies_and_both_taps_cost_the_same():
    """Since Clipper 2.0 a Clipper card and a contactless bank card are the same
    fare, so the two options differ only in label - never in price."""
    fd = _two_network_fd()
    opts, notes = compute_fare_options(_legs("SF:1", "AC:51"), fd)
    assert [o.id for o in opts] == ["clipper", "contactless"]
    assert {o.amount for o in opts} == {3.25}  # 2.50 + 0.75 transfer, both ways
    assert all(o.estimated for o in opts)  # a transfer discount was applied
    assert notes == []


def test_cash_is_not_a_payment_option_even_when_the_feed_prices_it():
    fd = _two_network_fd()
    opts, _ = compute_fare_options(_legs("SF:1"), fd)
    assert {o.id for o in opts} == {"clipper", "contactless"}
    # $3.00 is the feed's cash price for this leg; the tap price is what's shown.
    assert {o.amount for o in opts} == {2.50}


def test_walking_only_has_no_fare_options():
    assert compute_fare_options([{"mode": "WALK", "transitLeg": False}], _two_network_fd()) == ([], [])


def test_day_pass_is_not_offered_even_on_a_single_operator_trip():
    fd = _two_network_fd()
    fd.leg_rules["muni"].append(LegRule("", "", "muni_grp", "muni-1-day", False))
    fd.products["muni-1-day"] = [_p(5.00, "clipper", is_pass=True)]
    opts, notes = compute_fare_options(_legs("SF:1"), fd)
    assert {o.id for o in opts} == {"clipper", "contactless"}
    # The pass neither appears as an option nor leaks into the single-ride price.
    assert {o.amount for o in opts} == {2.50}
    assert notes == []


def test_distance_based_network_is_estimated():
    rules = [LegRule("", "", "bart_grp", f"bart-{i}", False) for i in range(40)]
    products = {f"bart-{i}": [_p(2.0 + i, "clipper")] for i in range(40)}
    fd = FareData(
        route_network={"BA:R": "bart"}, leg_rules={"bart": rules}, products=products
    )
    opts, _ = compute_fare_options(_legs("BA:R"), fd)
    assert opts and opts[0].estimated is True


def test_unpriceable_leg_no_longer_kills_the_whole_option():
    """A leg with zero fare products at all (e.g. a free shuttle) is treated as a
    $0/included leg, not a reason to discard every payment method for the whole
    itinerary (diagnosis.md issue 6)."""
    fd = _two_network_fd()
    fd.route_network["SI:Shuttle"] = "shuttle"  # no leg_rules/products for "shuttle" at all
    opts, _ = compute_fare_options(_legs("SI:Shuttle", "SF:1"), fd)
    by_id = {o.id: o for o in opts}
    assert "clipper" in by_id
    assert by_id["clipper"].amount == 2.50  # just the Muni leg; shuttle leg contributes $0
    assert by_id["clipper"].unpriced_legs == 1
    assert by_id["clipper"].leg_amounts == [None, 2.50]


def test_untappable_operator_yields_no_options_and_says_why():
    """A cash-only operator (real examples: Capitol Corridor, ACE, the small
    ferries) can't be tapped through, so there is honestly nothing to offer - but
    the rider must be told that, not left with a silently empty card."""
    fd = _two_network_fd()
    fd.products["ac-single"] = [_p(2.50, "cash")]  # AC Transit: cash-only now
    opts, notes = compute_fare_options(_legs("SF:1", "AC:51"), fd)
    assert opts == []
    assert any("actransit" in n.lower() and "contactless" in n.lower() for n in notes)


# ── Stage 4 dispatch ─────────────────────────────────────────────────────────
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

    # The walkthrough (custom card) carries live alerts; the fare/confirm
    # (detail card) is just the payment decision (ux-diagnosis.md issue B).
    walkthrough = _cards(ctx, "custom")
    assert walkthrough
    walkthrough_payload = json.loads(walkthrough[0]["card_payload"])
    badges = [c["label"] for c in walkthrough_payload["root"]["children"] if c["type"] == "badge"]
    assert any("Delays on Line 1" in b for b in badges)

    metas = _cards(ctx, "detail")
    assert metas
    payload = json.loads(metas[0]["card_payload"])
    assert payload["sub_options"]["choices"][0]["value"] == "clipper"
    saved = get_state(ctx, sender)
    assert saved["stage"] == SHOWING_DETAIL
    assert saved["selected_fare_option"] == "clipper"
    # Alerts must survive past this stage - final_itinerary_card reads them back
    # out of state at confirm time (test_review.py), not just this card's own send.
    assert saved["alerts"] == ["Delays on Line 1"]
    # New-to-Clipper guidance (physical/mobile card link) rides in the narration
    # as its own short line - not a card row (a link + full sentence wraps
    # across several lines there) and not folded into one long paragraph.
    fare_texts = [t for t in _texts(ctx) if "payment options" in t.lower()]
    assert fare_texts
    lines = fare_texts[0].split("\n")
    assert any("clippercard.com/get" in line for line in lines)
    assert all(len(line) < 120 for line in lines)


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
        ctx, sender, json.dumps({"action": "continue_review", "fare": "contactless"}), state
    )
    saved = get_state(ctx, sender)
    assert saved["stage"] == AWAITING_CONFIRM
    assert saved["selected_fare_option"] == "contactless"
