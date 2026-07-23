"""Card-builder unit tests for the diagnosis.md fixes that don't need a running
agent/stage dispatch - label disambiguation (issue 3), per-leg instructions and
headsigns (issues 4 & 5), and the zero-routes recovery card (issue 2)."""

from __future__ import annotations

import json

from cards import (
    _leg_step_items,
    _short_label,
    disambiguation_carousel_card,
    no_routes_recovery_card,
    route_carousel_card,
    route_title,
    route_walkthrough_card,
)
from models import GeocodeCandidate


# ── Issue 3: disambiguation candidates must be distinguishable ───────────────
def test_short_label_surfaces_kind_when_known():
    title, subtitle = _short_label("San Francisco International Airport, California", "aeroway/aerodrome")
    assert title == "San Francisco International Airport"
    assert "Airport" in subtitle


def test_short_label_keeps_more_address_context_than_a_bare_3_part_cut():
    label = "Golden Gate Bridge, San Francisco, Marin County, California, 94129"
    _, subtitle = _short_label(label, "")
    # The zip code four segments in must survive - the whole point of issue 3 is
    # that a 3-part truncation was hiding exactly this kind of differentiator.
    assert "94129" in subtitle


def test_disambiguation_carousel_distinguishes_same_named_candidates():
    candidates = [
        GeocodeCandidate("San Francisco International Airport, 780, South Airport Boulevard, San Francisco", 37.6, -122.38, "aeroway/aerodrome"),
        GeocodeCandidate("San Francisco International Airport, 780, North Link Road, Lomita Park", 37.6, -122.39, "highway/motorway"),
    ]
    payload = json.loads(disambiguation_carousel_card("origin", candidates)["card_payload"])
    subtitles = [item["subtitle"] for item in payload["items"]]
    assert len(set(subtitles)) == 2, f"expected distinguishable subtitles, got {subtitles!r}"
    assert "Airport" in subtitles[0]
    assert "Road" in subtitles[1]


# ── Issue 5: boarding direction must be visible on the route-options card ────
def test_route_carousel_shows_boarding_headsign():
    itinerary = {
        "duration": 1600,
        "transfers": 0,
        "waitingTime": 60,
        "startTime": 0,
        "endTime": 1600000,
        "legs": [
            {
                "mode": "RAIL",
                "transitLeg": True,
                "routeShortName": "Yellow-N",
                "headsign": "Richmond",
                "from": {"name": "Powell St"},
                "to": {"name": "MacArthur"},
                "startTime": 0,
                "endTime": 1600000,
            }
        ],
    }
    payload = json.loads(route_carousel_card([itinerary], "fastest")["card_payload"])
    assert "Board toward Richmond" in payload["items"][0]["subtitle"]


# ── Issue A: route titles must be plain-language, not line-color jargon ──────
def test_route_title_uses_agency_and_mode_not_routeshortname():
    itinerary = {
        "legs": [
            {"mode": "WALK", "transitLeg": False},
            {"mode": "RAIL", "transitLeg": True, "routeShortName": "Red-S", "agencyName": "Bay Area Rapid Transit"},
        ]
    }
    assert route_title(itinerary) == "BART train"


def test_route_title_collapses_consecutive_same_agency_legs():
    """A transfer between two BART lines is still just "BART" - the transfer
    count is already shown elsewhere, so repeating the label twice adds noise
    without adding information."""
    itinerary = {
        "legs": [
            {"mode": "RAIL", "transitLeg": True, "routeShortName": "Orange-S", "agencyName": "Bay Area Rapid Transit"},
            {"mode": "RAIL", "transitLeg": True, "routeShortName": "Yellow-S", "agencyName": "Bay Area Rapid Transit"},
        ]
    }
    assert route_title(itinerary) == "BART train"


def test_route_title_distinguishes_different_agencies():
    itinerary = {
        "legs": [
            {"mode": "RAIL", "transitLeg": True, "routeShortName": "Red-S", "agencyName": "Bay Area Rapid Transit"},
            {"mode": "BUS", "transitLeg": True, "routeShortName": "49", "agencyName": "San Francisco Municipal Transportation Agency"},
        ]
    }
    assert route_title(itinerary) == "BART train → Muni bus"


# ── Issues B & C: the walkthrough must read as a sequence, available pre-confirm ─
def test_leg_step_items_lead_with_plain_language_and_keep_the_route_code():
    itinerary = {
        "legs": [
            {"mode": "WALK", "transitLeg": False, "from": {}, "to": {"name": "Powell St"}, "duration": 300},
            {
                "mode": "RAIL",
                "transitLeg": True,
                "routeShortName": "Yellow-N",
                "agencyName": "Bay Area Rapid Transit",
                "headsign": "Richmond",
                "from": {"name": "Powell St"},
                "to": {"name": "MacArthur"},
                "startTime": 0,
                "endTime": 900000,
            },
        ]
    }
    items = _leg_step_items(itinerary, leg_amounts=[2.50])
    assert len(items) == 2  # one item per leg, in trip order - a sequence, not a flat table
    walk_item, board_item = items
    assert any(c["type"] == "text" and "Powell St" in c["value"] for c in walk_item["children"])

    heading = next(c for c in board_item["children"] if c["type"] == "heading")
    assert heading["value"] == "Board BART train toward Richmond"  # plain language, issue A
    detail = next(c for c in board_item["children"] if c["type"] == "text")
    assert "Powell St" in detail["value"] and "MacArthur" in detail["value"] and "$2.50" in detail["value"]
    badge = next(c for c in board_item["children"] if c["type"] == "badge")
    assert badge["label"] == "Yellow-N"  # the actual route code, kept for boarding time


def test_route_walkthrough_card_is_a_terminal_custom_list_sequence():
    """The walkthrough is its own card, sent separately from the fare/confirm
    decision (issue B: sequence content shouldn't share a flat row list with a
    payment choice) - and it's informational only (issue C: it goes out before
    the user has to decide anything, not as a post-confirm recap)."""
    itinerary = {
        "legs": [
            {
                "mode": "RAIL", "transitLeg": True, "routeShortName": "R", "agencyName": "BART",
                "from": {"name": "A"}, "to": {"name": "B"}, "startTime": 0, "endTime": 900000,
            },
        ]
    }
    meta = route_walkthrough_card(itinerary, ["Delay on Line 1"])
    assert meta["card_kind"] == "custom"
    assert meta["is_terminal"] == "true"
    payload = json.loads(meta["card_payload"])
    root = payload["root"]
    assert root["type"] == "section"
    kinds = [child["type"] for child in root["children"]]
    assert "badge" in kinds  # the live alert
    assert "list" in kinds  # the leg-by-leg sequence


# ── Issue 2: zero-route recovery must name the actual failed trip ────────────
def test_no_routes_recovery_card_names_the_trip_and_offers_next_steps():
    payload = json.loads(no_routes_recovery_card("Berkeley", "Powell St")["card_payload"])
    values = {row["label"]: row["value"] for row in payload["summary_rows"]}
    assert values == {"From": "Berkeley", "To": "Powell St"}
    actions = {cta["selection"]["action"] for cta in payload["ctas"]}
    assert actions == {"retry_later", "new_trip"}
