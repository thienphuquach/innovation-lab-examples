"""The conversation as a typed state machine, built on ``pydantic_graph`` nodes.

``pydantic_graph`` 2.x removed the resumable runner and its persistence package,
so this module uses the parts that survived - ``BaseNode``, ``GraphRunContext``
and ``End``, none of which are deprecated - as the vocabulary for a machine this
module drives itself, one transition per chat turn, with position and state held
in ``ctx.storage``. That keeps the branching declared per stage instead of
sprawling into an if/else chain, and keeps exactly one state store.

Nodes never touch the network directly and never build cards. They read facts
through :class:`TurnDeps` and append semantic :class:`Reply` objects to an
outbox, which ``chat_proto.py`` renders. That split is what lets the whole
machine be tested offline.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Any

import httpx
from pydantic_ai.models import Model
from pydantic_graph import BaseNode, End, GraphRunContext

import drugs
import nadac
import openfda
import pydantic_agent as ai
from drugs import Drug
from nadac import NadacStore, PriceGroup
from openfda import LabelText, Shortage
from session_state import SessionState, group_by_id

# ---------------------------------------------------------------- replies


@dataclass
class Reply:
    """Something the agent wants to say this turn, before it becomes a card."""


@dataclass
class Say(Reply):
    text: str


@dataclass
class ChooseDrug(Reply):
    """The drug wasn't recognised - offer the curated list rather than guess."""

    reason: str
    options: list[Drug]


@dataclass
class AskQuantityReply(Reply):
    drug: Drug
    strength: str | None


@dataclass
class PriceList(Reply):
    """Tier 1/2/3 price result. ``tight`` decides one number versus a range."""

    drug: Drug
    tier: int
    quantity: int
    groups: list[PriceGroup]
    tight: bool
    narration: str
    shortage: Shortage | None


@dataclass
class PriceDetail(Reply):
    drug: Drug
    quantity: int
    group: PriceGroup
    shortage: Shortage | None


@dataclass
class QuoteComparison(Reply):
    """The user's real-world quote measured against the estimate range."""

    drug: Drug
    quoted_usd: float
    low_usd: float
    high_usd: float


@dataclass
class DrugInfo(Reply):
    drug: Drug
    label: LabelText | None
    shortage: Shortage | None


@dataclass
class DrugDetail(Reply):
    drug: Drug
    sections: list[LabelText]


@dataclass
class TrendResult(Reply):
    drug: Drug
    group: PriceGroup
    points: list[nadac.PricePoint]


@dataclass
class CompareResult(Reply):
    drug: Drug
    rows: list[PriceGroup]


@dataclass
class AskUpfrontPayment(Reply):
    """Hand off to the payment protocol for the one-time, whole-agent unlock."""

    amount_cents: int
    description: str


# ---------------------------------------------------------------- deps


@dataclass
class TurnDeps:
    """Everything one turn needs, plus the outbox it fills."""

    text: str
    selection: dict[str, Any]
    store: NadacStore
    http: httpx.AsyncClient
    outbox: list[Reply] = field(default_factory=list)
    # Tests pass explicit Pydantic AI models; production leaves these None so
    # each agent uses its configured ASI:One model.
    fast_model: Model | None = None
    narration_model: Model | None = None
    intake: ai.Intake | None = None

    def say(self, reply: Reply) -> None:
        self.outbox.append(reply)


Ctx = GraphRunContext[SessionState, TurnDeps]

# One charge unlocks the whole agent for the session - price checks, drug
# info, price trend history, and brand-vs-generic comparison alike. There is
# no separate per-feature charge once this has cleared.
UPFRONT_PRICE_CENTS = int(os.getenv("STRIPE_AMOUNT_CENTS", "200"))
UPFRONT_DESCRIPTION = "Full access"


# ---------------------------------------------------------------- nodes


class Node(BaseNode[SessionState, TurnDeps, None]):
    """Base for every node in this machine.

    A node either returns the next node - the machine follows that immediately,
    within the same user turn - or calls :meth:`pause`, which records where to
    resume and ends the turn. Making the pause explicit rather than inferring it
    from the node type is what keeps "present a card, then wait" a single node
    instead of two.
    """

    def pause(self, ctx: Ctx, node: type[Node] | None = None) -> End[None]:
        """End the turn, resuming at ``node`` (default: this node) next time."""
        ctx.state.node = (node or type(self)).__name__
        return End(None)


class WaitNode(Node):
    """A node the machine can be resumed at, i.e. one that reads user input."""


async def _shortage_for(deps: TurnDeps, drug: Drug) -> Shortage | None:
    """Cross-check the FDA shortage list, tolerating an outage.

    A shortage is context, never the answer, so a failed lookup degrades to "no
    shortage mentioned" rather than failing the price check the user asked for.
    """
    try:
        return await openfda.fetch_shortage(deps.http, drug.fda_generic_name)
    except openfda.OpenFdaError:
        return None


def _drug(state: SessionState) -> Drug | None:
    return drugs.BY_KEY.get(state.drug_key) if state.drug_key else None


def _known_ndc(state: SessionState) -> str | None:
    """The one specific real product already pinned down for the current
    drug, if any - an exact Tier 1 NDC lookup (``state.groups`` holds exactly
    that one product) or a formulation the user tapped from a Tier 2/3 list
    (``state.selected_group_id``). ``None`` means the drug is still only known
    by name, so any FDA document resolved for it has no ground truth to
    confirm it actually matches what the user has in hand.
    """
    if len(state.groups) == 1:
        return state.groups[0].example_ndc
    if state.selected_group_id:
        group = group_by_id(state, state.selected_group_id)
        if group:
            return group.example_ndc
    return None


@dataclass
class Paywall(Node):
    """Gate the whole agent behind one upfront charge.

    Every fresh session starts here, before any drug/intent processing - a
    bare "hi" triggers the checkout exactly like a real drug question would.
    Once ``state.stripe_paid`` is set (by the payment handlers in
    ``payment.py``, never by this node itself), it steps straight through to
    ``Start`` and is never visited again for the rest of the session.
    """

    async def run(self, ctx: Ctx) -> Node | End[None]:
        deps, state = ctx.deps, ctx.state
        if state.stripe_paid:
            return Start()
        deps.say(
            AskUpfrontPayment(amount_cents=UPFRONT_PRICE_CENTS, description=UPFRONT_DESCRIPTION)
        )
        return self.pause(ctx, AwaitingAccessPayment)


@dataclass
class AwaitingAccessPayment(WaitNode):
    """Parked until ``payment.py`` reports the Stripe outcome for the unlock charge."""

    async def run(self, ctx: Ctx) -> Node | End[None]:
        ctx.deps.say(
            Say(
                "I'm still waiting on that payment to unlock the agent. I'm checking "
                "Stripe in the background and will let you in automatically within a "
                "few seconds of the checkout clearing, no reply needed. If you've "
                'already paid and want in sooner, type "paid." You can also cancel '
                "the Stripe sheet instead."
            )
        )
        return self.pause(ctx)


@dataclass
class Start(Node):
    """Absorb whatever facts the message carried, then route."""

    async def run(self, ctx: Ctx) -> Node | End[None]:
        deps, state = ctx.deps, ctx.state
        intake = deps.intake

        if intake and intake.ndc_text:
            parsed_ndc = drugs.parse_ndc(intake.ndc_text) or drugs.parse_ndc(deps.text)
            if parsed_ndc:
                state.ndc = parsed_ndc
            else:
                # The user was clearly trying to give an NDC, not a name - say so
                # rather than silently falling through to whatever drug/price was
                # already in state, which would look like the input was ignored.
                deps.say(
                    Say(
                        "That doesn't look like a complete NDC - it needs three "
                        "groups of digits (e.g. 0002-3227-30) or eleven digits run "
                        "together. Try the full number off the bottle, or just give "
                        "me the drug name."
                    )
                )
                return self.pause(ctx, AskDrug)
        else:
            # The classifier can miss an NDC embedded in natural language the
            # same way it can miss a drug name (see the deterministic name
            # backstop below) - but unlike the name case, nothing else in this
            # flow retries NDC parsing on the raw text once a drug is already
            # active in state: AskDrug has its own parse_ndc fallback, but
            # Start only ever routes there when state.drug_key is still None
            # (below), so a missed extraction with a drug already set would
            # otherwise fall straight through to whatever was already in
            # state - the exact failure mode confirmed live for a typed
            # sentence wrapping an NDC after a drug was already picked.
            # drugs.parse_ndc's pattern (three separated digit groups, or
            # exactly 11 bare digits) is specific enough that running it
            # unconditionally on free text is safe.
            backstop_ndc = drugs.parse_ndc(deps.text)
            if backstop_ndc:
                state.ndc = backstop_ndc

        switched_drug = False
        if intake and intake.drug_text:
            found = drugs.resolve(intake.drug_text)
            if found:
                # Switching drugs mid-conversation must not inherit the previous
                # drug's strength, selected formulation, or pinned FDA label
                # document - a leftover label_set_id would describe the old
                # drug entirely, not a mix, but it would be the wrong drug.
                if found.key != state.drug_key:
                    state.strength = None
                    state.groups = []
                    state.selected_group_id = None
                    state.label_set_id = None
                    state.label_exact = False
                state.drug_key = found.key
                switched_drug = True

        # The classifier is a model call and can miss a name it was handed
        # verbatim ("how about advil?" is idiomatic, not a direct name), or fail
        # outright - intake is None whenever that happens (see chat_proto.py).
        # A plain token match against the raw message is deterministic and free,
        # so it runs as a backstop whenever the model didn't already switch the
        # drug - the same check AskDrug already relies on with no model at all.
        if not switched_drug:
            found = drugs.resolve(deps.text)
            if found and found.key != state.drug_key:
                state.strength = None
                state.groups = []
                state.selected_group_id = None
                state.label_set_id = None
                state.label_exact = False
                state.drug_key = found.key
                switched_drug = True

        if intake and intake.strength_text:
            state.strength = drugs.parse_strength(intake.strength_text)
        if intake and intake.quantity:
            state.quantity = intake.quantity

        # An NDC identifies the exact product, so it needs no drug name.
        if state.ndc:
            return LookupPrice()

        intent = intake.intent if intake else "price"

        # "Search another drug" with no drug named yet - ask rather than silently
        # re-running the previous lookup, which is what made a topic switch look
        # like it had no effect at all.
        if intent == "new_search" and not switched_drug:
            state.strength = None
            state.groups = []
            state.selected_group_id = None
            state.label_set_id = None
            state.label_exact = False
            deps.say(
                ChooseDrug(
                    reason="Sure - which drug or NDC would you like to check next?",
                    options=list(drugs.CURATED),
                )
            )
            return self.pause(ctx, AskDrug)

        if state.drug_key is None:
            found = drugs.resolve(deps.text)
            if found is None:
                return AskDrug()
            state.drug_key = found.key

        if intent in {"info", "more_detail"}:
            return LookupInfo(detail=intent == "more_detail")
        if intent == "price_trend":
            return RunPriceTrend()
        if intent == "brand_compare":
            return RunBrandCompare()
        return LookupPrice()


@dataclass
class AskDrug(WaitNode):
    """No recognised drug yet. Ask - never fuzzy-match into a different one."""

    async def run(self, ctx: Ctx) -> Node | End[None]:
        deps, state = ctx.deps, ctx.state
        picked = deps.selection.get("drug_key")
        found = drugs.BY_KEY.get(picked) if isinstance(picked, str) else None

        # A user re-typing an NDC that missed last turn (or giving one for the
        # first time here) must get the real NADAC-file answer, not "couldn't
        # match to a drug" - which is only true of a drug *name*, not an NDC.
        if found is None:
            ndc = drugs.parse_ndc(deps.text)
            if ndc:
                state.ndc = ndc
                return LookupPrice()

        if found is None:
            found = drugs.resolve(deps.text)

        if found is None:
            typed = deps.text.strip()[:60]
            deps.say(
                ChooseDrug(
                    reason=(
                        f"I couldn't match \u201c{typed}\u201d to a drug I cover, and I'd "
                        "rather ask than guess at a similar name."
                        if typed
                        else "Which drug did you want?"
                    )
                    + " Every drug on this list has been checked for the formulation "
                    "price splits that make a naive lookup wrong, which is why it's short "
                    "rather than open-ended.",
                    options=list(drugs.CURATED),
                )
            )
            return self.pause(ctx)

        if found.key != state.drug_key:
            state.label_set_id = None
            state.label_exact = False
        state.drug_key = found.key
        state.strength = state.strength or drugs.parse_strength(deps.text)
        return LookupPrice()


@dataclass
class AskQuantity(WaitNode):
    """NADAC prices per tablet, so a fill size is required before any total."""

    async def run(self, ctx: Ctx) -> Node | End[None]:
        deps, state = ctx.deps, ctx.state
        raw = deps.selection.get("quantity") or deps.text
        quantity = _parse_quantity(str(raw))
        if quantity is None:
            drug = _drug(state)
            if drug:
                deps.say(AskQuantityReply(drug=drug, strength=state.strength))
            return self.pause(ctx)
        state.quantity = quantity
        return LookupPrice()


_QUANTITY_RE = re.compile(r"(\d{1,4})\s*(?:tabs?|tablets?|caps?|capsules?|pills?|units?|days?)?")


def _parse_quantity(text: str) -> int | None:
    """A tablet count or days supply, if the text plausibly contains one."""
    match = _QUANTITY_RE.search(text.replace(",", ""))
    if not match:
        return None
    value = int(match.group(1))
    return value if 1 <= value <= 1000 else None


@dataclass
class LookupPrice(Node):
    """Resolve the tier, read the cache, and hand the result to ShowPrices."""

    async def run(self, ctx: Ctx) -> Node | End[None]:
        deps, state = ctx.deps, ctx.state

        if state.ndc:
            group = deps.store.by_ndc(state.ndc)
            state.ndc = None
            if group is None:
                deps.say(
                    Say(
                        "That NDC isn't in the current NADAC file for any drug I cover. "
                        "Double-check the digits on the bottle, or just tell me the drug "
                        "name and strength instead."
                    )
                )
                return self.pause(ctx, AskDrug)
            state.drug_key = group.drug_key
            state.strength = group.strength
            state.tier = 1
            groups = [group]
        else:
            drug = _drug(state)
            if drug is None:
                return AskDrug()
            groups = deps.store.current_groups(drug.key, state.strength)
            if not groups and state.strength:
                # The drug is covered but not at that strength - name the ones
                # that exist rather than silently widening the search.
                available = deps.store.strengths(drug.key)
                deps.say(
                    Say(
                        f"I don't have {drug.display} at {state.strength} in the current "
                        f"file. Strengths I do have: {', '.join(available)}."
                    )
                )
                state.strength = None
                return self.pause(ctx, AskQuantity if state.quantity is None else AskDrug)
            if not groups:
                deps.say(Say(f"I don't have any current pricing for {drug.display}."))
                return self.pause(ctx, AskDrug)
            state.tier = 2 if state.strength else 3

        state.groups = groups
        if state.quantity is None:
            drug = _drug(state)
            if drug:
                deps.say(AskQuantityReply(drug=drug, strength=state.strength))
            return self.pause(ctx, AskQuantity)
        return ShowPrices(first=True)


@dataclass
class ShowPrices(WaitNode):
    """Render the result on entry; on later turns, act on what came back."""

    first: bool = False

    async def run(self, ctx: Ctx) -> Node | End[None]:
        deps, state = ctx.deps, ctx.state
        drug = _drug(state)
        if drug is None:
            return AskDrug()

        if self.first:
            await self._present(ctx, drug)
            return self.pause(ctx, ShowPrices)

        action = deps.selection.get("action")
        group_id = deps.selection.get("group_id")
        if isinstance(group_id, str) and group_by_id(state, group_id):
            state.selected_group_id = group_id
            return ShowPriceDetail(first=True)
        if action == "back_to_prices":
            return ShowPrices(first=True)
        if action == "price_trend":
            return RunPriceTrend()
        if action == "brand_compare":
            return RunBrandCompare()
        if action == "drug_info":
            return LookupInfo()

        quoted = _parse_quote(deps.text)
        if quoted is not None and state.quantity:
            low, high = nadac.total_range(state.groups, state.quantity)
            deps.say(QuoteComparison(drug=drug, quoted_usd=quoted, low_usd=low, high_usd=high))
            return self.pause(ctx)

        return Start()

    async def _present(self, ctx: Ctx, drug: Drug) -> None:
        deps, state = ctx.deps, ctx.state
        quantity = state.quantity or 30
        groups = state.groups
        tight = nadac.is_tight(groups)
        shortage = await _shortage_for(deps, drug)
        narration = await _narrate(deps, drug, state, groups, tight, shortage)
        deps.say(
            PriceList(
                drug=drug,
                tier=state.tier,
                quantity=quantity,
                groups=groups,
                tight=tight,
                narration=narration,
                shortage=shortage,
            )
        )


_QUOTE_RE = re.compile(r"\$\s*(\d{1,5}(?:\.\d{1,2})?)")


def _parse_quote(text: str) -> float | None:
    match = _QUOTE_RE.search(text or "")
    return float(match.group(1)) if match else None


async def _narrate(
    deps: TurnDeps,
    drug: Drug,
    state: SessionState,
    groups: list[PriceGroup],
    tight: bool,
    shortage: Shortage | None,
) -> str:
    """Ask ``asi1`` to introduce the result, handing it the figures as fixed facts."""
    quantity = state.quantity or 30
    low, high = nadac.total_range(groups, quantity)
    tier_note = {
        1: "The user gave an exact NDC, so this is the precise product on their bottle.",
        2: "The user gave a drug name and strength.",
        3: "The user gave only a drug name, so this spans every strength and is a rough estimate.",
    }[state.tier or 3]
    fee_range = f"${nadac.DISPENSING_FEE_LOW_USD:.0f}-${nadac.DISPENSING_FEE_HIGH_USD:.0f}"
    lines = [
        f"Drug: {drug.display}. Fill quantity: {quantity} units. {tier_note}",
        (
            f"All-in estimate range: ${low:.2f} to ${high:.2f} "
            f"(acquisition cost plus a {fee_range} dispensing fee)."
        ),
        "Formulations found:",
    ]
    for group in groups[:6]:
        lines.append(
            f"- {group.label()}: ${group.per_unit:.5f} per {group.pricing_unit.lower()}, "
            f"${group.fill_cost(quantity):.2f} for {quantity}, priced as of {group.as_of}."
        )
    if tight:
        lines.append(
            "These agree closely, so it is fair to describe this as one price, not a range."
        )
    elif len(groups) > 1:
        lines.append(
            "These are genuinely different products at genuinely different prices. Do not "
            "pick one or average them. Say there is more than one version, and that the NDC "
            "on the bottle they are eventually handed is what pins it down exactly."
        )
    if len({g.as_of for g in groups}) > 1:
        lines.append(
            "The 'as of' dates differ between these, so part of the gap may be one price "
            "being surveyed more recently than another. Mention this."
        )
    if drug.also_priced_as and nadac.other_salt_groups(groups):
        # Confirmed live: metoprolol succinate ER and carvedilol phosphate ER
        # both still start with the curated plain-salt name in NADAC's own
        # description text, so they show up in this same price list at a
        # >100x price gap with nothing else distinguishing them from a real
        # same-salt formulation choice (see nadac.other_salt_groups).
        lines.append(
            f"The extended-release entries in this list are actually {drug.also_priced_as} - "
            f"a different salt than plain {drug.display}, not a pricier version of the same "
            "drug. State this plainly rather than treating the price spread as ordinary "
            "formulation variation."
        )
    if shortage:
        # The shortage record can be for a different dosage form than what is
        # actually priced above - confirmed live for furosemide, whose only
        # current shortage is the injection while this agent prices the oral
        # tablet, two products with no shared supply chain. Telling the model
        # the form explicitly stops it from implying a link the data does not
        # establish.
        lines.append(
            f"This drug is on the FDA shortage list right now, specifically the "
            f"{shortage.dosage_form.lower() or 'unspecified'} form ({shortage.availability}). "
            "Only mention it as possible context for the price if that form matches what "
            "is being priced above; if it is a different form (e.g. injection vs. the oral "
            "tablet/capsule priced here), say the shortage exists but note it is a different "
            "form and likely unrelated to this price."
        )
    try:
        return await ai.narrate_price("\n".join(lines), model=deps.narration_model)
    except Exception:  # noqa: BLE001 - narration is decoration; the card carries the facts
        return (
            f"Here's what {drug.display} looks like for {quantity} units, based on NADAC "
            "acquisition cost plus a typical dispensing fee."
        )


@dataclass
class ShowPriceDetail(WaitNode):
    """One formulation in full - the detail half of the list/detail pattern."""

    first: bool = False

    async def run(self, ctx: Ctx) -> Node | End[None]:
        deps, state = ctx.deps, ctx.state
        drug = _drug(state)
        group = group_by_id(state, state.selected_group_id or "")
        if drug is None or group is None:
            return Start()

        if self.first:
            deps.say(
                PriceDetail(
                    drug=drug,
                    quantity=state.quantity or 30,
                    group=group,
                    shortage=await _shortage_for(deps, drug),
                )
            )
            return self.pause(ctx, ShowPriceDetail)

        action = deps.selection.get("action")
        if action == "back_to_prices":
            return ShowPrices(first=True)
        if action == "price_trend":
            return RunPriceTrend()
        if action == "brand_compare":
            return RunBrandCompare()
        if action == "drug_info":
            return LookupInfo()
        return Start()


@dataclass
class LookupInfo(Node):
    """Path B: FDA label text, verbatim, with the source field named.

    Every section shown for this drug - the regular info card and any later
    "full clinical detail" follow-up - is pinned to one openFDA document via
    ``state.label_set_id`` (see openfda.py module docstring), chosen once and
    reused rather than re-resolved per request. Without that pin, the info
    card and the detail card could each independently land on a different
    manufacturer's product - confirmed live for metformin IR/ER - splicing
    two real labels into one card that implies they are a single document.

    That pin is exact - a document confirmed to be the user's own real
    product - whenever a specific NDC is already in hand (:func:`_known_ndc`);
    otherwise it is still one stable document, but only ever a best guess
    among products that can be genuinely formulation-mismatched (confirmed
    live for metformin IR/ER, pantoprazole oral/IV). ``state.label_exact``
    tracks which case this session is in, and gates a plain disclosure next
    to the dosing/frequency section when a real mismatch is possible
    (``nadac.formulation_ambiguity``) - never a silent guess presented as fact.
    """

    detail: bool = False

    async def run(self, ctx: Ctx) -> Node | End[None]:
        deps, state = ctx.deps, ctx.state
        drug = _drug(state)
        if drug is None:
            return AskDrug()

        try:
            if state.label_set_id is None:
                doc, matched = await openfda.resolve_label_document(
                    deps.http, drug.fda_generic_name, ndc=_known_ndc(state)
                )
                if doc is not None:
                    state.label_set_id = str(doc.get("set_id") or "") or None
                state.label_exact = matched

            # also_priced_as drugs (metoprolol, carvedilol) are excluded: their
            # NADAC-visible "split" is a different salt filed under a wholly
            # different generic_name, so this drug's own openFDA query can
            # structurally never return that other product at all - there is
            # no resolution-side guess here to disclose, just the separate
            # coverage gap named on the card instead (see chat_proto.py).
            disclose = (
                not state.label_exact
                and drug.also_priced_as is None
                and nadac.formulation_ambiguity(deps.store.current_groups(drug.key))
            )

            # _known_ndc(state) is passed again here even once label_set_id is
            # already pinned - not to re-decide the document (set_id already
            # does that, see resolve_label_document), but so _section can show
            # the exact package NDC the user actually has rather than an
            # arbitrary sibling package listed on the same document (confirmed
            # live: one real Mylan metformin ER document legitimately lists
            # two package NDCs for the identical product).
            known_ndc = _known_ndc(state)
            if self.detail:
                sections = await openfda.fetch_detail_sections(
                    deps.http,
                    drug.fda_generic_name,
                    set_id=state.label_set_id,
                    ndc=known_ndc,
                    disclose=disclose,
                )
                if sections:
                    deps.say(DrugDetail(drug=drug, sections=sections))
                else:
                    deps.say(
                        Say(f"The FDA label for {drug.display} has no further detail sections.")
                    )
                return self.pause(ctx, ShowInfo)
            label = await openfda.fetch_label(
                deps.http, drug.fda_generic_name, set_id=state.label_set_id, ndc=known_ndc
            )
        except openfda.OpenFdaError:
            deps.say(Say("The FDA label service didn't answer just now. Try again in a moment."))
            return self.pause(ctx, ShowInfo)

        deps.say(DrugInfo(drug=drug, label=label, shortage=await _shortage_for(deps, drug)))
        return self.pause(ctx, ShowInfo)


@dataclass
class ShowInfo(WaitNode):
    async def run(self, ctx: Ctx) -> Node | End[None]:
        action = ctx.deps.selection.get("action")
        if action == "more_detail":
            return LookupInfo(detail=True)
        if action == "check_price":
            return LookupPrice()
        return Start()


@dataclass
class RunPriceTrend(Node):
    """Price history for the selected formulation - unlocked by the upfront charge.

    No separate approval step: the one charge already made at the paywall
    covers this, so it reads straight from the NADAC cache like any other
    lookup.
    """

    async def run(self, ctx: Ctx) -> Node | End[None]:
        deps, state = ctx.deps, ctx.state
        drug = _drug(state)
        if drug is None:
            return AskDrug()

        if not state.selected_group_id:
            if len(state.groups) == 1:
                state.selected_group_id = state.groups[0].group_id
            else:
                deps.say(
                    Say(
                        "Pick which version you want the price history for first. Tap one "
                        "of the options above."
                    )
                )
                return self.pause(ctx, ShowPrices)

        group = group_by_id(state, state.selected_group_id or "")
        if group is None:
            return ShowPrices(first=True)

        points = deps.store.history(group.example_ndc)
        if not points:
            deps.say(
                Say(f"Not enough price history on file yet for {drug.display} at that formulation.")
            )
            return self.pause(ctx, ShowPrices)
        deps.say(TrendResult(drug=drug, group=group, points=points))
        return self.pause(ctx, ShowPrices)


@dataclass
class RunBrandCompare(Node):
    """Brand-vs-generic comparison - unlocked by the upfront charge."""

    async def run(self, ctx: Ctx) -> Node | End[None]:
        deps, state = ctx.deps, ctx.state
        drug = _drug(state)
        if drug is None:
            return AskDrug()

        rows = deps.store.brand_vs_generic(drug.key)
        if not rows:
            deps.say(
                Say(f"No priced brand equivalent for {drug.display} in the current NADAC release.")
            )
            return self.pause(ctx, ShowPrices)
        deps.say(CompareResult(drug=drug, rows=rows))
        return self.pause(ctx, ShowPrices)


NODES: dict[str, type[Node]] = {
    cls.__name__: cls
    for cls in (
        Paywall,
        AwaitingAccessPayment,
        Start,
        AskDrug,
        AskQuantity,
        LookupPrice,
        ShowPrices,
        ShowPriceDetail,
        LookupInfo,
        ShowInfo,
        RunPriceTrend,
        RunBrandCompare,
    )
}


async def advance(state: SessionState, deps: TurnDeps, *, max_steps: int = 8) -> None:
    """Drive the machine for exactly one user turn.

    Resumes at the node the last turn paused on and follows automatic
    transitions until a node pauses again. ``max_steps`` is a loop guard, not a
    feature; hitting it means a node is cycling and the session is reset to a
    known state rather than left mid-flight.
    """
    node: Node = NODES.get(state.node, Paywall)()
    run_ctx: Ctx = GraphRunContext(state=state, deps=deps)
    for _ in range(max_steps):
        nxt = await node.run(run_ctx)
        if not isinstance(nxt, Node):
            return
        node = nxt
    state.node = Paywall.__name__ if not state.stripe_paid else Start.__name__
