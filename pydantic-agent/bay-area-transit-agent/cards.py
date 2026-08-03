from __future__ import annotations
import json
import re
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from uagents import Context
from uagents_core.contrib.protocols.chat import (
    ChatMessage,
    MetadataContent,
    TextContent,
)

from clients.transitland import transit_legs
from models import GeocodeCandidate, epoch_ms_to_clock, fmt_duration

CARD_PROTOCOL_VERSION = "1"

# Wait longer than this (seconds) earns a distinct warning badge on a route.
LONG_WAIT_THRESHOLD_S = 20 * 60


# Wire helpers 
def build_card_metadata(
    card_kind: str, payload: dict[str, Any], *, is_terminal: bool = False
) -> dict[str, str]:
    """Wrap a card payload into the flat string→string metadata dict.

    This is the ONLY place ``card_protocol_version`` is set, so it can never be
    forgotten. ``card_payload`` is JSON-stringified here, never nested as a dict.
    """
    meta: dict[str, str] = {
        "card_protocol_version": CARD_PROTOCOL_VERSION,
        "requires_card_interaction": "true",
        "card_kind": card_kind,
        "card_payload": json.dumps(payload),
    }
    if is_terminal:
        meta["is_terminal"] = "true"
    return meta


async def send_card(
    ctx: Context, sender: str, narration: str, card_meta: dict[str, str]
) -> None:
    """Send one narration bubble + one card declaration in a single ChatMessage.

    The narration must be self-contained: if ASI:One rejects the card payload it
    silently degrades to showing only this text.
    """
    content: list[Any] = []
    if narration:
        content.append(TextContent(type="text", text=narration))
    content.append(MetadataContent(type="metadata", metadata=card_meta))
    await ctx.send(
        sender,
        ChatMessage(timestamp=datetime.now(timezone.utc), msg_id=uuid4(), content=content),
    )


async def send_resource(
    ctx: Context, sender: str, resource_content: Any, caption: str = ""
) -> None:
    """Send a ``ResourceContent`` block (e.g. the trip map image) as its own
    message, with an optional narration bubble alongside it - e.g. the map's
    colour legend and "open in Google Maps" link (ux-diagnosis.md issue D),
    which the PNG itself can't carry.

    Kept separate from ``send_card`` since a ``ResourceContent`` is a different
    Chat Protocol content type, not card metadata (diagnosis.md issue 7).
    """
    content: list[Any] = []
    if caption:
        content.append(TextContent(type="text", text=caption))
    content.append(resource_content)
    await ctx.send(
        sender,
        ChatMessage(timestamp=datetime.now(timezone.utc), msg_id=uuid4(), content=content),
    )


async def send_text(ctx: Context, sender: str, text: str) -> None:
    await ctx.send(
        sender,
        ChatMessage(
            timestamp=datetime.now(timezone.utc),
            msg_id=uuid4(),
            content=[TextContent(type="text", text=text)],
        ),
    )


def extract_text(msg: ChatMessage) -> str:
    """First TextContent block, with a leading ``@mention`` stripped."""
    for block in msg.content:
        if isinstance(block, TextContent):
            return re.sub(r"^@\S+\s+", "", (block.text or "")).strip()
    return ""


def parse_selection(text: str) -> dict[str, Any]:
    """Parse a card selection from JSON (direct @mention) first, prose second.

    Returns a dict that always carries an ``action`` when one can be inferred, so
    stage handlers can branch on ``selection.get("action")`` uniformly. Prose
    parsing only fills in what it can confidently detect; ambiguous input yields
    an empty-ish dict that the caller treats as free text.
    """
    stripped = (text or "").strip()
    if stripped.startswith("{") and stripped.endswith("}"):
        try:
            data = json.loads(stripped)
            if isinstance(data, dict):
                return {str(k): v for k, v in data.items()}
        except json.JSONDecodeError:
            pass
    return {}


# Stage 2 — trip intake form 
def intake_form_card() -> dict[str, str]:
    """The ``form`` card that collects a trip request.

    The ``form`` schema is exactly ``{title, fields, submit_cta}`` - no top-level
    ``subtitle`` key (that would get the card silently dropped), so framing copy
    lives in the caller's narration text.

    There is no depart-time field: the search always departs at the moment it
    runs, which is the moment the rider is asking.
    """
    payload = {
        "title": "Plan a Bay Area trip",
        "fields": [
            {
                "name": "origin",
                "kind": "text",
                "label": "From",
                "required": True,
                "placeholder": "Downtown Berkeley, or Powell St",
            },
            {
                "name": "destination",
                "kind": "text",
                "label": "To",
                "required": True,
                "placeholder": "The Mission, or Fruitvale BART",
            },
            {
                "name": "priority",
                "kind": "select",
                "label": "Optimize for",
                "required": True,
                "options": [
                    {"value": "fastest", "label": "Fastest"},
                    {"value": "fewest_transfers", "label": "Fewest transfers"},
                    {"value": "cheapest", "label": "Cheapest"},
                ],
            },
        ],
        "submit_cta": {"label": "Find routes", "selection": {"action": "submit_trip"}},
    }
    return build_card_metadata("form", payload)


# Stage 2.5 - geocoding disambiguation + no-match 
_KIND_LABELS = {
    "aeroway/aerodrome": "Airport",
    "railway/station": "Train station",
    "railway/halt": "Train station",
    "highway/bus_stop": "Bus stop",
    "highway/motorway": "Road",
    "highway/primary": "Road",
    "highway/secondary": "Road",
    "man_made/bridge": "Bridge",
    "tourism/attraction": "Landmark",
    "amenity/university": "University",
}


def _kind_label(kind: str) -> str | None:
    return _KIND_LABELS.get(kind)


def _short_label(label: str, kind: str = "") -> tuple[str, str]:
    """Split a Nominatim display_name into (title, subtitle).

    Keeps more of the address than a bare 3-part truncation (a 3-part cut can
    silently drop the one differentiator - e.g. a zip code - that would tell two
    similarly-named candidates apart), and appends a human-readable ``kind`` tag
    when one is known, since that's a real signal Nominatim gives us that a raw
    address string doesn't (diagnosis.md issue 3).
    """
    parts = [p.strip() for p in label.split(",")]
    title = parts[0] if parts else label
    subtitle = ", ".join(parts[1:5]) if len(parts) > 1 else ""
    kind_label = _kind_label(kind)
    if kind_label:
        subtitle = f"{subtitle} · {kind_label}" if subtitle else kind_label
    return title, subtitle


def disambiguation_carousel_card(field: str, candidates: list[GeocodeCandidate]) -> dict[str, str]:
    """A ``carousel`` asking the user which match they meant for ``field``."""
    items = []
    for i, c in enumerate(candidates):
        title, subtitle = _short_label(c.label, c.kind)
        items.append(
            {
                "id": f"{field}_{i}",
                "title": title,
                "subtitle": subtitle,
                "primary_cta": {"label": "Use this", "selection": c.to_selection(field)},
            }
        )
    payload = {
        "title": f"Which {'starting point' if field == 'origin' else 'destination'}?",
        "subtitle": "Pick the closest match, or retype it in chat.",
        "items": items,
    }
    return build_card_metadata("carousel", payload)


def terminal_info_card(title: str, body: str) -> dict[str, str]:
    """A read-only informational ``detail`` card (no interaction expected)."""
    payload = {
        "title": title,
        "summary_rows": [{"label": "", "value": body}],
    }
    return build_card_metadata("detail", payload, is_terminal=True)


# Stage 3 - route search carousel 
_AGENCY_SHORT_NAMES = {
    "Bay Area Rapid Transit": "BART",
    "San Francisco Municipal Transportation Agency": "Muni",
    "AC TRANSIT": "AC Transit",
}
_MODE_NOUN = {"RAIL": "train", "SUBWAY": "train", "TRAM": "train", "BUS": "bus", "FERRY": "ferry", "BOAT": "ferry"}
_DUAL_TAP_AGENCY_MARKERS = (
    "bay area rapid transit",
    "golden gate transit",
    "caltrain",
    "peninsula corridor",
    "san francisco bay ferry",
    "water emergency transportation",
    "sonoma-marin area rail transit",
    "sonoma county transit",
)


def _requires_tap_off(leg: dict[str, Any]) -> bool:
    agency = (leg.get("agencyName") or "").lower()
    return any(marker in agency for marker in _DUAL_TAP_AGENCY_MARKERS)


def _tap_off_agencies(itinerary: dict[str, Any]) -> list[str]:
    """Distinct plain-language agencies in this itinerary that need a second tap.

    Computed per-trip so the fare card only ever names the legs that actually
    apply here, rather than a generic disclaimer a first-time rider has no way
    to map onto their own itinerary.
    """
    seen: list[str] = []
    for leg in transit_legs(itinerary):
        if _requires_tap_off(leg):
            label = _plain_leg_label(leg)
            if label not in seen:
                seen.append(label)
    return seen


def fare_narration_lines(itinerary: dict[str, Any], fare_choices: list[dict[str, Any]]) -> list[str]:
    """Short, standalone sentences for the fare-choice narration (Stage 4).

    Previously these lived as ``route_detail_card`` summary_rows, but a link
    plus a full sentence doesn't fit a table cell without wrapping across
    several lines - it reads as a wall of text *inside* the card rather than
    the plain instruction it should be. Each fact is its own short line here so
    the narration stays scannable instead of one run-on paragraph.
    """
    lines: list[str] = []
    tap_off = _tap_off_agencies(itinerary)
    if tap_off:
        lines.append(f"Please tap off when you exit {', '.join(tap_off)} as it charges by distance.")
    if fare_choices:
        lines.append(
            "New to Clipper? Not required - a contactless bank card/phone works just as well."
        )
        lines.append(
            "Want a Clipper card or need help getting one? https://clippercard.com/get"
        )
    return lines


def _plain_leg_label(leg: dict[str, Any]) -> str:
    """"BART train", "Muni bus" - agency + mode, not a route code."""
    agency = _AGENCY_SHORT_NAMES.get(leg.get("agencyName") or "", leg.get("agencyName") or "Transit")
    noun = _MODE_NOUN.get(leg.get("mode") or "", "")
    if noun and noun not in agency.lower():
        return f"{agency} {noun}"
    return agency


def _route_title(itinerary: dict[str, Any]) -> str:
    """Plain-language title: agency+mode per leg, collapsing consecutive repeats.

    A transfer between two BART lines (still "BART", just a different color code)
    collapses to one label - the transfer count is already shown in the subtitle,
    so no information is lost by not repeating "BART" twice.
    """
    legs = transit_legs(itinerary)
    if not legs:
        return "Walk the whole way"
    labels: list[str] = []
    for leg in legs:
        label = _plain_leg_label(leg)
        if not labels or labels[-1] != label:
            labels.append(label)
    return " → ".join(labels)


route_title = _route_title
plain_leg_label = _plain_leg_label


def _first_boarding_headsign(itinerary: dict[str, Any]) -> str | None:
    legs = transit_legs(itinerary)
    return legs[0].get("headsign") if legs else None


def route_carousel_card(itineraries: list[dict[str, Any]], priority: str) -> dict[str, str]:
    durations = [it.get("duration", 0) or 0 for it in itineraries]
    transfers = [it.get("transfers", 0) or 0 for it in itineraries]
    fastest_idx = durations.index(min(durations)) if durations else -1
    fewest_idx = transfers.index(min(transfers)) if transfers else -1

    items = []
    for i, it in enumerate(itineraries):
        badges = []
        if i == fastest_idx:
            badges.append({"label": "Fastest", "variant": "success"})
        if i == fewest_idx and fewest_idx != fastest_idx:
            badges.append({"label": "Fewest transfers", "variant": "info"})
        wait = it.get("waitingTime", 0) or 0
        if wait >= LONG_WAIT_THRESHOLD_S:
            badges.append({"label": f"Long wait — {fmt_duration(wait)}", "variant": "warning"})

        n = it.get("transfers", 0) or 0
        subtitle = (
            f"{fmt_duration(it.get('duration'))} · {n} transfer{'s' if n != 1 else ''} · "
            f"{epoch_ms_to_clock(it.get('startTime'))}–{epoch_ms_to_clock(it.get('endTime'))}"
        )
        headsign = _first_boarding_headsign(it)
        if headsign:
            subtitle += f" · Board toward {headsign}"
        items.append(
            {
                "id": f"route_{i}",
                "title": _route_title(it),
                "subtitle": subtitle,
                "badges": badges,
                "primary_cta": {
                    "label": "View & fares",
                    "selection": {"action": "pick_route", "route_index": i},
                },
            }
        )

    payload = {
        "title": "Route options",
        "subtitle": f"Optimizing for {priority.replace('_', ' ')}. Tap one for fares & live status.",
        "items": items,
    }
    return build_card_metadata("carousel", payload)


# Stage 4 - the walkthrough (custom list) + fare/confirm (detail) 
def _leg_step_items(
    itinerary: dict[str, Any], leg_amounts: list[float | None] | None = None
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    transit_idx = 0
    for leg in itinerary.get("legs", []):
        frm = leg.get("from") or {}
        to = leg.get("to") or {}
        if leg.get("transitLeg"):
            headsign = leg.get("headsign")
            heading = f"Board {_plain_leg_label(leg)}" + (f" toward {headsign}" if headsign else "")
            detail = (
                f"{frm.get('name') or 'boarding point'} ({epoch_ms_to_clock(leg.get('startTime'))}) "
                f"→ {to.get('name') or 'alight point'} ({epoch_ms_to_clock(leg.get('endTime'))})"
            )
            if leg_amounts and transit_idx < len(leg_amounts) and leg_amounts[transit_idx] is not None:
                detail += f" · ${leg_amounts[transit_idx]:.2f}"
            transit_idx += 1
            children: list[dict[str, Any]] = [
                {"type": "heading", "value": heading, "level": 3},
                {"type": "text", "value": detail, "style": "muted"},
            ]
            route_code = leg.get("routeShortName") or leg.get("routeLongName")
            if route_code:
                children.append({"type": "badge", "label": str(route_code), "variant": "info"})
            if _requires_tap_off(leg):
                children.append(
                    {
                        "type": "text",
                        "value": "Tap off when you exit - this leg is priced by distance/zone.",
                        "style": "muted",
                    }
                )
        else:
            dest = to.get("name")
            walk_text = f"Walk to {dest}" if dest else f"Walk ({fmt_duration(leg.get('duration'))})"
            children = [{"type": "text", "value": walk_text, "style": "body"}]
        items.append({"children": children})
    return items


def route_walkthrough_card(
    itinerary: dict[str, Any],
    alerts: list[str],
    *,
    leg_amounts: list[float | None] | None = None,
) -> dict[str, str]:
    legs = itinerary.get("legs", [])
    start = legs[0].get("startTime") if legs else None
    end = legs[-1].get("endTime") if legs else None
    n = itinerary.get("transfers", 0) or 0
    subtitle = (
        f"{fmt_duration(itinerary.get('duration'))} · {n} transfer{'s' if n != 1 else ''} · "
        f"{epoch_ms_to_clock(start)}–{epoch_ms_to_clock(end)}"
    )
    children: list[dict[str, Any]] = [
        {"type": "badge", "label": f"⚠ {alert}", "variant": "warning"} for alert in alerts
    ]
    children.append({"type": "list", "items": _leg_step_items(itinerary, leg_amounts)})
    payload = {
        "root": {
            "type": "section",
            "title": f"How to make this trip: {_route_title(itinerary)}",
            "subtitle": subtitle,
            "children": children,
        }
    }
    return build_card_metadata("custom", payload, is_terminal=True)


def no_fare_labels(itinerary: dict[str, Any]) -> tuple[str, str]:
    if transit_legs(itinerary):
        return "Pay the operator directly", "Not available"
    return "Walking - free", "$0.00"


def route_detail_card(
    itinerary: dict[str, Any],
    fare_choices: list[dict[str, Any]],
    *,
    fare_notes: list[str] | None = None,
) -> dict[str, str]:
    legs = itinerary.get("legs", [])
    start = legs[0].get("startTime") if legs else None
    end = legs[-1].get("endTime") if legs else None
    n = itinerary.get("transfers", 0) or 0

    summary_rows = [
        {"label": "Route", "value": _route_title(itinerary)},
        {"label": "Departure", "value": epoch_ms_to_clock(start)},
        {"label": "Arrival", "value": epoch_ms_to_clock(end)},
        {"label": "Transfers", "value": str(n)},
        {"label": "Duration", "value": fmt_duration(itinerary.get("duration"))},
    ]
    for note in fare_notes or []:
        summary_rows.append({"label": "Fare note", "value": note})

    payload: dict[str, Any] = {
        "title": "Payment Options",
        "summary_rows": summary_rows,
        "ctas": [
            {"label": "Confirm this trip", "selection": {"action": "continue_review"}, "primary": True},
            {"label": "Back to routes", "selection": {"action": "back_to_routes"}},
        ],
    }
    if fare_choices:
        payload["sub_options"] = {
            "name": "fare",
            "kind": "radio",
            "label": "Payment Options",
            "choices": fare_choices,
        }
    else:
        how_to_pay, _ = no_fare_labels(itinerary)
        payload["summary_rows"].append({"label": "Fare", "value": how_to_pay})
    return build_card_metadata("detail", payload)


# Stage 5 - review & confirm 
def review_card(summary_rows: list[dict[str, str]]) -> dict[str, str]:
    """The Stage 5 ``review`` card - a read-only summary with approve/reject.
    """
    payload = {
        "title": "Confirm your trip",
        "summary_rows": summary_rows,
        "approve_cta": {"label": "Confirm", "selection": {"action": "confirm"}, "primary": True},
        "reject_cta": {"label": "Start over", "selection": {"action": "cancel"}},
    }
    return build_card_metadata("review", payload)


def final_itinerary_card(
    itinerary: dict[str, Any],
    fare_label: str | None,
    fare_value: str | None,
    *,
    leg_amounts: list[float | None] | None = None,
    alerts: list[str] | None = None,
) -> dict[str, str]:
    legs = itinerary.get("legs", [])
    start = legs[0].get("startTime") if legs else None
    end = legs[-1].get("endTime") if legs else None
    n = itinerary.get("transfers", 0) or 0
    subtitle = (
        f"{fmt_duration(itinerary.get('duration'))} · {n} transfer{'s' if n != 1 else ''} · "
        f"{epoch_ms_to_clock(start)}–{epoch_ms_to_clock(end)}"
    )
    children: list[dict[str, Any]] = [
        {"type": "badge", "label": f"⚠ {alert}", "variant": "warning"} for alert in (alerts or [])
    ]
    if fare_label and fare_value:
        children.append({"type": "text", "value": f"Fare: {fare_label} · {fare_value}", "style": "emphasis"})
    children.append({"type": "list", "items": _leg_step_items(itinerary, leg_amounts)})
    payload = {
        "root": {
            "type": "section",
            "title": f"Trip confirmed 🎉 — {_route_title(itinerary)}",
            "subtitle": subtitle,
            "children": children,
        }
    }
    return build_card_metadata("custom", payload, is_terminal=True)


def no_routes_recovery_card(origin_text: str, destination_text: str) -> dict[str, str]:
    payload = {
        "title": "No routes found",
        "summary_rows": [
            {"label": "From", "value": origin_text},
            {"label": "To", "value": destination_text},
            {
                "label": "",
                "value": "Nothing runs between these two points right now. "
                "Try a nearby station or a different destination.",
            },
        ],
        "ctas": [
            {
                "label": "Start a new trip",
                "selection": {"action": "new_trip"},
                "primary": True,
            },
        ],
    }
    return build_card_metadata("detail", payload)


def routing_error_card() -> dict[str, str]:
    payload = {
        "title": "Couldn't reach the trip planner",
        "summary_rows": [
            {"label": "", "value": "The routing service didn't respond. Want to try again?"}
        ],
        "ctas": [
            {"label": "Try again", "selection": {"action": "retry_routing"}, "primary": True}
        ],
    }
    return build_card_metadata("detail", payload)
