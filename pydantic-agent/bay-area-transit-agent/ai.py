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
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, Field
from pydantic_ai import (
    Agent,
    DeferredToolRequests,
    DeferredToolResults,
    RunContext,
    ToolDenied,
)
from pydantic_ai.messages import ModelMessage, ModelMessagesTypeAdapter
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


# ── Cross-cutting interrupt classifier ───────────────────────────────────────
class IntentClassification(BaseModel):
    """How to handle a free-text message that isn't a structured card selection."""

    intent: Literal["override", "escalate", "side_question", "clarify", "accept_default"] = Field(
        description=(
            "override: user states a different origin/destination than the one in "
            "progress. escalate: urgency ('now', 'stuck', 'cancelled', 'emergency') - "
            "wants the single fastest option immediately. side_question: a question "
            "about the current plan, not a new request. clarify: genuinely ambiguous "
            "input that needs one direct question back. accept_default: the user "
            "explicitly hands the current decision to the agent instead of choosing "
            "themselves ('just pick for me', 'you decide', 'whatever's cheapest/"
            "fastest is fine', 'I don't mind, you choose') - not the same as clarify, "
            "since nothing here is ambiguous about what the user wants."
        )
    )
    reply: str = Field(
        default="",
        description=(
            "For side_question: a concise answer from context. For clarify: one direct "
            "clarifying question. Empty for override/escalate."
        ),
    )


@dataclass
class IntentResult:
    intent: str
    reply: str
    history_json: str  # updated history to persist so follow-ups keep context


_intent_agent: Agent[None, IntentClassification] = Agent(
    _DEFAULT_MODEL,
    output_type=IntentClassification,
    system_prompt=(
        "You triage a Bay Area transit user's free-text message while they are "
        "mid-way through planning a trip (they may be looking at a list of routes, a "
        "fare detail, or a final confirmation). Classify their message into exactly "
        "one intent and, when the intent is side_question or clarify, provide a short "
        "reply. Use the conversation history for context. Do not invent trip details."
    ),
)


async def classify_intent(
    text: str,
    *,
    history_json: str | None = None,
    stage: str | None = None,
    model: Model | None = None,
) -> IntentResult:
    """Classify a mid-flow free-text message, carrying session history for context."""
    history = load_history(history_json) if history_json else None
    prompt = f"Current step: {stage}\n\nUser message: {text}"
    result = await _intent_agent.run(prompt, message_history=history, model=model)
    out = result.output
    return IntentResult(
        intent=out.intent, reply=out.reply, history_json=dump_history(result.all_messages())
    )


# ── message_history (de)serialization ────────────────────────────────────────
# Pydantic AI ModelMessages need the type adapter, not plain json.dumps
# (research-notes.md §5). These back the session's ``message_history`` field.
def dump_history(messages: list[ModelMessage]) -> str:
    return ModelMessagesTypeAdapter.dump_json(messages).decode("utf-8")


def load_history(history_json: str) -> list[ModelMessage]:
    return list(ModelMessagesTypeAdapter.validate_json(history_json))


# ── Stage 5 - finalize gate (deferred tool, requires_approval) ───────────────
@dataclass
class FinalizeDeps:
    """Injected so the gated tool can flip a confirmation flag on approval."""

    confirmed: bool = False


@dataclass
class FinalizeStart:
    """Outcome of the first finalize run (the tool defers before it runs)."""

    deferred: bool
    history_json: str
    tool_call_id: str | None = None


_finalize_agent: Agent[FinalizeDeps, str | DeferredToolRequests] = Agent(
    _DEFAULT_MODEL,
    deps_type=FinalizeDeps,
    output_type=[str, DeferredToolRequests],
    system_prompt=(
        "You finalize a Bay Area transit itinerary the user has already chosen by "
        "calling the finalize_itinerary tool exactly once. After it returns, reply "
        "with a one-line confirmation. Never invent trip details."
    ),
)


@_finalize_agent.tool(requires_approval=True)
async def finalize_itinerary(ctx: RunContext[FinalizeDeps], summary: str) -> str:
    """Commit the chosen itinerary (no external side effect - payment happened at Stage 0).

    Gated by ``requires_approval=True``: the body only runs after an explicit
    approval, which is exactly the Review card's Confirm action.
    """
    ctx.deps.confirmed = True
    return f"Itinerary confirmed: {summary}"


async def start_finalize(summary: str, *, model: Model | None = None) -> FinalizeStart:
    """First finalize run: the model calls the gated tool, which defers.

    Returns the serialized history + the pending tool-call id so the caller can
    resume with the user's Confirm/Cancel on the next chat turn.
    """
    result = await _finalize_agent.run(
        f"Finalize this itinerary: {summary}", deps=FinalizeDeps(), model=model
    )
    history_json = dump_history(result.all_messages())
    output = result.output
    if isinstance(output, DeferredToolRequests) and output.approvals:
        return FinalizeStart(
            deferred=True,
            history_json=history_json,
            tool_call_id=output.approvals[0].tool_call_id,
        )
    return FinalizeStart(deferred=False, history_json=history_json)


async def resume_finalize(
    *,
    history_json: str,
    tool_call_id: str,
    approved: bool,
    denial_message: str = "The user cancelled before confirming.",
    model: Model | None = None,
) -> bool:
    """Resume the deferred finalize with the user's decision.

    Returns ``True`` iff the approval ran the gated tool body.
    """
    deps = FinalizeDeps()
    results = DeferredToolResults()
    results.approvals[tool_call_id] = True if approved else ToolDenied(denial_message)
    await _finalize_agent.run(
        message_history=load_history(history_json),
        deferred_tool_results=results,
        deps=deps,
        model=model,
    )
    return deps.confirmed
