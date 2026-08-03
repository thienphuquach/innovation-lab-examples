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

from models import PRIORITIES

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


# Stage 2 - free-text trip extraction 
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
        "the user actually named; leave them empty otherwise - do not invent locations, "
        "and never extract a place from a question that isn't actually requesting a "
        "trip (e.g. a question about fares, Clipper, or how tapping on/off works has "
        "no origin or destination, even if it happens to contain the word \"to\"). "
        "The most common phrasing is simply \"<origin> to <destination>\" (e.g. \"SFO "
        "to Golden Gate Bridge\", \"Berkeley to the Mission\") - extract both places "
        "from that pattern even when one of them is a landmark rather than a transit "
        "stop. Every search departs now, so ignore any time the user mentions "
        "(\"at 6pm\", \"in 20 min\", \"tonight\") - never fold it into a place name. "
        "Choose priority only if the user expresses a preference."
    ),
)


async def extract_trip(text: str, *, model: Model | None = None) -> TripExtraction:
    """Extract a typed :class:`TripExtraction` from free text (structured output)."""
    result = await _trip_extract_agent.run(f"User message: {text}", model=model)
    out = result.output
    if out.priority not in PRIORITIES:
        out.priority = "fastest"
    return out


# Cross-cutting interrupt classifier 
class IntentClassification(BaseModel):
    """How to handle a free-text message that isn't a structured card selection."""

    intent: Literal["override", "escalate", "side_question", "clarify", "accept_default"] = Field(
        description=(
            "override: the user names a *concrete, different* origin and/or "
            "destination than the trip in progress (e.g. 'actually take me from "
            "Oakland instead', 'go to the Mission not downtown'). Never choose "
            "override just because the message doesn't obviously fit another "
            "category, and never because the message is confusingly worded, "
            "ungrammatical, or asks a question - those are side_question/clarify "
            "below, even with no new place named at all. "
            "escalate: urgency ('now', 'stuck', 'cancelled', 'emergency') - wants "
            "the single fastest option immediately. "
            "side_question: the user is asking something, expressing confusion, or "
            "asking for more explanation - about the current plan, paying, Clipper, "
            "or how tapping on/off works. This includes messages with typos or "
            "broken grammar ('does I need to tab in and tab off', 'I still don't "
            "know anything about clipper can you explain') - answer from context "
            "and the reference facts below, in plain, simple language for someone "
            "new to the Bay Area. "
            "clarify: the user's *intent* (not wording) is genuinely ambiguous - you "
            "cannot tell what they want done next even generously interpreted. "
            "accept_default: the user explicitly hands the current decision to the "
            "agent instead of choosing themselves ('just pick for me', 'you decide', "
            "'whatever's cheapest/fastest is fine', 'I don't mind, you choose') - not "
            "the same as clarify, since nothing here is ambiguous about what the "
            "user wants."
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


_FARE_KNOWLEDGE = (
    "Reference facts for fare/payment questions (answer from these, in 1-2 short "
    "sentences - never a paragraph): "
    "Clipper is the Bay Area's regional transit card, but it is NOT required - every "
    "Clipper agency (BART, Muni, Caltrain, AC Transit, VTA, Golden Gate, etc.) also "
    "takes a contactless bank card or phone wallet (Visa/Mastercard/Amex/Discover) at "
    "the identical price, with zero setup; cash is no longer offered. "
    "Adding Clipper to a phone (Apple Wallet or Google Wallet) is free; a physical "
    "plastic card has a one-time $3 fee, waived by signing up for Autoload - get "
    "either at clippercard.com/get, at station ticket machines, or at retailers. "
    "Add money online, in the Clipper app, at ticket machines, or at retailers - "
    "newly added value can take anywhere from a few minutes to a few days to become "
    "active depending on the method. "
    "Discount Clipper cards (50% off or more) are available for youth, seniors, "
    "riders with disabilities, and income-qualified adults (Clipper START) - arranged "
    "at clippercard.com, not something this agent issues. "
    "A lost or stolen Clipper card can be replaced for a $5 fee with its balance "
    "restored, reported at clippercard.com or by calling (877) 878-8883; a lost "
    "contactless bank card is instead handled by the rider's own bank, not Clipper. "
    "Tap-on-and-off (charges by distance, tap again when exiting or you're charged "
    "the maximum fare): BART, Caltrain, Golden Gate Transit's bus, SF Bay Ferry, "
    "SMART, Sonoma County Transit. Tap-on-only (flat fare): Muni, AC Transit, VTA, "
    "Golden Gate Ferry. Always tap the same card/phone in and out on a tap-off "
    "agency. BART's penalty for a missing or mismatched tap is $7.55. A fare marked "
    "'(est.)' means the operator prices by zone/distance and this is a "
    "representative amount, not the exact fare."
)

_fare_qa_agent: Agent[None, str] = Agent(
    _DEFAULT_MODEL,
    output_type=str,
    system_prompt=(
        "Answer a prospective rider's question about paying for Bay Area transit in "
        "1-2 short sentences, using only the facts below - never a paragraph. If the "
        "question isn't about fares, Clipper, or paying, say briefly that you can help "
        "with that once they've paid.\n\n" + _FARE_KNOWLEDGE
    ),
)


async def answer_fare_question(text: str, *, model: Model | None = None) -> str:
    """A short, grounded answer to a pre-payment question about fares/Clipper.

    Used only ahead of the Stage 0 payment gate (chat_proto._handle_unpaid), where
    no trip/route context exists yet - deliberately separate from the mid-flow
    ``_intent_agent`` below, which needs that context and classifies into intents
    that don't apply before payment (there's nothing yet to override or default).
    """
    result = await _fare_qa_agent.run(text, model=model)
    return result.output


_intent_agent: Agent[None, IntentClassification] = Agent(
    _DEFAULT_MODEL,
    output_type=IntentClassification,
    system_prompt=(
        "You triage a Bay Area transit user's free-text message. Most of the time "
        "they are mid-way through planning a trip (looking at a list of routes, a "
        "fare detail, or a final confirmation); occasionally they have just confirmed "
        "a trip (step 'done', nothing currently in progress) and are asking a "
        "follow-up question or starting a new trip. Classify their message into "
        "exactly one intent and, when the intent is side_question or clarify, provide "
        "a short reply. Use the conversation history for context. Do not invent trip "
        "details. 'override' is a narrow category - it requires a concrete new place "
        "name (at step 'done' this simply means starting the next trip, since nothing "
        "is in progress to override). If the message instead reads as a question, "
        "confusion, or a request for more detail (however it's worded, including "
        "typos or broken grammar), that is side_question, not override - wiping the "
        "user's trip in progress to answer a question they asked would be a serious "
        "mistake, and at step 'done' a question deserves a real answer rather than "
        "being treated as a malformed trip request.\n\n"
        f"{_FARE_KNOWLEDGE}"
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



def dump_history(messages: list[ModelMessage]) -> str:
    return ModelMessagesTypeAdapter.dump_json(messages).decode("utf-8")


def load_history(history_json: str) -> list[ModelMessage]:
    return list(ModelMessagesTypeAdapter.validate_json(history_json))


# Stage 5 - finalize gate (deferred tool, requires_approval) 
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
