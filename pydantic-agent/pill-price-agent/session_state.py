"""Session persistence: one record per sender in ``ctx.storage``.

This is the single state store for the whole agent. The conversation graph in
``conversation.py`` uses ``pydantic_graph`` for typed nodes and transitions but
deliberately not for state: ``pydantic_graph`` 2.x removed its persistence
package outright, and two competing stores would be a bug waiting to happen
even if it hadn't. The graph's position is just the ``node`` string below.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, TypeAdapter, ValidationError
from uagents import Context

from nadac import PriceGroup

# Graph node names, persisted so the next turn resumes at the right node.
# A brand-new session always starts at the paywall, not at Start - the whole
# agent is gated behind one upfront charge, not just the two premium analyses.
PAYWALL = "Paywall"
AWAITING_ACCESS_PAYMENT = "AwaitingAccessPayment"


class SessionState(BaseModel):
    """Everything that survives between turns for one sender."""

    node: str = PAYWALL

    drug_key: str | None = None
    strength: str | None = None
    ndc: str | None = None
    # Deliberately NOT cleared when the drug changes (unlike strength/groups/
    # selected_group_id/label_set_id below, each reset in conversation.py with
    # its own stated reason) - a fill size is a fact about the patient's
    # prescription-filling habits, not about which drug it names, and this
    # agent never gives dosing advice that a fill size could contradict. A
    # user pricing three drugs in one session most likely wants the same
    # count priced for each unless they say otherwise.
    quantity: int | None = None
    tier: int = 0

    groups: list[PriceGroup] = Field(default_factory=list)
    selected_group_id: str | None = None

    # The one openFDA document (by set_id) that this drug's info/detail cards
    # are pinned to for the rest of the session - see openfda.py module
    # docstring. Cleared whenever the drug changes, alongside strength/groups.
    label_set_id: str | None = None

    # True only when label_set_id was resolved from a real NDC already in hand
    # (an exact Tier 1 lookup, or a formulation the user tapped from a price
    # list) - so it is the user's own actual product, not a priority-walk
    # guess among products that can be formulation-mismatched (confirmed live
    # for metformin IR/ER and pantoprazole oral/IV). Gates the dosing
    # disclosure in openfda.py: no guess, no disclosure needed.
    label_exact: bool = False

    # True once the one-time upfront charge has cleared for this chat window.
    # Resets to False along with the rest of the state on a new window (see
    # ``check_new_window_and_reset``), so the charge is per-session, not
    # per-address-forever.
    stripe_session_id: str | None = None
    stripe_paid: bool = False


_ADAPTER: TypeAdapter[SessionState] = TypeAdapter(SessionState)
_SESSION_KEY = "session:{}"
_WINDOW_KEY = "chat:window:{}"


def get_state(ctx: Context, sender: str) -> SessionState:
    raw = ctx.storage.get(_SESSION_KEY.format(sender))
    if not raw:
        return SessionState()
    try:
        return _ADAPTER.validate_json(raw)
    except (TypeError, ValidationError):
        return SessionState()


def save_state(ctx: Context, sender: str, state: SessionState) -> None:
    ctx.storage.set(_SESSION_KEY.format(sender), _ADAPTER.dump_json(state).decode("utf-8"))


def check_new_window_and_reset(ctx: Context, sender: str) -> None:
    """Start fresh when a message arrives on a new chat window.

    ``ctx.storage`` is keyed by sender, and ASI:One reuses one sender address
    across a user's separate conversations - without this, opening a new chat
    would silently resume a half-finished price check from an unrelated one.
    ``ctx.session`` changes per window, so comparing it catches that.
    """
    current = str(ctx.session)
    stored = ctx.storage.get(_WINDOW_KEY.format(sender))
    if stored and stored != current:
        save_state(ctx, sender, SessionState())
    ctx.storage.set(_WINDOW_KEY.format(sender), current)


def group_by_id(state: SessionState, group_id: str) -> PriceGroup | None:
    return next((g for g in state.groups if g.group_id == group_id), None)


def as_dict(state: SessionState) -> dict[str, Any]:
    return state.model_dump(mode="json")
