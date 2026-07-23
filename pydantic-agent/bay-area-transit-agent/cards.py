"""ASI:One interactive-card builders + the shared send/parse helpers.

Every card leaves this module through :func:`build_card_metadata`, which always
stamps ``card_protocol_version="1"`` - forgetting that key anywhere makes ASI:One
silently drop the card (master edge-case #1, see ``research-notes.md`` §1). Card
payloads are JSON-*stringified* into a flat ``dict[str, str]`` exactly as the wire
protocol requires.

Selections come back either as JSON (direct ``@mention``) or as natural-language
prose (planner-mediated); :func:`parse_selection` tries JSON first and falls back
to keyword parsing, uniformly at every stage (master edge-case #2).
"""

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


# ── Wire helpers ─────────────────────────────────────────────────────────────
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


# ── Stage 2 — trip intake form ───────────────────────────────────────────────
def intake_form_card() -> dict[str, str]:
    """The ``form`` card that collects a trip request.

    The ``form`` schema is exactly ``{title, fields, submit_cta}`` - no top-level
    ``subtitle`` key (that would get the card silently dropped), so framing copy
    lives in the caller's narration text.
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
                "name": "depart_option",
                "kind": "select",
                "label": "Depart",
                "required": True,
                "options": [
                    {"value": "now", "label": "Leave now"},
                    {"value": "15", "label": "In 15 minutes"},
                    {"value": "30", "label": "In 30 minutes"},
                    {"value": "custom", "label": "Pick a time (type it in chat)"},
                ],
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


# ── Stage 2.5 - geocoding disambiguation + no-match ───────────────────────────
# Human-friendly labels for the OSM class/type pairs Nominatim returns most often
# for Bay Area queries, so a candidate's *kind* of place is visible, not just its
# address text (diagnosis.md issue 3 - e.g. an airport terminal vs. a same-named
# rail station a few blocks away are otherwise textually near-identical).
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


# ── Stage 3 - route search carousel ──────────────────────────────────────────
# Route-selection titles must be readable with zero prior Bay Area transit
# knowledge (ux-diagnosis.md issue A) - BART's ``routeShortName`` is an internal
# line-color code ("Red-S") that means nothing to a newcomer deciding between
# options, even though it's exactly what a rider *should* look for once they're
# actually at the platform (kept, deliberately, in ``_leg_instruction_rows``).
# Agencies whose live 511/Transitland ``agencyName`` is long or non-obvious get a
# short, commonly recognized name; anything unmapped falls back to the agency
# name as-is (verbose, but not cryptic - only BART's/Muni's official legal names
# are actually unrecognizable to a rider).
_AGENCY_SHORT_NAMES = {
    "Bay Area Rapid Transit": "BART",
    "San Francisco Municipal Transportation Agency": "Muni",
    "AC TRANSIT": "AC Transit",
}
_MODE_NOUN = {"RAIL": "train", "SUBWAY": "train", "TRAM": "train", "BUS": "bus", "FERRY": "ferry", "BOAT": "ferry"}


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


# Public aliases - chat_proto.py's Review/re-send cards and map_image.py's map
# legend both need this same plain-language labeling, not a second copy of it.
route_title = _route_title
plain_leg_label = _plain_leg_label


def _first_boarding_headsign(itinerary: dict[str, Any]) -> str | None:
    """The destination-facing headsign of the first transit leg, if known.

    This is the direction label a rider needs at the very first platform/stop to
    avoid boarding the correct line going the wrong way (diagnosis.md issue 5) -
    Transitland returns it on every transit leg, but nothing previously read it.
    """
    legs = transit_legs(itinerary)
    return legs[0].get("headsign") if legs else None


def route_carousel_card(itineraries: list[dict[str, Any]], priority: str) -> dict[str, str]:
    """Build the Stage 3 ``carousel`` from Transitland itineraries.

    Badges: Fastest (min duration), Fewest transfers (min transfers, when distinct),
    and a distinct ``warning`` badge for any itinerary with a long wait (sparse
    service) so it never looks like a normal short-wait option.
    """
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


# ── Stage 4 - the walkthrough (custom list) + fare/confirm (detail) ──────────
def _leg_step_items(
    itinerary: dict[str, Any], leg_amounts: list[float | None] | None = None
) -> list[dict[str, Any]]:
    """One ``custom``-card ``list`` item per leg, in trip order: walk, board,
    walk, board, ... - a sequence a rider can follow, not a flat table to
    decode (ux-diagnosis.md issue B).

    The heading leads with the plain-language mode/agency (``_plain_leg_label``,
    issue A) so deciding "does this leg make sense to me" never requires
    decoding a line-color code first; the actual route code, which *is* what a
    rider needs once they're at the platform, stays visible as a badge on the
    same step. ``leg_amounts``, if given, is the chosen fare option's per-leg
    cost, so the total is verifiable leg-by-leg (issue 6, prior pass).
    """
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
    """The Stage 4 ``custom`` list-sequence card: "how to make this trip."

    Sent as its own message *before* the fare/confirm card - so this, the
    clearest explanation of what the trip actually involves, is available at
    the point a rider is deciding whether to proceed, not only after they've
    already confirmed (ux-diagnosis.md issue C). ``is_terminal`` because it's
    read-only; the Confirm/Back decision lives on the card sent right after it.
    """
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


def route_detail_card(
    itinerary: dict[str, Any],
    fare_choices: list[dict[str, Any]],
    *,
    fare_notes: list[str] | None = None,
) -> dict[str, str]:
    """Build the Stage 4 ``detail`` card: trip summary + fare radio + confirm.

    The step-by-step walkthrough now lives on ``route_walkthrough_card``, sent
    right before this one - this card is just the payment decision itself, so
    it isn't competing with sequential content for the same flat row list
    (ux-diagnosis.md issue B). ``fare_notes`` (why a payment method that was
    considered isn't offered, e.g. "Cash isn't accepted on BART") stays here
    since it directly explains this card's own choices.
    """
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
        "title": "How would you like to pay?",
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
            "label": "How to pay (cheapest option first - already selected below)",
            "choices": fare_choices,
        }
    else:
        payload["summary_rows"].append({"label": "Fare", "value": "Walking - free"})
    return build_card_metadata("detail", payload)


# ── Stage 5 - review & confirm ───────────────────────────────────────────────
def review_card(summary_rows: list[dict[str, str]]) -> dict[str, str]:
    """The Stage 5 ``review`` card - a read-only summary with approve/reject.

    Approve/Reject map 1:1 onto the Pydantic AI deferred-tool approval that gates
    the finalize step (research-notes.md §5).
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
) -> dict[str, str]:
    """Terminal ``custom`` card recapping the confirmed trip as a step sequence.

    Same list-sequence rendering as ``route_walkthrough_card`` (issue B) - this
    is also the card the urgent/``escalate`` path sends directly (skipping the
    walkthrough entirely, by design), so it must stand on its own with the full
    per-leg board/alight/direction breakdown, not just a recap.
    """
    legs = itinerary.get("legs", [])
    start = legs[0].get("startTime") if legs else None
    end = legs[-1].get("endTime") if legs else None
    n = itinerary.get("transfers", 0) or 0
    subtitle = (
        f"{fmt_duration(itinerary.get('duration'))} · {n} transfer{'s' if n != 1 else ''} · "
        f"{epoch_ms_to_clock(start)}–{epoch_ms_to_clock(end)}"
    )
    children: list[dict[str, Any]] = []
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
    """A ``detail`` card shown when a fully-geocoded trip returns zero itineraries.

    Names the actual trip that failed (diagnosis.md issue 2 - the old fallback was
    a blank, generic form with unrelated placeholder examples) and offers two
    concrete next steps instead of a silent reset.
    """
    payload = {
        "title": "No routes found",
        "summary_rows": [
            {"label": "From", "value": origin_text},
            {"label": "To", "value": destination_text},
        ],
        "ctas": [
            {"label": "Try 30 min later", "selection": {"action": "retry_later"}, "primary": True},
            {"label": "Start a new trip", "selection": {"action": "new_trip"}},
        ],
    }
    return build_card_metadata("detail", payload)


def routing_error_card() -> dict[str, str]:
    """A ``detail`` card with a Try-again CTA when routing fails."""
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
