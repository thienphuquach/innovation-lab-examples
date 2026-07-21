"""The Pydantic AI layer - ASI:One model wiring + the conversational pieces.

Three uses of Pydantic AI live here (added stage by stage):
1. **Structured extraction** - :func:`extract_trip` turns free text into a typed
   :class:`TripExtraction` (Stage 2 free-text intake).
2. **Intent classification** - the cross-cutting interrupt classifier (added later).
3. **Deferred-tool approval** - the Stage 5 finalize gate (added later).

ASI:One is reached through Pydantic AI's OpenAI-compatible model, exactly as in
``shipping-label-agent/pydantic_agent.py``. Tests pass a ``TestModel``/
``FunctionModel`` to the run helpers so no real ASI:One call happens in CI.
"""

from __future__ import annotations

import os
from typing import Literal

from pydantic import BaseModel, Field
from pydantic_ai import Agent
from pydantic_ai.models import Model
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider

from models import PRIORITIES, now_local

ASI_ONE_BASE_URL = "https://api.asi1.ai/v1"
DEFAULT_ASI_MODEL = os.getenv("ASI_ONE_MODEL", "asi1-mini")


def build_asi1_model() -> OpenAIChatModel:
    """Build the ASI:One OpenAI-compatible chat model.

    A placeholder api_key keeps construction working in tests, where a
    ``TestModel``/``FunctionModel`` is always supplied to the run helpers.
    """
    return OpenAIChatModel(
        DEFAULT_ASI_MODEL,
        provider=OpenAIProvider(
            base_url=ASI_ONE_BASE_URL,
            api_key=os.environ.get("ASI_ONE_API_KEY") or "not-used-in-tests",
        ),
    )


_DEFAULT_MODEL = build_asi1_model()


# ── Stage 2 - free-text trip extraction ──────────────────────────────────────
class TripExtraction(BaseModel):
    """Structured target for parsing a free-text trip request."""

    origin: str = Field(
        default="",
        description="Where the trip starts. Empty string if the user didn't say.",
    )
    destination: str = Field(
        default="",
        description="Where the trip ends. Empty string if the user didn't say.",
    )
    depart_time_iso: str | None = Field(
        default=None,
        description=(
            "Absolute local departure time in ISO-8601 (e.g. 2026-07-21T18:00:00-07:00) "
            "if the user specified or implied one; otherwise null to mean 'leave now'."
        ),
    )
    priority: Literal["fastest", "fewest_transfers", "cheapest"] = Field(
        default="fastest",
        description="What to optimize for; default 'fastest' unless the user asked otherwise.",
    )


_trip_extract_agent: Agent[None, TripExtraction] = Agent(
    _DEFAULT_MODEL,
    output_type=TripExtraction,
    system_prompt=(
        "You extract a San Francisco Bay Area transit trip request from the user's "
        "message into the structured schema. Only fill origin/destination with places "
        "the user actually named; leave them empty otherwise - do not invent locations. "
        "Resolve relative times (\"in 20 min\", \"6pm\", \"tonight\") against the provided "
        "current local time and return an absolute ISO-8601 timestamp, or null for "
        "'leave now'. Choose priority only if the user expresses a preference."
    ),
)


async def extract_trip(text: str, *, model: Model | None = None) -> TripExtraction:
    """Extract a typed :class:`TripExtraction` from free text (structured output)."""
    prompt = f"Current local time: {now_local().isoformat()}\n\nUser message: {text}"
    result = await _trip_extract_agent.run(prompt, model=model)
    out = result.output
    if out.priority not in PRIORITIES:
        out.priority = "fastest"
    return out
