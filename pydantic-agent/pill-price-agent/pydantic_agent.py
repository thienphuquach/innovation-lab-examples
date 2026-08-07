"""The Pydantic AI layer: intake extraction and result narration.

Model split follows the cost of being wrong. ``asi1-mini`` handles the cheap
mechanical turns - which drug, what strength, how many tablets - where a wrong
answer is caught immediately by the user. ``asi1`` writes the final price
narration, the one place where phrasing a range honestly actually takes some
judgement.

Payment gating lives entirely in ``payment.py``/``conversation.py`` now: one
upfront Stripe charge unlocks the whole agent, so there is no per-feature
approval step here for this module to gate.

Two safety properties are deliberately *not* delegated to a model:

* The dosing / medical-judgement boundary is a regex pre-check in
  :func:`crosses_medical_boundary`, run before any model call. A classifier that
  is right 99% of the time is not an acceptable gate on "should I take this?".
* Every number the user sees is computed in ``nadac.py`` and rendered into the
  card by ``chat_proto.py``. The narration model is handed those figures as
  fixed facts and told not to derive new ones, so a loose sentence can never
  contradict the authoritative card beneath it.
"""

from __future__ import annotations

import os
import re
from typing import Literal

from pydantic import BaseModel, Field
from pydantic_ai import Agent
from pydantic_ai.models import Model
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider

ASI_ONE_BASE_URL = "https://api.asi1.ai/v1"
FAST_MODEL = os.getenv("ASI_ONE_FAST_MODEL", "asi1-mini")
NARRATION_MODEL = os.getenv("ASI_ONE_MODEL", "asi1")

Intent = Literal[
    "price", "info", "more_detail", "price_trend", "brand_compare", "new_search", "other"
]


class Intake(BaseModel):
    """What the user's message is asking for, and any facts it already supplies."""

    intent: Intent = Field(description="What the user wants on this turn")
    drug_text: str | None = Field(
        default=None, description="The drug name exactly as the user wrote it, if any"
    )
    strength_text: str | None = Field(
        default=None, description="Dose strength as written, e.g. '1000 mg', if any"
    )
    ndc_text: str | None = Field(
        default=None, description="An NDC number if the user read one off a bottle"
    )
    quantity: int | None = Field(
        default=None, description="Tablet/unit count or days supply, if stated"
    )


# Questions this agent will not answer at all, checked before any model runs.
# "Should I take this" and "is this dose right for me" are clinical judgement,
# and the boundary holds regardless of which path the conversation started on.
_BOUNDARY_PATTERNS = (
    r"\bshould i (take|stop|start|keep taking|switch|double|skip)\b",
    r"\b(is|are) (this|that|these|it|the) (dose|dosage|amount|strength) (right|safe|ok|okay|correct|too (much|high|low))\b",
    r"\bhow (much|many) should i\b",
    r"\bcan i (take|combine|mix|stop|double|skip)\b",
    r"\bis it safe (for me|to)\b",
    r"\b(do|should) i need (to see|a doctor|more)\b",
    r"\bwhat dose\b.*\bfor me\b",
    r"\bdiagnos(e|is)\b",
)
_BOUNDARY_RE = re.compile("|".join(_BOUNDARY_PATTERNS), re.IGNORECASE)

BOUNDARY_REPLY = (
    "That one needs a person who can see your chart, not me. A pharmacist can "
    "answer dosing questions for free, usually on the spot, and your prescriber "
    "can change a dose if it needs changing.\n\n"
    "I can tell you what a drug costs and what its FDA label says - ask me either "
    "of those and I'll help."
)


def crosses_medical_boundary(text: str) -> bool:
    """True when the message asks for clinical judgement rather than a fact."""
    return bool(_BOUNDARY_RE.search(text or ""))


def build_model(name: str) -> OpenAIChatModel:
    """ASI:One through Pydantic AI's OpenAI-compatible model.

    The placeholder key keeps imports and construction working under tests,
    which always pass an explicit ``TestModel``/``FunctionModel`` instead.
    """
    return OpenAIChatModel(
        name,
        provider=OpenAIProvider(
            base_url=ASI_ONE_BASE_URL,
            api_key=os.environ.get("ASI_ONE_API_KEY") or "not-used-in-tests",
        ),
    )


_FAST = build_model(FAST_MODEL)
_NARRATOR = build_model(NARRATION_MODEL)


intake_agent: Agent[None, Intake] = Agent(
    _FAST,
    output_type=Intake,
    system_prompt=(
        "You classify one message from a user asking about US prescription drug "
        "prices and FDA label information, and extract any facts it contains. "
        "Copy the drug name, strength and NDC exactly as the user wrote them - do "
        "not correct spelling, expand abbreviations, or substitute a similar drug. "
        "Leave a field null when the user did not supply it; never guess a value. "
        "Intents: 'price' for what something costs, 'info' for what a drug is or "
        "does, 'more_detail' for a request to see fuller label detail on a drug "
        "already discussed, 'price_trend' for how a price changed over time, "
        "'brand_compare' for brand versus generic cost, 'new_search' when the user "
        "wants to look up a different drug or NDC but does not name one in this "
        "message (e.g. 'let's search another drug', 'something else', 'try a "
        "different one'), 'other' for anything else."
    ),
)


async def classify(text: str, *, model: Model | None = None) -> Intake:
    """Extract intent and any supplied facts from one user message."""
    result = await intake_agent.run(text, model=model)
    return result.output


narrate_agent: Agent[None, str] = Agent(
    _NARRATOR,
    output_type=str,
    system_prompt=(
        "You write one short, plain-spoken paragraph introducing a US drug price "
        "estimate that is already displayed in a card below your text.\n\n"
        "Absolute rules:\n"
        "- Use only the numbers given to you. Never calculate, round, average or "
        "invent a figure, and never state a single price when you are given a range.\n"
        "- The figure is an acquisition cost plus a typical dispensing fee, not a "
        "quote. Say so plainly rather than implying it is what a pharmacy will charge.\n"
        "- Never give medical advice, never suggest a drug is right or wrong for "
        "someone, and never comment on dosing.\n"
        "- No coupon cards, no discount programs, no upsell of any kind.\n"
        "- Two to four sentences. No headings, no bullet points, no emoji."
    ),
)


async def narrate_price(facts: str, *, model: Model | None = None) -> str:
    """Narrate a price result from pre-computed facts."""
    result = await narrate_agent.run(facts, model=model)
    return result.output
