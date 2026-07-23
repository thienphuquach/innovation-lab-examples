"""Card-builder unit tests for the diagnosis.md fixes that don't need a running
agent/stage dispatch - label disambiguation (issue 3), per-leg instructions and
headsigns (issues 4 & 5), and the zero-routes recovery card (issue 2)."""

from __future__ import annotations

import json

from cards import (
    _leg_instruction_rows,
    _short_label,
    disambiguation_carousel_card,
    no_routes_recovery_card,
    route_carousel_card,
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


# ── Issues 4 & 6: per-leg board/alight/fare instructions ─────────────────────
def test_leg_instruction_rows_include_stops_headsign_and_fare():
    itinerary = {
        "legs": [
            {"mode": "WALK", "transitLeg": False, "from": {}, "to": {"name": "Powell St"}, "duration": 300},
            {
                "mode": "RAIL",
                "transitLeg": True,
                "routeShortName": "Yellow-N",
                "headsign": "Richmond",
                "from": {"name": "Powell St"},
                "to": {"name": "MacArthur"},
                "startTime": 0,
                "endTime": 900000,
            },
        ]
    }
    rows = _leg_instruction_rows(itinerary, leg_amounts=[2.50])
    board_row = next(r for r in rows if r["label"].startswith("Board"))
    assert "toward Richmond" in board_row["label"]
    assert "Powell St" in board_row["value"] and "MacArthur" in board_row["value"]
    assert "$2.50" in board_row["value"]


# ── Issue 2: zero-route recovery must name the actual failed trip ────────────
def test_no_routes_recovery_card_names_the_trip_and_offers_next_steps():
    payload = json.loads(no_routes_recovery_card("Berkeley", "Powell St")["card_payload"])
    values = {row["label"]: row["value"] for row in payload["summary_rows"]}
    assert values == {"From": "Berkeley", "To": "Powell St"}
    actions = {cta["selection"]["action"] for cta in payload["ctas"]}
    assert actions == {"retry_later", "new_trip"}
