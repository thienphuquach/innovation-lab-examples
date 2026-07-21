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

from datetime import datetime, timezone
from typing import Any

from uagents import Context, Protocol
from uagents_core.contrib.protocols.chat import (
    ChatAcknowledgement,
    ChatMessage,
    StartSessionContent,
    TextContent,
    chat_protocol_spec,
)

from ai import extract_trip
from cards import (
    disambiguation_carousel_card,
    extract_text,
    intake_form_card,
    parse_selection,
    route_carousel_card,
    route_detail_card,
    routing_error_card,
    send_card,
    send_text,
    terminal_info_card,
)
from clients.five11 import alerts_for_routes, load_fare_data
from clients.geocode import resolve_place
from clients.transitland import RoutingError, plan, transit_legs
from fares import compute_fare_options
from models import depart_option_to_iso, new_trip, validate_trip_texts
from payment import confirm_payment_via_text, request_payment
from session_state import (
    AWAITING_CONFIRM,
    INTAKE,
    SHOWING_DETAIL,
    SHOWING_ROUTES,
    check_new_window_and_reset,
    get_state,
    save_state,
)

chat_proto = Protocol(spec=chat_protocol_spec)

# Text a user might type to force a manual payment re-check (fallback path only;
# normally ASI:One sends CommitPayment automatically once Stripe settles).
_PAID_WORDS = {"paid", "done", "i paid", "paid!", "finished", "complete", "completed"}


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
            "Payment confirmed - you're unlocked! Where are you headed? Fill in the trip "
            'form, or just tell me in your own words (e.g. "Berkeley to the Mission at 6pm").'
        )
    else:
        narration = "Let's plan a trip. Fill in the form, or just describe it in a sentence."
    await send_card(ctx, sender, narration, intake_form_card())


# ── Stage 0 - payment gate ───────────────────────────────────────────────────
async def _handle_unpaid(ctx: Context, sender: str, text: str, state: dict[str, Any]) -> None:
    """Gate every unpaid message. No functionality is reachable before payment."""
    if text.lower().strip() in _PAID_WORDS:
        if await confirm_payment_via_text(ctx, sender):
            return
        await send_text(
            ctx,
            sender,
            "I don't see a completed payment yet. Please finish the Stripe checkout above.",
        )
        return
    # Any other message => (re)issue a fresh Stripe checkout gate.
    await request_payment(ctx, sender, state)


# ── Stage 2 - trip intake (form + free-text paths) ───────────────────────────
async def _handle_intake(ctx: Context, sender: str, text: str, state: dict[str, Any]) -> None:
    """Collect a trip request from either the form card or free text.

    If a geocoding disambiguation is in progress (``pending_trip`` set), this
    message either picks a candidate or retypes the place being resolved.
    """
    selection = parse_selection(text)

    # Mid-geocoding: a carousel pick resolves the current field.
    if state.get("pending_trip") and selection.get("action") == "pick_place":
        await _apply_place_pick(ctx, sender, state, selection)
        return

    if selection.get("action") == "submit_trip":
        origin = str(selection.get("origin", "") or "")
        destination = str(selection.get("destination", "") or "")
        priority = str(selection.get("priority", "fastest") or "fastest")
        depart_time = depart_option_to_iso(selection.get("depart_option"))
        if selection.get("depart_option") == "custom" and depart_time is None:
            # We can't invent a time; ask for it (free-text extractor handles it).
            state["pending_trip"] = None
            save_state(ctx, sender, state)
            await send_text(
                ctx,
                sender,
                "Sure - what time? Tell me the whole trip with a time, e.g. "
                '"Berkeley to the Mission at 6:15pm".',
            )
            return
    else:
        # Free-text path -> Pydantic AI structured extraction.
        extraction = await extract_trip(text)
        origin, destination = extraction.origin, extraction.destination
        priority = extraction.priority
        depart_time = extraction.depart_time_iso

    error = validate_trip_texts(origin, destination)
    if error:
        await start_intake(ctx, sender, state, error=error)
        return

    state["pending_trip"] = new_trip(
        origin_text=origin,
        destination_text=destination,
        depart_time=depart_time,
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

    # Ambiguous -> ask the user to pick.
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
async def _search_routes(ctx: Context, sender: str, state: dict[str, Any]) -> None:
    """Call Transitland and render the itinerary carousel (cached for Back)."""
    trip = state.get("trip")
    if not trip or not trip.get("origin_coords") or not trip.get("destination_coords"):
        await start_intake(ctx, sender, state)
        return

    try:
        plan_obj = await plan(
            trip["origin_coords"], trip["destination_coords"], trip.get("depart_time")
        )
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
    if not itineraries:
        await send_card(
            ctx,
            sender,
            "No routes found for that time.",
            terminal_info_card(
                "No routes found",
                "I couldn't find any transit for that time. Try a different departure time.",
            ),
        )
        await start_intake(ctx, sender, state)
        return

    state["last_itineraries"] = plan_obj  # cached so Back needs no re-fetch
    state["stage"] = SHOWING_ROUTES
    save_state(ctx, sender, state)

    n = len(itineraries)
    lead = (
        "Here's a walking route." if n == 1 and not _has_transit(itineraries[0])
        else f"Found {n} option{'s' if n != 1 else ''}."
    )
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
    try:
        idx = int(state.get("selected_route_id"))
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
        options = compute_fare_options(itinerary.get("legs", []), fare_data)
    except Exception as exc:  # fares are best-effort; never block the trip on them
        ctx.logger.error(f"[stage4] fare computation failed: {exc}")
        options = []

    legs = transit_legs(itinerary)
    route_ids = {leg.get("routeId") for leg in legs if leg.get("routeId")}
    agency_ids = {leg.get("agencyId") for leg in legs if leg.get("agencyId")}
    alerts = await alerts_for_routes(route_ids, agency_ids)

    choices = [o.to_choice() for o in options]
    state["fare_options"] = [
        {"id": o.id, "label": o.label, "amount": o.amount, "estimated": o.estimated}
        for o in options
    ]
    # Default the selection to the cheapest option (options are sorted).
    state["selected_fare_option"] = options[0].id if options else None
    state["stage"] = SHOWING_DETAIL
    save_state(ctx, sender, state)

    lead = "Here's the route with fares and any live alerts."
    if options and options[0].estimated:
        lead += " Fares marked (est.) are approximate where an operator uses zone pricing."
    await send_card(ctx, sender, lead, route_detail_card(itinerary, choices, alerts))


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
        state["stage"] = AWAITING_CONFIRM
        save_state(ctx, sender, state)
        # Stage 5 (review + deferred-tool gate) replaces this stub in its own commit.
        await send_text(
            ctx,
            sender,
            "Ready to confirm... (the review & confirm step lands in the next build stage).",
        )
        return

    # A bare fare pick with no CTA: acknowledge and keep the card in place.
    if selection.get("fare"):
        await send_text(
            ctx, sender, "Got it - tap \"Confirm this trip\" when you're ready."
        )
        return

    # Free text at the detail card: treat as a new/overridden trip request.
    await _handle_intake(ctx, sender, text, state)


# ── Paid-stage dispatch (Stages 2-6) ─────────────────────────────────────────
async def _dispatch_paid(ctx: Context, sender: str, text: str, state: dict[str, Any]) -> None:
    """Route a paid user's message by current stage.

    Stages 3-6 are added in their own commits; until then a resolved trip stops
    at the Stage 3 seam.
    """
    # TODO(interrupt-classifier): run the cross-cutting classifier here first.
    stage = state.get("stage", INTAKE)
    if stage == INTAKE:
        await _handle_intake(ctx, sender, text, state)
    elif stage == SHOWING_ROUTES:
        await _handle_routes(ctx, sender, text, state)
    elif stage == SHOWING_DETAIL:
        await _handle_detail(ctx, sender, text, state)
    else:
        # Stages 5+ not yet wired; restart intake so the flow stays usable.
        await start_intake(ctx, sender, state)


# ── Protocol handlers ────────────────────────────────────────────────────────
@chat_proto.on_message(ChatMessage)
async def handle_message(ctx: Context, sender: str, msg: ChatMessage) -> None:
    await ctx.send(
        sender,
        ChatAcknowledgement(
            timestamp=datetime.now(timezone.utc), acknowledged_msg_id=msg.msg_id
        ),
    )

    check_new_window_and_reset(ctx, sender)
    state = get_state(ctx, sender)

    # A brand-new conversation (StartSession) from an unpaid sender => gate.
    if any(isinstance(c, StartSessionContent) for c in msg.content) and not state["paid"]:
        await request_payment(ctx, sender, state)
        return

    has_text = any(isinstance(c, TextContent) for c in msg.content)
    if not has_text:
        return

    text = extract_text(msg)
    if not text:
        return

    if not state["paid"]:
        await _handle_unpaid(ctx, sender, text, state)
    else:
        await _dispatch_paid(ctx, sender, text, state)


@chat_proto.on_message(ChatAcknowledgement)
async def handle_ack(ctx: Context, sender: str, msg: ChatAcknowledgement) -> None:
    ctx.logger.debug(f"[chat] ack from {sender} for {msg.acknowledged_msg_id}")
