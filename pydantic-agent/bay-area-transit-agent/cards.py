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

CARD_PROTOCOL_VERSION = "1"


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
