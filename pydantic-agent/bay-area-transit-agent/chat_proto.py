"""Agent Chat Protocol handlers - the session/stage dispatcher.

Every inbound ``ChatMessage`` is acknowledged first, then run through:

1. a per-window reset (new chat conversation => pay again, per product decision),
2. the payment gate (Stage 0): any message from an unpaid sender gets the identical
   Stripe gate and nothing else,
3. once paid, the stage dispatcher (Stages 2-6) - filled in stage by stage.

The interrupt classifier (cross-cutting) will sit in front of the paid-stage
dispatch once the conversational layer lands; for now the paid path routes into
Stage 2 intake.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from uagents import Context, Protocol
from uagents_core.contrib.protocols.chat import (
    ChatAcknowledgement,
    ChatMessage,
    TextContent,
    chat_protocol_spec,
)

from ai import (
    answer_fare_question,
    classify_intent,
    extract_trip,
    resume_finalize,
    start_finalize,
)
from cards import (
    disambiguation_carousel_card,
    extract_text,
    fare_narration_lines,
    final_itinerary_card,
    intake_form_card,
    no_fare_labels,
    no_routes_recovery_card,
    parse_selection,
    review_card,
    route_carousel_card,
    route_detail_card,
    route_title,
    route_walkthrough_card,
    routing_error_card,
    send_card,
    send_resource,
    send_text,
    terminal_info_card,
)
from clients.five11 import alerts_for_routes, load_fare_data
from clients.geocode import resolve_place
from clients.transitland import RoutingError, plan, transit_legs
from fares import compute_fare_options
from map_image import build_trip_map_resource
from models import epoch_ms_to_clock, fmt_duration, new_trip, validate_trip_texts
from payment import clear_watch, request_payment, settle_or_request_payment
from session_state import (
    AWAITING_CONFIRM,
    DONE,
    INTAKE,
    SHOWING_DETAIL,
    SHOWING_ROUTES,
    clear_trip_state,
    get_state,
    reset_on_new_window,
    save_state,
)

chat_proto = Protocol(spec=chat_protocol_spec)


# ── Stage 2 entry (called from here and from payment._grant_access) ───────────
async def start_intake(
    ctx: Context,
    sender: str,
    state: dict[str, Any],
    *,
    welcome: bool = False,
    error: str | None = None,
) -> None:
    """Move the session into Stage 2 and (re)send the trip-intake form.

    ``error`` re-renders the form with an inline correction message; ``welcome``
    is the just-paid greeting.
    """
    state["stage"] = INTAKE
    state["pending_trip"] = None
    save_state(ctx, sender, state)
    if error:
        narration = f"{error} Let's try again - fill in the form or describe your trip."
    elif welcome:
        narration = (
            "Payment confirmed! Where are you headed? Fill in the trip "
            'form, or just tell me in your own words (e.g. "Berkeley to the Mission"). '
            "I'll plan it departing right now. \n\n"
            "New to Bay Area transit? At any point, just ask me to explain something, or say "
            '"you decide" / "pick for me" and I\'ll go with a sensible default.'
        )
    else:
        narration = "Let's plan a trip. Fill in the form, or just describe it in a sentence."
    await send_card(ctx, sender, narration, intake_form_card())


# ── Stage 0 - payment gate ───────────────────────────────────────────────────
# A cheap, local check so an LLM call only happens for messages that actually
# look like a question - the common pre-payment cases (a greeting, an attempted
# trip request) skip straight to the gate, no extra latency or cost.
_QUESTION_STARTERS = (
    "what", "how", "why", "where", "when", "who",
    "do i", "does", "is ", "are ", "can i", "should i", "will ",
)
# Phrases that read as a request for explanation wherever they appear in the
# message, not just as a sentence opener - e.g. "I still don't know anything
# about Clipper, can you explain" (observed live: the mid-flow LLM interrupt
# classifier misread this exact phrasing as "override" instead of
# "side_question", wiping the trip already in progress). Kept to specific,
# multi-word phrases rather than single common words like "is"/"are", which
# would otherwise also match plain statements ("This is the wallet I use").
_EXPLANATION_PHRASES = (
    "can you explain", "could you explain", "can you tell me",
    "could you tell me", "don't know", "dont know", "not sure",
    "no idea", "explain how", "explain what", "tell me about",
    "tell me more",
)
# Scopes the pre-payment Q&A to fare/Clipper topics specifically - deliberately
# narrower than "any question". Without this, something like "How do I get from
# Berkeley to the Mission?" would also match _looks_like_a_question and get
# routed to an LLM with no route data, which could hallucinate a plausible-
# sounding but fabricated transit answer. That's both a real "no functionality
# before payment" violation and a wrong-information risk, so a question that
# doesn't mention a fare/payment topic falls straight through to the gate
# instead, same as any other pre-payment message.
_FARE_QUESTION_KEYWORDS = (
    "clipper", "fare", "pay", "card", "tap", "cash", "cost", "price",
    "ticket", "contactless", "wallet", "charge", "refund", "discount",
)


def _looks_like_a_fare_question(text: str) -> bool:
    t = text.strip().lower()
    is_question = (
        t.endswith("?")
        or t.startswith(_QUESTION_STARTERS)
        or any(p in t for p in _EXPLANATION_PHRASES)
    )
    return is_question and any(k in t for k in _FARE_QUESTION_KEYWORDS)


async def _handle_unpaid(ctx: Context, sender: str, text: str) -> None:
    """Gate every unpaid message. No functionality is reachable before payment.

    Reads state itself, fresh, rather than trusting a value the caller read
    earlier: the background Stripe poller can grant access between that read
    and this call (e.g. while a prior turn's own LLM call was in flight), and a
    rider who just paid must not be held up answering a question they no
    longer need to ask.

    Otherwise, a genuine fare/Clipper question (e.g. "what is Clipper", "do I
    need a physical card") gets a short, grounded answer instead of just
    re-showing the gate silently again - a first-time rider shouldn't be left
    guessing about how paying even works. The answer is sent as its own
    text-only reply and the gate is *not* re-sent in the same turn:
    ``RequestPayment`` must go out with no accompanying text or ASI:One drops
    the native payment sheet (payment.py), so the question is answered against
    the card already on screen, not a fresh one. Anything else - a greeting, an
    attempted trip request, a question unrelated to paying, or the Q&A call
    being unavailable - falls straight through to the gate, unchanged.
    """
    state = get_state(ctx, sender)
    if state.get("paid"):
        await _dispatch_paid(ctx, sender, text, state)
        return
    if _looks_like_a_fare_question(text):
        try:
            reply = (await answer_fare_question(text)).strip()
        except Exception as exc:  # never let a Q&A hiccup block the payment gate
            reply = ""
            ctx.logger.error(f"[payment] fare Q&A failed, gating as usual: {exc}")
        if reply:
            await send_text(ctx, sender, f"{reply} Pay above to unlock trip planning.")
            return
    await settle_or_request_payment(ctx, sender, state)


# ── Stage 2 - trip intake (form + free-text paths) ───────────────────────────
async def _handle_intake(ctx: Context, sender: str, text: str, state: dict[str, Any]) -> None:
    """Collect a trip request from either the form card or free text.

    If a geocoding disambiguation is in progress (``pending_trip`` set), this
    message either picks a candidate or retypes the place being resolved.
    """
    selection = parse_selection(text)
    action = selection.get("action")

    # Mid-geocoding: a carousel pick resolves the current field.
    if state.get("pending_trip") and action == "pick_place":
        await _apply_place_pick(ctx, sender, state, selection)
        return

    # Recovery-card action from a just-failed zero-route search (diagnosis.md
    # issue 2).
    if action == "new_trip":
        clear_trip_state(state)
        await start_intake(ctx, sender, state)
        return

    if action == "submit_trip":
        origin = str(selection.get("origin", "") or "")
        destination = str(selection.get("destination", "") or "")
        priority = str(selection.get("priority", "fastest") or "fastest")
    else:
        # Free-text path -> Pydantic AI structured extraction.
        extraction = await extract_trip(text)
        origin, destination = extraction.origin, extraction.destination
        priority = extraction.priority

    error = validate_trip_texts(origin, destination)
    if error:
        await start_intake(ctx, sender, state, error=error)
        return

    state["pending_trip"] = new_trip(
        origin_text=origin,
        destination_text=destination,
        priority=priority,
    )
    save_state(ctx, sender, state)
    await _resolve_next_field(ctx, sender, state)


# ── Stage 2.5 - geocoding resolution loop ────────────────────────────────────
async def _resolve_next_field(ctx: Context, sender: str, state: dict[str, Any]) -> None:
    """Geocode origin then destination, one field at a time.

    Resolves the first field still missing coords; a disambiguation returns a
    carousel and pauses here until the user picks. When both are resolved the
    trip is finalized and handed to Stage 3.
    """
    pending = state.get("pending_trip")
    if not pending:
        await start_intake(ctx, sender, state)
        return

    if pending["origin_coords"] is None:
        field, place_text = "origin", pending["origin_text"]
    elif pending["destination_coords"] is None:
        field, place_text = "destination", pending["destination_text"]
    else:
        await _finalize_trip(ctx, sender, state)
        return

    status, candidates = await resolve_place(place_text)
    if status == "not_found":
        await send_card(
            ctx,
            sender,
            f'I couldn\'t find "{place_text}".',
            terminal_info_card(
                "Location not found",
                f'I couldn\'t find "{place_text}". Try a station name or a nearby cross street.',
            ),
        )
        await start_intake(ctx, sender, state)
        return
    if status == "resolved":
        c = candidates[0]
        pending[f"{field}_coords"] = [c.lat, c.lon]
        pending[f"{field}_text"] = c.label
        save_state(ctx, sender, state)
        await _resolve_next_field(ctx, sender, state)
        return

    # Ambiguous -> ask the user to pick. Keep every candidate (not just the one
    # picked) so a zero-route Stage 3 search can retry a sibling candidate before
    # giving up (diagnosis.md issue 1 - Nominatim can return multiple points for
    # one landmark, not all of which are actually transit-reachable).
    alternates = state.setdefault("geocode_alternates", {}) or {}
    alternates[field] = [
        {"lat": c.lat, "lon": c.lon, "label": c.label} for c in candidates
    ]
    state["geocode_alternates"] = alternates
    save_state(ctx, sender, state)

    where = "starting point" if field == "origin" else "destination"
    await send_card(
        ctx,
        sender,
        f'A few places match "{place_text}" - which {where} did you mean?',
        disambiguation_carousel_card(field, candidates),
    )


async def _apply_place_pick(
    ctx: Context, sender: str, state: dict[str, Any], selection: dict[str, Any]
) -> None:
    """Apply a disambiguation-carousel pick and continue resolving."""
    pending = state.get("pending_trip")
    if not pending:
        await start_intake(ctx, sender, state)
        return
    field = selection.get("field")
    if field not in ("origin", "destination"):
        await _resolve_next_field(ctx, sender, state)
        return
    try:
        lat, lon = float(selection["lat"]), float(selection["lon"])
    except (KeyError, TypeError, ValueError):
        await _resolve_next_field(ctx, sender, state)
        return
    pending[f"{field}_coords"] = [lat, lon]
    if selection.get("label"):
        pending[f"{field}_text"] = str(selection["label"])
    save_state(ctx, sender, state)
    await _resolve_next_field(ctx, sender, state)


async def _finalize_trip(ctx: Context, sender: str, state: dict[str, Any]) -> None:
    """Promote a fully-geocoded pending trip and run the route search (Stage 3)."""
    state["trip"] = state.get("pending_trip")
    state["pending_trip"] = None
    state["stage"] = SHOWING_ROUTES
    save_state(ctx, sender, state)
    await _search_routes(ctx, sender, state)


# ── Stage 3 - route search (carousel) ────────────────────────────────────────
async def _retry_with_alternate_candidate(
    ctx: Context, sender: str, state: dict[str, Any]
) -> tuple[dict[str, Any], str] | None:
    """On a zero-itinerary result, retry once against a sibling geocode candidate.

    Nominatim can return several points for one landmark query, not all of which
    are actually transit-reachable (diagnosis.md issue 1 - e.g. two of four
    "Golden Gate Bridge" matches sit on the bridge's own motorway deck, which is
    correctly unroutable on foot, while the other two are the real vista-point
    stops). Only the endpoint(s) that were genuinely ambiguous have alternates
    recorded (see ``_resolve_next_field``), so this is at most a couple of extra
    Transitland calls, only in the already-rare zero-result case.

    Returns ``(plan_obj, note)`` on success, or ``None`` if no alternate helped.
    """
    trip = state.get("trip") or {}
    alternates = state.get("geocode_alternates") or {}
    for field in ("origin", "destination"):
        candidates = alternates.get(field) or []
        current_coords = trip.get(f"{field}_coords")
        for cand in candidates:
            alt_coords = [cand["lat"], cand["lon"]]
            if alt_coords == current_coords:
                continue
            origin = alt_coords if field == "origin" else trip.get("origin_coords")
            dest = alt_coords if field == "destination" else trip.get("destination_coords")
            # Both were already set for the just-failed search this retry follows.
            assert origin is not None and dest is not None
            try:
                plan_obj = await plan(origin, dest)
            except RoutingError:
                continue
            if plan_obj.get("itineraries"):
                trip[f"{field}_coords"] = alt_coords
                trip[f"{field}_text"] = cand.get("label", trip.get(f"{field}_text"))
                state["trip"] = trip
                where = "starting point" if field == "origin" else "destination"
                note = (
                    f'Note: the {where} you picked has no reachable transit stop nearby, '
                    f'so I used "{cand.get("label")}" instead.'
                )
                return plan_obj, note
    return None


async def _search_routes(ctx: Context, sender: str, state: dict[str, Any]) -> None:
    """Call Transitland and render the itinerary carousel (cached for Back)."""
    trip = state.get("trip")
    if not trip or not trip.get("origin_coords") or not trip.get("destination_coords"):
        await start_intake(ctx, sender, state)
        return

    try:
        plan_obj = await plan(trip["origin_coords"], trip["destination_coords"])
    except RoutingError as exc:
        ctx.logger.error(f"[stage3] routing error: {exc}")
        await send_card(
            ctx,
            sender,
            "The trip planner didn't respond just now.",
            routing_error_card(),
        )
        return

    itineraries = plan_obj.get("itineraries") or []
    swap_note = None
    if not itineraries:
        retried = await _retry_with_alternate_candidate(ctx, sender, state)
        if retried:
            plan_obj, swap_note = retried
            itineraries = plan_obj.get("itineraries") or []
            trip = state["trip"]  # _retry_with_alternate_candidate updated coords/text

    if not itineraries:
        await send_card(
            ctx,
            sender,
            f'No routes found from "{trip["origin_text"]}" to "{trip["destination_text"]}" '
            "departing right now.",
            no_routes_recovery_card(trip["origin_text"], trip["destination_text"]),
        )
        state["stage"] = INTAKE
        save_state(ctx, sender, state)
        return

    state["last_itineraries"] = plan_obj  # cached so Back needs no re-fetch
    state["stage"] = SHOWING_ROUTES
    save_state(ctx, sender, state)

    n = len(itineraries)
    lead = (
        "Here's a walking route." if n == 1 and not _has_transit(itineraries[0])
        else f"Found {n} option{'s' if n != 1 else ''}."
    )
    if swap_note:
        lead = f"{swap_note} {lead}"
    await send_card(
        ctx, sender, lead, route_carousel_card(itineraries, trip.get("priority", "fastest"))
    )


def _has_transit(itinerary: dict[str, Any]) -> bool:
    return any(leg.get("transitLeg") for leg in itinerary.get("legs", []))


async def _handle_routes(ctx: Context, sender: str, text: str, state: dict[str, Any]) -> None:
    """Stage 3 dispatch: pick a route, retry, or treat free text as a new trip."""
    selection = parse_selection(text)
    action = selection.get("action")

    if action == "retry_routing":
        await _search_routes(ctx, sender, state)
        return

    if action == "pick_route":
        try:
            idx = int(selection["route_index"])
        except (KeyError, TypeError, ValueError):
            await _search_routes(ctx, sender, state)
            return
        itineraries = (state.get("last_itineraries") or {}).get("itineraries") or []
        if not 0 <= idx < len(itineraries):
            await _search_routes(ctx, sender, state)
            return
        state["selected_route_id"] = str(idx)
        save_state(ctx, sender, state)
        await _show_detail(ctx, sender, state)
        return

    # Free text at the carousel: treat as a new/overridden trip request.
    await _handle_intake(ctx, sender, text, state)


# ── Stage 4 - route + fare detail ────────────────────────────────────────────
def _selected_itinerary(state: dict[str, Any]) -> dict[str, Any] | None:
    raw = state.get("selected_route_id")
    if raw is None:
        return None
    try:
        idx = int(raw)
    except (TypeError, ValueError):
        return None
    itineraries = (state.get("last_itineraries") or {}).get("itineraries") or []
    return itineraries[idx] if 0 <= idx < len(itineraries) else None


async def _show_detail(ctx: Context, sender: str, state: dict[str, Any]) -> None:
    """Compute fares + pull the live overlay and render the detail card."""
    itinerary = _selected_itinerary(state)
    if itinerary is None:
        await _search_routes(ctx, sender, state)
        return

    try:
        fare_data = await load_fare_data()
        options, fare_notes = compute_fare_options(itinerary.get("legs", []), fare_data)
    except Exception as exc:  # fares are best-effort; never block the trip on them
        ctx.logger.error(f"[stage4] fare computation failed: {exc}")
        options, fare_notes = [], []

    legs = transit_legs(itinerary)
    route_ids = {rid for leg in legs if (rid := leg.get("routeId"))}
    agency_ids = {aid for leg in legs if (aid := leg.get("agencyId"))}
    alerts = await alerts_for_routes(route_ids, agency_ids)

    choices = [o.to_choice() for o in options]
    state["fare_options"] = [
        {
            "id": o.id,
            "label": o.label,
            "amount": o.amount,
            "estimated": o.estimated,
            "leg_amounts": o.leg_amounts,
        }
        for o in options
    ]
    state["fare_notes"] = fare_notes
    # Carried forward so the final confirm card (Stage 5) can still show it -
    # without this it's fetched once here and then silently lost by confirm time.
    state["alerts"] = alerts
    # Default to Clipper, which leads the list because it's the only one that also
    # carries discounted fares; a bank card always charges the full adult fare.
    state["selected_fare_option"] = options[0].id if options else None
    state["stage"] = SHOWING_DETAIL
    save_state(ctx, sender, state)

    leg_amounts = options[0].leg_amounts if options else None
    # The walkthrough goes out first, on its own: the clearest explanation of
    # what the trip involves belongs at the point a rider is deciding whether
    # to proceed, not only after they've confirmed (ux-diagnosis.md issue C).
    await send_card(
        ctx, sender, "Here's exactly how to make this trip.",
        route_walkthrough_card(itinerary, alerts, leg_amounts=leg_amounts),
    )

    # Each fact on its own short line rather than one paragraph (a link plus a
    # full sentence doesn't fit a card row without wrapping across several
    # lines, which reads as a wall of text inside the card - ux follow-up).
    # The classifier (ai.py) is what actually answers a rider's follow-up
    # questions about any of this in more depth.
    fare_lines = ["Payment Options"]
    fare_lines.extend(fare_narration_lines(itinerary, choices))
    fare_lines.append("Just ask if anything's unclear.")
    await send_card(
        ctx, sender, "\n".join(fare_lines),
        route_detail_card(itinerary, choices, fare_notes=fare_notes),
    )


async def _handle_detail(ctx: Context, sender: str, text: str, state: dict[str, Any]) -> None:
    """Stage 4 dispatch: choose a fare + continue, go back, or override."""
    selection = parse_selection(text)
    action = selection.get("action")

    if selection.get("fare"):
        state["selected_fare_option"] = str(selection["fare"])
        save_state(ctx, sender, state)

    if action == "back_to_routes":
        itineraries = (state.get("last_itineraries") or {}).get("itineraries") or []
        if not itineraries:
            await _search_routes(ctx, sender, state)
            return
        trip = state.get("trip") or {}
        state["stage"] = SHOWING_ROUTES
        save_state(ctx, sender, state)
        await send_card(
            ctx,
            sender,
            "Back to your route options.",
            route_carousel_card(itineraries, trip.get("priority", "fastest")),
        )
        return

    if action == "continue_review":
        await _show_review(ctx, sender, state)
        return

    # A bare fare pick with no CTA: acknowledge and keep the card in place.
    if selection.get("fare"):
        await send_text(
            ctx, sender, "Got it - tap \"Confirm this trip\" when you're ready."
        )
        return

    # Free text at the detail card: treat as a new/overridden trip request.
    await _handle_intake(ctx, sender, text, state)


async def _send_trip_map(ctx: Context, sender: str, itinerary: dict[str, Any]) -> None:
    """Best-effort: render + upload the confirmed trip's map, then send it.

    Rendering (OSM tiles) and upload (Agentverse External Storage) are both
    genuine network calls that can fail; this must never block or fail the trip
    confirmation itself (diagnosis.md issue 7), matching the same best-effort
    pattern already used for fares/alerts in ``_show_detail``. The colour
    legend and "open in Google Maps" link (ux-diagnosis.md issue D) need no
    network call, so they still go out even if the image itself doesn't.
    """
    try:
        trip_map = await build_trip_map_resource(ctx, sender, itinerary)
    except Exception as exc:
        ctx.logger.error(f"[map] trip map failed: {exc}")
        return

    caption_lines = [line for line in (trip_map.legend,) if line]
    if trip_map.maps_url:
        caption_lines.append(f"\n Open in Google Maps for a live, pannable view: {trip_map.maps_url}")
    caption = "\n".join(caption_lines)

    if trip_map.resource is not None:
        await send_resource(ctx, sender, trip_map.resource, caption)
    elif caption:
        await send_text(ctx, sender, caption)


def _trip_recap_line(
    itinerary: dict[str, Any], fare_label: str | None, fare_value: str | None
) -> str:
    """A self-contained one-line summary of a confirmed trip.

    ASI:One silently drops a card on any schema/size validation failure,
    falling back to showing only its narration text (cards.py's own rule:
    every narration must stand alone) - so the text sent alongside the final
    itinerary card must carry the route, timing and fare itself, not just an
    empty "all set!" that means nothing if the card underneath doesn't render.
    """
    legs = itinerary.get("legs", [])
    n = itinerary.get("transfers", 0) or 0
    start = epoch_ms_to_clock(legs[0].get("startTime") if legs else None)
    end = epoch_ms_to_clock(legs[-1].get("endTime") if legs else None)
    line = (
        f"{route_title(itinerary)} · {fmt_duration(itinerary.get('duration'))} · "
        f"{n} transfer{'s' if n != 1 else ''} · {start}\u2013{end}"
    )
    if fare_label and fare_value:
        line += f" · Fare: {fare_label} {fare_value}"
    return line


# Stage 5 - review & confirm (deferred-tool gate) 
def _selected_fare_display(state: dict[str, Any]) -> tuple[str | None, str | None]:
    """Return (label, price string) for the chosen fare, or (None, None) if none."""
    options = state.get("fare_options") or []
    if not options:
        return None, None
    chosen = next(
        (o for o in options if o["id"] == state.get("selected_fare_option")), options[0]
    )
    price = f"${chosen['amount']:.2f}" + (" (est.)" if chosen.get("estimated") else "")
    return chosen["label"], price


def _selected_fare_leg_amounts(state: dict[str, Any]) -> list[float | None] | None:
    """Per-leg costs for the currently-chosen fare option, if any (for issue-6 breakdowns)."""
    options = state.get("fare_options") or []
    if not options:
        return None
    chosen = next(
        (o for o in options if o["id"] == state.get("selected_fare_option")), options[0]
    )
    return chosen.get("leg_amounts")


async def _show_review(ctx: Context, sender: str, state: dict[str, Any]) -> None:
    """Open the deferred-tool gate and render the Stage 5 review card."""
    itinerary = _selected_itinerary(state)
    if itinerary is None:
        await _search_routes(ctx, sender, state)
        return

    route_name = route_title(itinerary)
    fare_label, fare_value = _selected_fare_display(state)
    if fare_label is None:
        fare_label, fare_value = no_fare_labels(itinerary)

    summary_rows = [
        {"label": "Route", "value": route_name},
        {"label": "Fare option", "value": fare_label},
        {"label": "Total", "value": fare_value or ""},
    ]

    # Open the requires_approval gate: the finalize tool defers before running.
    summary = f"{route_name} | {fare_label} {fare_value or ''}".strip()
    try:
        start = await start_finalize(summary)
        state["finalize_history"] = start.history_json
        state["pending_approval"] = (
            {"tool_call_id": start.tool_call_id} if start.deferred else None
        )
    except Exception as exc:  # never trap the user if the LLM is unavailable
        ctx.logger.error(f"[stage5] finalize gate failed to open: {exc}")
        state["pending_approval"] = None

    state["stage"] = AWAITING_CONFIRM
    save_state(ctx, sender, state)
    await send_card(
        ctx, sender, "Here is your trip summary. Please confirm to lock it in.", review_card(summary_rows)
    )


async def _handle_confirm(ctx: Context, sender: str, text: str, state: dict[str, Any]) -> None:
    """Stage 5 dispatch: resolve the deferred-tool gate on Confirm / Start over."""
    selection = parse_selection(text)
    action = selection.get("action")
    pending = state.get("pending_approval") or {}
    tool_call_id = pending.get("tool_call_id")
    history = state.get("finalize_history")

    if action == "confirm":
        if tool_call_id and history:
            try:
                await resume_finalize(
                    history_json=history, tool_call_id=tool_call_id, approved=True
                )
            except Exception as exc:  # approval is structural; don't block the user
                ctx.logger.error(f"[stage5] resume(approve) failed: {exc}")
        itinerary = _selected_itinerary(state)
        fare_label, fare_value = _selected_fare_display(state)
        if itinerary is not None:
            leg_amounts = _selected_fare_leg_amounts(state)
            alerts = state.get("alerts") or []
            await send_card(
                ctx,
                sender,
                f"Your trip has been confirmed: {_trip_recap_line(itinerary, fare_label, fare_value)}",
                final_itinerary_card(
                    itinerary, fare_label, fare_value, leg_amounts=leg_amounts, alerts=alerts
                ),
            )
            await _send_trip_map(ctx, sender, itinerary)
        # Stage 6: keep paid + message_history, drop trip state, ready for the next one.
        clear_trip_state(state)
        state["stage"] = DONE
        save_state(ctx, sender, state)
        return

    if action == "cancel":
        if tool_call_id and history:
            try:
                await resume_finalize(
                    history_json=history, tool_call_id=tool_call_id, approved=False
                )
            except Exception as exc:
                ctx.logger.error(f"[stage5] resume(deny) failed: {exc}")
        clear_trip_state(state)
        await start_intake(ctx, sender, state)
        return

    # Free text at the review card: treat as a new/overridden trip request.
    await _handle_intake(ctx, sender, text, state)

_CARD_STAGES = {SHOWING_ROUTES, SHOWING_DETAIL, AWAITING_CONFIRM, DONE}


def _stage_handler(stage: str):
    return {
        SHOWING_ROUTES: _handle_routes,
        SHOWING_DETAIL: _handle_detail,
        AWAITING_CONFIRM: _handle_confirm,
    }.get(stage, _handle_intake)


def _fare_choices_from_state(state: dict[str, Any]) -> list[dict[str, Any]]:
    choices = []
    for o in state.get("fare_options") or []:
        secondary = f"${o['amount']:.2f}" + (" (est.)" if o.get("estimated") else "")
        choices.append({"value": o["id"], "label": o["label"], "secondary_text": secondary})
    return choices


async def _resend_current_card(ctx: Context, sender: str, state: dict[str, Any], note: str) -> None:
    """Re-render the exact card for the current stage so a side-question never
    loses the user's place. Uses only cached state - no re-fetch."""
    stage = state.get("stage")
    if stage == SHOWING_ROUTES:
        itineraries = (state.get("last_itineraries") or {}).get("itineraries") or []
        trip = state.get("trip") or {}
        await send_card(
            ctx, sender, note, route_carousel_card(itineraries, trip.get("priority", "fastest"))
        )
    elif stage == SHOWING_DETAIL:
        itinerary = _selected_itinerary(state)
        if itinerary is not None:
            await send_card(
                ctx, sender, note,
                route_detail_card(itinerary, _fare_choices_from_state(state)),
            )
    elif stage == AWAITING_CONFIRM:
        itinerary = _selected_itinerary(state)
        route_name = route_title(itinerary or {})
        fare_label, fare_value = _selected_fare_display(state)
        if fare_label is None:
            fare_label, fare_value = no_fare_labels(itinerary or {})
        rows = [
            {"label": "Route", "value": route_name},
            {"label": "Fare option", "value": fare_label},
            {"label": "Total", "value": fare_value or ""},
        ]
        await send_card(ctx, sender, note, review_card(rows))
    else:
        await send_text(ctx, sender, note)


async def _handle_escalate(ctx: Context, sender: str, text: str, state: dict[str, Any]) -> None:
    """Urgent path: skip the carousel→detail→review sequence, answer the single
    fastest option directly with a terminal card."""
    trip = state.get("trip")
    itineraries = (state.get("last_itineraries") or {}).get("itineraries") or []
    if not itineraries:
        if not trip or not trip.get("origin_coords") or not trip.get("destination_coords"):
            # No trip context yet - treat the urgent message as a new request.
            await _handle_intake(ctx, sender, text, state)
            return
        try:
            plan_obj = await plan(trip["origin_coords"], trip["destination_coords"])
            itineraries = plan_obj.get("itineraries") or []
            state["last_itineraries"] = plan_obj
        except RoutingError as exc:
            ctx.logger.error(f"[escalate] routing error: {exc}")
            await send_card(ctx, sender, "The trip planner didn't respond.", routing_error_card())
            return
    if not itineraries:
        await send_text(
            ctx,
            sender,
            "I couldn't find any transit running between those two points right now.",
        )
        return

    fastest = min(itineraries, key=lambda it: it.get("duration", 0) or 0)
    state["stage"] = DONE  # a direct urgent answer ends the flow; next msg restarts
    save_state(ctx, sender, state)
    await send_card(
        ctx,
        sender,
        f"In a hurry - fastest option right now: {_trip_recap_line(fastest, None, None)}",
        final_itinerary_card(fastest, None, None),
    )


# Paid-stage dispatch (Stages 2-6) 
async def _dispatch_paid(ctx: Context, sender: str, text: str, state: dict[str, Any]) -> None:
    """Route a paid user's message by current stage (Stages 2-6).

    A structured card selection (JSON) fast-paths straight to the stage handler.
    Free text at a card stage first runs the cross-cutting interrupt classifier
    (override / escalate / side_question / clarify / accept_default) so the user
    can type past any card at any point. INTAKE/DONE free text is always a
    (new) trip request.
    """
    stage = state.get("stage", INTAKE)

    # Fast path: a structured selection dispatches directly, no LLM call.
    if parse_selection(text).get("action") or stage not in _CARD_STAGES:
        ctx.logger.info(
            f"[dispatch] fast-path to {_stage_handler(stage).__name__} | "
            f"stage={stage!r} has_trip={bool(state.get('trip'))} "
            f"has_itineraries={bool((state.get('last_itineraries') or {}).get('itineraries'))} "
            f"text={text[:60]!r}"
        )
        await _stage_handler(stage)(ctx, sender, text, state)
        return

    # Slow path: free text mid-flow -> classify the interrupt (history attached).
    try:
        result = await classify_intent(
            text, history_json=state.get("message_history"), stage=stage
        )
    except Exception as exc:  # classifier down -> treat as a trip override, never hang
        ctx.logger.error(f"[classifier] failed, defaulting to override: {exc}")
        await _handle_override(ctx, sender, text, state)
        return

    state["message_history"] = result.history_json  # persist so follow-ups keep context
    save_state(ctx, sender, state)
    ctx.logger.info(f"[dispatch] classifier at stage={stage!r} -> {result.intent!r}")

    if result.intent == "escalate":
        await _handle_escalate(ctx, sender, text, state)
    elif result.intent == "side_question":
        note = result.reply or "Here's where we left off."
        await _resend_current_card(ctx, sender, state, note)
    elif result.intent == "clarify":
        await send_text(ctx, sender, result.reply or "Could you clarify what you'd like to do?")
    elif result.intent == "accept_default":
        await _handle_accept_default(ctx, sender, state)
    else:  # override
        await _handle_override(ctx, sender, text, state)


async def _handle_override(ctx: Context, sender: str, text: str, state: dict[str, Any]) -> None:
    selection = parse_selection(text)
    if not selection.get("action"):
        extraction = await extract_trip(text)
        if not extraction.origin and not extraction.destination:
            await _resend_current_card(
                ctx,
                sender,
                state,
                "I didn't catch a new starting point or destination there - "
                "here's where we left off. Ask me anything, or tell me both "
                "ends of a trip to change it.",
            )
            return
    clear_trip_state(state)
    state["stage"] = INTAKE
    save_state(ctx, sender, state)
    await _handle_intake(ctx, sender, text, state)


async def _handle_accept_default(ctx: Context, sender: str, state: dict[str, Any]) -> None:
    """"Just pick for me": hand the current card's decision to the agent instead
    of making the user evaluate it themselves (ux-diagnosis.md issue E).

    Each stage already has a sensible default computed server-side - the
    default fare (``_show_detail``), the fastest route (badge-eligible in
    ``route_carousel_card``) - this just acts on it via the same selection
    shape a tap would send, so it goes through the exact same code path as a
    real card interaction.
    """
    stage = state.get("stage")
    if stage == SHOWING_DETAIL:
        await _handle_detail(ctx, sender, json.dumps({"action": "continue_review"}), state)
    elif stage == SHOWING_ROUTES:
        itineraries = (state.get("last_itineraries") or {}).get("itineraries") or []
        if not itineraries:
            await _search_routes(ctx, sender, state)
            return
        durations = [it.get("duration", 0) or 0 for it in itineraries]
        fastest_idx = durations.index(min(durations))
        await _handle_routes(
            ctx, sender, json.dumps({"action": "pick_route", "route_index": fastest_idx}), state
        )
    elif stage == AWAITING_CONFIRM:
        await _handle_confirm(ctx, sender, json.dumps({"action": "confirm"}), state)
    else:
        await send_text(ctx, sender, "Sure - let's start with where you're headed.")
        await start_intake(ctx, sender, state)


# Protocol handlers 
@chat_proto.on_message(ChatMessage)
async def handle_message(ctx: Context, sender: str, msg: ChatMessage) -> None:
    await ctx.send(
        sender,
        ChatAcknowledgement(
            timestamp=datetime.now(timezone.utc), acknowledged_msg_id=msg.msg_id
        ),
    )

    is_new_window = reset_on_new_window(ctx, sender, msg)
    if is_new_window:
        await clear_watch(ctx, sender)
        await request_payment(ctx, sender, get_state(ctx, sender))
        return

    state = get_state(ctx, sender)

    has_text = any(isinstance(c, TextContent) for c in msg.content)
    if not has_text:
        return

    text = extract_text(msg)
    if not text:
        return

    if not state["paid"]:
        await _handle_unpaid(ctx, sender, text)
    else:
        await _dispatch_paid(ctx, sender, text, state)


@chat_proto.on_message(ChatAcknowledgement)
async def handle_ack(ctx: Context, sender: str, msg: ChatAcknowledgement) -> None:
    ctx.logger.debug(f"[chat] ack from {sender} for {msg.acknowledged_msg_id}")
