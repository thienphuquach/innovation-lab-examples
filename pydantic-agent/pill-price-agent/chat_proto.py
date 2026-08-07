"""Chat protocol: card rendering, selection parsing, and one graph turn per message.

Cards follow the News Card Agent's list-to-detail pattern - a `card_kind:
"custom"` element tree whose list items each carry a button, and a second card
built from the same primitives when one is tapped. Price results are a natural
fit for it: the list is the set of formulations found, the detail is one of them
in full.

This module owns all IO. ``conversation.py`` decides what to say as semantic
:class:`~conversation.Reply` objects and this renders them, which is what keeps
the state machine testable without a network.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import httpx
from uagents import Context, Protocol
from uagents_core.contrib.protocols.chat import (
    ChatAcknowledgement,
    ChatMessage,
    MetadataContent,
    TextContent,
    chat_protocol_spec,
)

import conversation as conv
import drugs as drug_registry
import nadac
import openfda
import payment
import pydantic_agent as ai
from drugs import Drug
from nadac import NadacStore, PriceGroup
from openfda import Shortage
from session_state import SessionState, check_new_window_and_reset, get_state, save_state

chat_proto = Protocol(spec=chat_protocol_spec)

CARD_PROTOCOL_VERSION = "1"
_PAID_WORDS = {"paid", "done", "i've paid", "ive paid", "payment done"}
_MAX_LIST_ITEMS = 6

_store: NadacStore | None = None
_http: httpx.AsyncClient | None = None


def store() -> NadacStore:
    global _store
    if _store is None:
        _store = NadacStore()
    return _store


def http() -> httpx.AsyncClient:
    global _http
    if _http is None:
        _http = httpx.AsyncClient(timeout=30.0)
    return _http


# ---------------------------------------------------------------- sending


def _wrap(card_kind: str, payload: dict[str, Any], *, is_terminal: bool = False) -> dict[str, str]:
    meta: dict[str, str] = {
        "card_protocol_version": CARD_PROTOCOL_VERSION,
        "requires_card_interaction": "true",
        "card_kind": card_kind,
        "card_payload": json.dumps(payload),
    }
    if is_terminal:
        meta["is_terminal"] = "true"
    return meta


async def send_card(ctx: Context, sender: str, narration: str, card: dict[str, str]) -> None:
    content: list[Any] = []
    if narration:
        content.append(TextContent(type="text", text=narration))
    content.append(MetadataContent(type="metadata", metadata=card))
    await ctx.send(
        sender, ChatMessage(timestamp=datetime.now(UTC), msg_id=uuid4(), content=content)
    )


async def send_text(ctx: Context, sender: str, text: str) -> None:
    await ctx.send(
        sender,
        ChatMessage(
            timestamp=datetime.now(UTC),
            msg_id=uuid4(),
            content=[TextContent(type="text", text=text)],
        ),
    )


# ---------------------------------------------------------------- formatting


def _money(amount: float) -> str:
    return f"${amount:,.2f}"


def _per_unit(group: PriceGroup) -> str:
    return f"${group.per_unit:.5f} per {group.pricing_unit.lower()}"


def _fill_line(group: PriceGroup, quantity: int) -> str:
    low = group.fill_cost(quantity) + nadac.DISPENSING_FEE_LOW_USD
    high = group.fill_cost(quantity) + nadac.DISPENSING_FEE_HIGH_USD
    return f"{_money(low)}-{_money(high)} for {quantity}"


def _as_of_badge(group: PriceGroup) -> dict[str, str]:
    """The survey date, always beside the price.

    NADAC carries a price forward for any NDC it did not re-survey that cycle,
    so a row can legitimately be months old while the file itself is days old.
    """
    return {"type": "badge", "label": f"as of {group.as_of}", "variant": "info"}


def _fee_note() -> dict[str, str]:
    return {
        "type": "text",
        "style": "muted",
        "value": (
            f"Includes a typical {_money(nadac.DISPENSING_FEE_LOW_USD)}-"
            f"{_money(nadac.DISPENSING_FEE_HIGH_USD)} pharmacy dispensing fee on top of "
            "the NADAC acquisition cost. NADAC excludes that fee by design, so the bare "
            "acquisition number is not what any pharmacy charges."
        ),
    }


def _shortage_block(shortage: Shortage | None) -> list[dict[str, Any]]:
    """Surface a current FDA shortage record - without implying it explains
    a price for a formulation it may not actually be about.

    Confirmed live: furosemide's only current shortage record is the
    injection, while this agent prices the oral tablet - two products that
    do not share a supply chain, so a shortage cross-check that unconditionally
    says "that's the usual explanation" would be asserting a link the data
    does not establish. ``dosage_form`` is surfaced so the user can judge
    that themselves rather than the card judging it for them.
    """
    if shortage is None:
        return []
    header = (
        f"{shortage.generic_name} is on the FDA drug shortage list "
        f"({shortage.availability or 'status reported'}"
        f"{', updated ' + shortage.updated_on if shortage.updated_on else ''})."
    )
    if shortage.dosage_form:
        caveat = (
            f" The shortage record is specifically for the {shortage.dosage_form.lower()} "
            "form. If that matches what you're pricing, it's the usual explanation for a "
            "price that looks out of line; for a different form, this particular shortage "
            "most likely doesn't apply."
        )
    else:
        caveat = " That can be the explanation for a price that looks out of line."
    return [
        {"type": "divider"},
        {"type": "badge", "label": "Current FDA shortage", "variant": "warning"},
        {"type": "text", "style": "body", "value": header + caveat},
    ]


# ---------------------------------------------------------------- cards


def welcome_card() -> dict[str, str]:
    """Shown once, right after the upfront charge clears."""
    items = [
        {
            "children": [
                {"type": "heading", "value": drug.display, "level": 3},
                {
                    "type": "text",
                    "style": "muted",
                    "value": ", ".join(b.title() for b in drug.brands) or "generic only",
                },
                {
                    "type": "button",
                    "label": f"Price {drug.display}",
                    "primary": True,
                    "action": {"selection": {"drug_key": drug.key, "action": "pick_drug"}},
                },
            ]
        }
        for drug in drug_registry.CURATED
    ]
    payload = {
        "root": {
            "type": "section",
            "title": "You're in",
            "subtitle": "Honest generic drug prices and FDA label facts - no upsells, no guessing",
            "children": [
                {
                    "type": "text",
                    "style": "body",
                    "value": (
                        "Tell me a drug name (metformin, lisinopril, ...), the strength if "
                        "you have it, or read me the NDC straight off the bottle. I'll ask "
                        "for a fill quantity, then give you a price built from real NADAC "
                        "acquisition cost data plus a typical dispensing fee. I can also "
                        "pull up the FDA label, price history, and brand-vs-generic "
                        "comparison for anything on this list, all included."
                    ),
                },
                {"type": "divider"},
                {"type": "list", "items": items},
            ],
        }
    }
    return _wrap("custom", payload)


def drug_chooser_card(options: list[Drug]) -> dict[str, str]:
    items = [
        {
            "children": [
                {"type": "heading", "value": drug.display, "level": 3},
                {
                    "type": "text",
                    "style": "muted",
                    "value": ", ".join(b.title() for b in drug.brands) or "generic only",
                },
                {
                    "type": "button",
                    "label": f"Price {drug.display}",
                    "primary": True,
                    "action": {"selection": {"drug_key": drug.key, "action": "pick_drug"}},
                },
            ]
        }
        for drug in options
    ]
    payload = {
        "root": {
            "type": "section",
            "title": "Drugs I cover",
            "subtitle": "Each one has been checked for formulation price splits",
            "children": [{"type": "list", "items": items}],
        }
    }
    return _wrap("custom", payload)


def quantity_form_card(drug: Drug, strength: str | None) -> dict[str, str]:
    # The documented `form` schema is {title, fields, submit_cta} exactly - no
    # top-level subtitle key, so framing copy goes in the narration instead.
    payload = {
        "title": f"How many {drug.display}{' ' + strength if strength else ''}?",
        "fields": [
            {
                "name": "quantity",
                "kind": "number",
                "label": "Tablets or units in the fill",
                "required": True,
                "placeholder": "30",
            }
        ],
        "submit_cta": {"label": "Price it", "selection": {"action": "submit_quantity"}},
    }
    return _wrap("form", payload)


_TIER_SUBTITLE = {
    1: "Exact product from the NDC you gave",
    2: "Matched on drug name and strength",
    3: "Rough estimate across every strength",
}


def price_list_card(reply: conv.PriceList) -> dict[str, str]:
    """The list half of the pattern: one tappable item per formulation."""
    groups = reply.groups[:_MAX_LIST_ITEMS]
    items: list[dict[str, Any]] = []
    for group in groups:
        items.append(
            {
                "children": [
                    {"type": "heading", "value": group.label(), "level": 3},
                    {
                        "type": "text",
                        "style": "emphasis",
                        "value": _fill_line(group, reply.quantity),
                    },
                    {"type": "text", "style": "muted", "value": _per_unit(group)},
                    _as_of_badge(group),
                    {
                        "type": "button",
                        "label": "See this version",
                        "primary": True,
                        "action": {
                            "selection": {"group_id": group.group_id, "action": "pick_group"}
                        },
                    },
                ]
            }
        )

    children: list[dict[str, Any]] = [{"type": "list", "items": items}]

    if reply.tier == 3:
        children.append(
            {
                "type": "text",
                "style": "muted",
                "value": (
                    "This spans every strength of the drug, so treat it as a rough range. "
                    "Tell me the strength on your prescription to narrow it."
                ),
            }
        )
    elif not reply.tight and len(reply.groups) > 1:
        low, high = nadac.total_range(reply.groups, reply.quantity)
        children.append(
            {
                "type": "text",
                "style": "body",
                "value": (
                    f"These are different products, not different sellers, priced "
                    f"{_money(low)} to {_money(high)} depending on which one you are handed. "
                    "The NDC printed on the bottle pins it down exactly. Read it back to me "
                    "once the prescription is filled and I can give you the precise number."
                ),
            }
        )
    if len({g.as_of for g in reply.groups}) > 1:
        children.append(
            {
                "type": "text",
                "style": "muted",
                "value": (
                    "Heads up: these were last surveyed on different dates, so part of the "
                    "gap between them may be staleness rather than a real price difference."
                ),
            }
        )

    if reply.drug.also_priced_as and nadac.other_salt_groups(reply.groups):
        children.append(
            {
                "type": "text",
                "style": "muted",
                "value": (
                    f"The extended-release entries above are actually {reply.drug.also_priced_as}"
                    f" - a different salt than plain {reply.drug.display}, not just a pricier "
                    f"formulation of it. This agent's FDA-label lookup for "
                    f'"{reply.drug.display}" does not cover that product.'
                ),
            }
        )

    children.append(_fee_note())
    children.extend(_shortage_block(reply.shortage))
    children.append(
        {
            "type": "group",
            "direction": "row",
            "gap": 8,
            "children": [
                {
                    "type": "button",
                    "label": "What does the label say?",
                    "primary": False,
                    "action": {"selection": {"action": "drug_info"}},
                },
                {
                    "type": "button",
                    "label": "Brand vs generic",
                    "primary": False,
                    "action": {"selection": {"action": "brand_compare"}},
                },
            ],
        }
    )

    payload = {
        "root": {
            "type": "section",
            "title": f"{reply.drug.display} ({reply.quantity} units)",
            "subtitle": _TIER_SUBTITLE[reply.tier],
            "children": children,
        }
    }
    return _wrap("custom", payload)


def price_detail_card(reply: conv.PriceDetail) -> dict[str, str]:
    """The detail half: one formulation, everything known about it."""
    group = reply.group
    rows = [
        ("Estimated total", _fill_line(group, reply.quantity)),
        ("Acquisition cost", f"{_money(group.fill_cost(reply.quantity))} for {reply.quantity}"),
        ("Per unit", _per_unit(group)),
        ("Priced as of", group.as_of),
        ("Effective", group.effective_on),
        ("NDCs at this price", str(group.ndc_count)),
        ("Example NDC", group.example_ndc),
    ]
    children: list[dict[str, Any]] = [
        {
            "type": "group",
            "direction": "column",
            "gap": 6,
            "children": [
                {"type": "text", "style": "body", "value": f"{label}: {value}"}
                for label, value in rows
            ],
        },
        {"type": "divider"},
        _fee_note(),
    ]
    children.extend(_shortage_block(reply.shortage))
    children.append(
        {
            "type": "group",
            "direction": "row",
            "gap": 8,
            "children": [
                {
                    "type": "button",
                    "label": "Back to all versions",
                    "primary": False,
                    "action": {"selection": {"action": "back_to_prices"}},
                },
                {
                    "type": "button",
                    "label": "Price history",
                    "primary": True,
                    "action": {"selection": {"action": "price_trend"}},
                },
            ],
        }
    )
    payload = {
        "root": {
            "type": "section",
            "title": group.label(),
            "subtitle": f"{reply.drug.display} ({reply.quantity} units)",
            "children": children,
        }
    }
    return _wrap("custom", payload)


_INFO_LIMIT = 1400


def _label_blocks(text: str) -> list[dict[str, Any]]:
    """One card text element per paragraph/bullet in a label section.

    A single string with embedded newlines renders as one undifferentiated
    block in the card drawer, which is what made a full Medication Guide look
    like a wall of text. Splitting into elements gives real visual structure
    without touching a word of the label.
    """
    return [
        {"type": "text", "style": "body", "value": block}
        for block in openfda.paragraphs(text, limit=_INFO_LIMIT)
    ]


def _provenance_note(label: openfda.LabelText) -> dict[str, Any] | None:
    """Which one real, specific product this text came from, if known.

    Every section on the card is pinned to a single openFDA document (see
    openfda.py), but that document can still be one manufacturer's product
    among several for the drug's generic name - and, for a drug like
    metformin, either the immediate- or extended-release formulation. A user
    whose own bottle is a different manufacturer or formulation is entitled to
    know this describes a real but possibly different product, not "metformin"
    as some single averaged thing.
    """
    if not label.manufacturer and not label.product_ndc:
        return None
    bits = [
        b
        for b in (label.manufacturer, f"NDC {label.product_ndc}" if label.product_ndc else "")
        if b
    ]
    return {
        "type": "text",
        "style": "muted",
        "value": (
            f"This label is from one specific product: {', '.join(bits)}. If your own "
            "bottle is a different manufacturer or a different release type (e.g. "
            "immediate- vs. extended-release), some details here may not apply to it."
        ),
    }


def drug_info_card(reply: conv.DrugInfo) -> dict[str, str]:
    label = reply.label
    children: list[dict[str, Any]] = []
    if label is None:
        children.append(
            {
                "type": "text",
                "style": "body",
                "value": (
                    f"openFDA has no patient-facing label section on file for "
                    f"{reply.drug.display}. I won't fill that gap with my own words."
                ),
            }
        )
    else:
        children.append({"type": "badge", "label": label.source, "variant": "info"})
        children.extend(_label_blocks(label.text))
        note = _provenance_note(label)
        if note:
            children.append(note)
    if reply.drug.also_priced_as:
        children.append(
            {
                "type": "text",
                "style": "muted",
                "value": (
                    f"Note: this label only covers {reply.drug.display} as priced here. "
                    f"{reply.drug.also_priced_as} is a different salt, filed with the FDA "
                    "under its own separate label not shown by this lookup - but it is "
                    "priced separately under this same drug if you check its cost."
                ),
            }
        )
    children.append({"type": "divider"})
    children.append(
        {
            "type": "text",
            "style": "muted",
            "value": (
                "This is FDA label text, reformatted for reading and not reworded. "
                "It describes the drug; it can't tell you whether it's right for you. "
                "A pharmacist can, for free."
            ),
        }
    )
    children.extend(_shortage_block(reply.shortage))
    children.append(
        {
            "type": "group",
            "direction": "row",
            "gap": 8,
            "children": [
                {
                    "type": "button",
                    "label": "Full clinical detail",
                    "primary": False,
                    "action": {"selection": {"action": "more_detail"}},
                },
                {
                    "type": "button",
                    "label": "What does it cost?",
                    "primary": True,
                    "action": {"selection": {"action": "check_price"}},
                },
            ],
        }
    )
    payload = {
        "root": {
            "type": "section",
            "title": reply.drug.display,
            "subtitle": "From the FDA label",
            "children": children,
        }
    }
    return _wrap("custom", payload)


def drug_detail_card(reply: conv.DrugDetail) -> dict[str, str]:
    children: list[dict[str, Any]] = []
    for section in reply.sections:
        children.append({"type": "heading", "value": section.source, "level": 3})
        children.extend(_label_blocks(section.text))
        # Attached to this section specifically, not a generic card footnote -
        # only ever set on dosage_and_administration, the one section it
        # actually qualifies (see openfda._section).
        if section.formulation_note:
            children.append({"type": "text", "style": "muted", "value": section.formulation_note})
        children.append({"type": "divider"})
    # All sections are pinned to the same one document (see LookupInfo in
    # conversation.py), so the note only needs to be built from any one of them.
    if reply.sections:
        note = _provenance_note(reply.sections[0])
        if note:
            children.append(note)
            children.append({"type": "divider"})
    children.append(
        {
            "type": "button",
            "label": "Back to price",
            "primary": False,
            "action": {"selection": {"action": "check_price"}},
        }
    )
    payload = {
        "root": {
            "type": "section",
            "title": f"{reply.drug.display}: full label detail",
            "subtitle": "Verbatim FDA label sections",
            "children": children,
        }
    }
    return _wrap("custom", payload)


def trend_card(reply: conv.TrendResult) -> dict[str, str]:
    points = reply.points
    first, last = points[0], points[-1]
    change = ((last.per_unit - first.per_unit) / first.per_unit * 100) if first.per_unit else 0.0
    direction = "down" if change < 0 else "up"
    children: list[dict[str, Any]] = [
        {
            "type": "text",
            "style": "emphasis",
            "value": (
                f"{abs(change):.1f}% {direction} across {len(points)} surveys, "
                f"{first.effective_on} to {last.effective_on}."
            ),
        },
        {"type": "divider"},
        {
            "type": "group",
            "direction": "column",
            "gap": 4,
            "children": [
                {
                    "type": "text",
                    "style": "body",
                    "value": f"{point.effective_on}   ${point.per_unit:.5f}",
                }
                for point in points
            ],
        },
        {"type": "divider"},
        {
            "type": "text",
            "style": "muted",
            "value": "NADAC acquisition cost per unit, from the cached CMS file.",
        },
        {
            "type": "button",
            "label": "Back to all versions",
            "primary": False,
            "action": {"selection": {"action": "back_to_prices"}},
        },
    ]
    payload = {
        "root": {
            "type": "section",
            "title": f"Price history: {reply.group.label()}",
            "subtitle": reply.drug.display,
            "children": children,
        }
    }
    return _wrap("custom", payload)


def compare_card(reply: conv.CompareResult) -> dict[str, str]:
    items = []
    for row in reply.rows[:_MAX_LIST_ITEMS]:
        generic = row.generic_per_unit or 0.0
        multiple = (row.per_unit / generic) if generic else 0.0
        items.append(
            {
                "children": [
                    {"type": "heading", "value": row.label(), "level": 3},
                    {
                        "type": "text",
                        "style": "emphasis",
                        "value": f"Brand ${row.per_unit:.5f} vs generic ${generic:.5f} per unit",
                    },
                    {
                        "type": "badge",
                        "label": f"{multiple:.0f}x the generic" if multiple else "no multiple",
                        "variant": "warning" if multiple >= 5 else "info",
                    },
                    {"type": "text", "style": "muted", "value": f"as of {row.as_of}"},
                ]
            }
        )
    payload = {
        "root": {
            "type": "section",
            "title": f"Brand vs. generic: {reply.drug.display}",
            "subtitle": "NADAC acquisition cost, brand products with a priced generic",
            "children": [
                {"type": "list", "items": items},
                {
                    "type": "text",
                    "style": "muted",
                    "value": (
                        "Only brand products that are both priced in the current NADAC file "
                        "and have a generic equivalent price on record appear here - not "
                        "every drug's brand shows up in NADAC at all."
                    ),
                },
                {
                    "type": "button",
                    "label": "Back to all versions",
                    "primary": False,
                    "action": {"selection": {"action": "back_to_prices"}},
                },
            ],
        }
    }
    return _wrap("custom", payload)


# ---------------------------------------------------------------- parsing


def _extract_text(msg: ChatMessage) -> str:
    return " ".join(
        item.text for item in msg.content if isinstance(item, TextContent) and item.text
    ).strip()


_ACTIONS = (
    "pick_drug",
    "pick_group",
    "back_to_prices",
    "price_trend",
    "brand_compare",
    "drug_info",
    "more_detail",
    "check_price",
    "submit_quantity",
)


def parse_selection(text: str) -> dict[str, Any]:
    """Read a card selection, JSON first and prose second.

    ASI:One delivers the selection verbatim as JSON on a direct ``@mention``, but
    narrates it in prose when its planner routed the user, so both shapes have to
    parse. Anything unrecognised returns empty and is treated as free text.
    """
    stripped = (text or "").strip()
    if stripped.startswith("{") and stripped.endswith("}"):
        try:
            data = json.loads(stripped)
        except json.JSONDecodeError:
            data = None
        if isinstance(data, dict):
            return {k: v for k, v in data.items() if isinstance(k, str)}

    # Prose matching requires the underscored or hyphenated token, not the words
    # with a plain space between them. "check_price" in a planner narration is a
    # selection; "check the price of metformin" is a normal question, and reading
    # the second as the first would skip intent classification entirely.
    selection: dict[str, Any] = {}
    for action in _ACTIONS:
        if re.search(action.replace("_", r"[_-]"), stripped, re.IGNORECASE):
            selection["action"] = action
            break
    group = re.search(r"group[_\s-]?id[\s:=\"']*([A-Za-z0-9_,.\-|/ ]+)", stripped, re.IGNORECASE)
    if group:
        selection["group_id"] = group.group(1).strip().strip("\"'")
    drug = re.search(r"drug[_\s-]?key[\s:=\"']*([a-z]+)", stripped, re.IGNORECASE)
    if drug:
        selection["drug_key"] = drug.group(1).lower()
    quantity = re.search(r"quantity[\s:=\"']*(\d{1,4})", stripped, re.IGNORECASE)
    if quantity:
        selection["quantity"] = int(quantity.group(1))
    return selection


# ---------------------------------------------------------------- rendering


async def _render(ctx: Context, sender: str, replies: list[conv.Reply]) -> None:
    """Turn the graph's semantic replies into chat messages and cards.

    A payment request short-circuits everything else in the turn: ASI:One renders
    its own Stripe sheet from a bare ``RequestPayment``, and any other message
    sent from the same handler call causes that sheet to be swallowed.
    """
    ask = next((r for r in replies if isinstance(r, conv.AskUpfrontPayment)), None)
    if ask is not None:
        state = get_state(ctx, sender)
        await payment.request_payment(
            ctx, sender, state, amount_cents=ask.amount_cents, description=ask.description
        )
        return

    for reply in replies:
        if isinstance(reply, conv.Say):
            await send_text(ctx, sender, reply.text)
        elif isinstance(reply, conv.ChooseDrug):
            await send_card(ctx, sender, reply.reason, drug_chooser_card(reply.options))
        elif isinstance(reply, conv.AskQuantityReply):
            await send_card(
                ctx,
                sender,
                "NADAC prices per tablet, not per prescription, so I need the fill size "
                "before I can give you a total. How many are on the script?",
                quantity_form_card(reply.drug, reply.strength),
            )
        elif isinstance(reply, conv.PriceList):
            await send_card(ctx, sender, reply.narration, price_list_card(reply))
        elif isinstance(reply, conv.PriceDetail):
            await send_card(
                ctx, sender, f"{reply.group.label()}, in full:", price_detail_card(reply)
            )
        elif isinstance(reply, conv.QuoteComparison):
            await send_text(ctx, sender, _quote_text(reply))
        elif isinstance(reply, conv.DrugInfo):
            await send_card(
                ctx,
                sender,
                f"Here's what the FDA label says about {reply.drug.display}.",
                drug_info_card(reply),
            )
        elif isinstance(reply, conv.DrugDetail):
            await send_card(ctx, sender, "The fuller clinical sections:", drug_detail_card(reply))
        elif isinstance(reply, conv.TrendResult):
            await send_card(ctx, sender, "Here's the price history.", trend_card(reply))
        elif isinstance(reply, conv.CompareResult):
            await send_card(ctx, sender, "Here's brand versus generic.", compare_card(reply))


def _quote_text(reply: conv.QuoteComparison) -> str:
    """Compare a real quote to the estimate as a percentage, never as a verdict.

    The estimate is a range, so "you were ripped off" is not a claim the data can
    support - the honest statement is where the quote falls relative to the band.
    """
    if reply.quoted_usd < reply.low_usd:
        delta = (reply.low_usd - reply.quoted_usd) / reply.low_usd * 100
        return (
            f"{_money(reply.quoted_usd)} is about {delta:.0f}% below the bottom of my estimate "
            f"({_money(reply.low_usd)}-{_money(reply.high_usd)}). That's a good price by this "
            "measure. Worth saying my estimate is a national average plus a typical dispensing "
            "fee, not a quote for your pharmacy."
        )
    if reply.quoted_usd > reply.high_usd:
        delta = (reply.quoted_usd - reply.high_usd) / reply.high_usd * 100
        return (
            f"{_money(reply.quoted_usd)} is about {delta:.0f}% above the top of my estimate "
            f"({_money(reply.low_usd)}-{_money(reply.high_usd)}). That gap is worth asking about, "
            "though a pharmacy's own acquisition cost and dispensing fee legitimately vary from "
            "the national average, so it isn't proof of anything on its own."
        )
    return (
        f"{_money(reply.quoted_usd)} sits inside my estimate of {_money(reply.low_usd)}-"
        f"{_money(reply.high_usd)}, so it looks in line with what the data would predict."
    )


# ---------------------------------------------------------------- handlers


async def _run_turn(ctx: Context, sender: str, state: SessionState, deps: conv.TurnDeps) -> None:
    await conv.advance(state, deps)
    save_state(ctx, sender, state)
    await _render(ctx, sender, deps.outbox)


@chat_proto.on_message(ChatMessage)
async def handle_message(ctx: Context, sender: str, msg: ChatMessage) -> None:
    await ctx.send(
        sender,
        ChatAcknowledgement(timestamp=datetime.now(UTC), acknowledged_msg_id=msg.msg_id),
    )
    try:
        await _handle_inner(ctx, sender, msg)
    except Exception:
        ctx.logger.exception("[chat] turn failed")
        await send_text(ctx, sender, "Something went wrong on my end. Try that again in a moment.")


async def _handle_inner(ctx: Context, sender: str, msg: ChatMessage) -> None:
    check_new_window_and_reset(ctx, sender)
    text = _extract_text(msg)
    if not text:
        return

    state = get_state(ctx, sender)
    ctx.logger.info(f"[chat] {sender} | node={state.node} | {text[:80]}")

    # The dosing boundary is checked before anything else and holds no matter
    # which path the conversation is on - a medical-judgement question does not
    # get a pass because it arrived mid price check.
    if ai.crosses_medical_boundary(text):
        await send_text(ctx, sender, ai.BOUNDARY_REPLY)
        return

    typed_paid = text.strip().lower() in _PAID_WORDS and state.stripe_session_id
    if typed_paid and await payment.confirm_payment_via_text(ctx, sender):
        return

    selection = parse_selection(text)
    deps = conv.TurnDeps(text=text, selection=selection, store=store(), http=http())
    # Nothing before the paywall reads intake, so an unpaid session skips the
    # classification call entirely rather than spending it on a message the
    # graph won't act on until the charge clears.
    if not selection and state.stripe_paid:
        try:
            deps.intake = await ai.classify(text)
        except Exception as exc:  # noqa: BLE001 - fall back to plain name resolution
            ctx.logger.warning(f"[chat] intake classification failed: {exc}")

    await _run_turn(ctx, sender, state, deps)


async def deliver_access_payment(
    ctx: Context, sender: str, state: SessionState, *, approved: bool
) -> None:
    """Resolve the upfront charge: unlock the agent, or hard-stop and re-arm.

    Nothing was running while the payment was pending - the paywall sits
    before any drug/intent processing - so there is no partial result to
    resume, just a gate to open or leave shut.
    """
    state.stripe_session_id = None

    if not approved:
        state.node = "Paywall"
        save_state(ctx, sender, state)
        await send_text(
            ctx,
            sender,
            "No charge went through, so nothing is unlocked. Send another message "
            "whenever you're ready to pay and get started.",
        )
        return

    state.stripe_paid = True
    state.node = "Start"
    save_state(ctx, sender, state)
    await send_card(
        ctx,
        sender,
        "Payment confirmed - here's how this works.",
        welcome_card(),
    )


@chat_proto.on_message(ChatAcknowledgement)
async def handle_ack(ctx: Context, sender: str, msg: ChatAcknowledgement) -> None:
    ctx.logger.debug(f"[chat] ack from {sender} for {msg.acknowledged_msg_id}")
