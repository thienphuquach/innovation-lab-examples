"""Card-builder unit tests for the diagnosis.md fixes that don't need a running
agent/stage dispatch - label disambiguation (issue 3), per-leg instructions and
headsigns (issues 4 & 5), and the zero-routes recovery card (issue 2)."""

from __future__ import annotations

import json

from cards import (
    _leg_step_items,
    _requires_tap_off,
    _short_label,
    disambiguation_carousel_card,
    fare_narration_lines,
    final_itinerary_card,
    intake_form_card,
    no_fare_labels,
    no_routes_recovery_card,
    route_carousel_card,
    route_detail_card,
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


# ── Clipper guidance: tap-on/tap-off policy varies by agency, not by leg cost ─
def test_distance_priced_agencies_require_tap_off():
    """BART, Caltrain and Golden Gate Transit charge by distance/zone, so a
    second tap at exit is how the correct (lower) fare gets applied - confirmed
    against Clipper's own rider FAQ and each agency's fare guide."""
    assert _requires_tap_off({"agencyName": "Bay Area Rapid Transit"})
    assert _requires_tap_off({"agencyName": "Golden Gate Transit"})
    assert not _requires_tap_off({"agencyName": "San Francisco Municipal Transportation Agency"})
    assert not _requires_tap_off({"agencyName": "AC TRANSIT"})


def test_golden_gate_ferry_is_single_tap_unlike_the_bus():
    """Same district, different brand, different policy - the ferry must not
    match on a bare "golden gate" substring."""
    assert not _requires_tap_off({"agencyName": "Golden Gate Ferry"})


def test_leg_step_items_flag_tap_off_only_for_distance_priced_legs():
    bart_leg = {
        "mode": "RAIL", "transitLeg": True, "routeShortName": "R",
        "agencyName": "Bay Area Rapid Transit",
        "from": {"name": "A"}, "to": {"name": "B"}, "startTime": 0, "endTime": 900000,
    }
    muni_leg = {
        "mode": "BUS", "transitLeg": True, "routeShortName": "49",
        "agencyName": "San Francisco Municipal Transportation Agency",
        "from": {"name": "B"}, "to": {"name": "C"}, "startTime": 900000, "endTime": 1800000,
    }
    bart_item, muni_item = _leg_step_items({"legs": [bart_leg, muni_leg]})
    assert any("Tap off" in c["value"] for c in bart_item["children"] if c["type"] == "text")
    assert not any("Tap off" in c["value"] for c in muni_item["children"] if c["type"] == "text")


def test_route_detail_card_carries_no_tap_off_or_clipper_rows():
    """This detail/copy now lives in the narration text (chat_proto._show_detail),
    not the card - a link plus a full sentence wraps across several lines inside
    a table cell, which reads as a wall of text within the card itself."""
    itinerary = {
        "legs": [
            {
                "mode": "RAIL", "transitLeg": True, "routeShortName": "R",
                "agencyName": "Bay Area Rapid Transit",
                "from": {"name": "A"}, "to": {"name": "B"}, "startTime": 0, "endTime": 900000,
            },
        ]
    }
    choices = [{"value": "clipper", "label": "Clipper card", "secondary_text": "$5.00"}]
    payload = json.loads(route_detail_card(itinerary, choices)["card_payload"])
    labels = {r["label"] for r in payload["summary_rows"]}
    assert "Tap off at" not in labels
    assert "New to Clipper?" not in labels


def test_fare_narration_lines_names_only_the_agencies_that_actually_need_a_tap_off():
    """Each fact is its own short, standalone sentence - specific to this
    itinerary's own legs, not a generic disclaimer the rider has to map onto
    their own trip."""
    itinerary = {
        "legs": [
            {
                "mode": "RAIL", "transitLeg": True, "routeShortName": "R",
                "agencyName": "Bay Area Rapid Transit",
                "from": {"name": "A"}, "to": {"name": "B"}, "startTime": 0, "endTime": 900000,
            },
            {
                "mode": "BUS", "transitLeg": True, "routeShortName": "49",
                "agencyName": "San Francisco Municipal Transportation Agency",
                "from": {"name": "B"}, "to": {"name": "C"}, "startTime": 900000, "endTime": 1800000,
            },
        ]
    }
    choices = [{"value": "clipper", "label": "Clipper card", "secondary_text": "$5.00"}]
    lines = fare_narration_lines(itinerary, choices)

    tap_off_line = next(line for line in lines if "tap off" in line.lower())
    assert "BART" in tap_off_line
    assert "Muni" not in tap_off_line
    # "Not required" and the get-a-card link are two standalone facts, not one
    # blob - each is its own short sentence rather than a paragraph.
    assert any("Not required" in line for line in lines)
    assert any("clippercard.com/get" in line for line in lines)
    assert all(len(line) < 120 for line in lines)


def test_fare_narration_lines_has_no_tap_off_line_when_nothing_requires_it():
    itinerary = {
        "legs": [
            {
                "mode": "BUS", "transitLeg": True, "routeShortName": "49",
                "agencyName": "San Francisco Municipal Transportation Agency",
                "from": {"name": "A"}, "to": {"name": "B"}, "startTime": 0, "endTime": 900000,
            },
        ]
    }
    choices = [{"value": "clipper", "label": "Clipper card", "secondary_text": "$2.50"}]
    lines = fare_narration_lines(itinerary, choices)
    assert not any("Tap off" in line for line in lines)
    assert any("Clipper" in line for line in lines)


def test_fare_narration_lines_has_no_clipper_line_when_no_fare_choices():
    itinerary = {"legs": []}
    assert fare_narration_lines(itinerary, []) == []


# ── Issue: alerts seen at Stage 4 must not silently vanish by confirm time ────
def test_final_itinerary_card_carries_alerts_forward():
    itinerary = {
        "legs": [
            {
                "mode": "RAIL", "transitLeg": True, "routeShortName": "R", "agencyName": "BART",
                "from": {"name": "A"}, "to": {"name": "B"}, "startTime": 0, "endTime": 900000,
            },
        ]
    }
    meta = final_itinerary_card(itinerary, "Clipper card", "$2.50", alerts=["Delay on Line 1"])
    payload = json.loads(meta["card_payload"])
    kinds = [child["type"] for child in payload["root"]["children"]]
    assert "badge" in kinds  # the carried-forward alert, not just dropped


def test_final_itinerary_card_alerts_default_to_none_without_error():
    itinerary = {"legs": []}
    meta = final_itinerary_card(itinerary, None, None)
    assert json.loads(meta["card_payload"])["root"]["title"]


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
    assert values["From"] == "Berkeley"
    assert values["To"] == "Powell St"
    actions = {cta["selection"]["action"] for cta in payload["ctas"]}
    assert actions == {"new_trip"}


# ── Every search departs now: no depart-time input anywhere ──────────────────
def test_intake_form_collects_only_origin_destination_and_priority():
    payload = json.loads(intake_form_card()["card_payload"])
    assert [f["name"] for f in payload["fields"]] == ["origin", "destination", "priority"]


def test_no_fare_labels_never_calls_a_transit_trip_free():
    """Cash is no longer modelled, so an unpriceable transit trip is a real case -
    it must not inherit the walking-only "free" label."""
    walk_only = {"legs": [{"mode": "WALK", "transitLeg": False}]}
    assert no_fare_labels(walk_only) == ("Walking - free", "$0.00")

    has_transit = {"legs": [{"mode": "RAIL", "transitLeg": True, "routeId": "AM:CC"}]}
    how_to_pay, total = no_fare_labels(has_transit)
    assert "free" not in how_to_pay.lower()
    assert total != "$0.00"
