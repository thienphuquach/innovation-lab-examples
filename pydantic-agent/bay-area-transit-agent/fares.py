"""Fare computation over an itinerary's leg sequence (GTFS-Fares v2).

Given the transit legs of a chosen itinerary, compute what the trip costs to tap
through, using the real 511 regional Fares v2 data (:mod:`clients.five11`).

Two payment methods are offered - a Clipper card and a contactless bank card or
mobile wallet - and they always cost the same, because since Clipper 2.0 they are
the same fare product from the rider's side (see ``clients.five11.TAP_MEDIA``).
Cash is not modelled at all: every Clipper agency takes a tap, and the handful of
operators that take neither (Capitol Corridor, ACE, a few small ferries) are
reported as unpriceable rather than quietly priced in cash the rider can't
actually use at a gate.

The leg sequence matters: consecutive legs get transfer discounts via
``fare_transfer_rules``. Distance/zone-based fares (BART's ~2,450 station-pair
matrix) can't be joined to the routing feed's stop IDs across feeds, so those legs
are priced from a representative fare and the total is flagged **Estimated**
rather than presenting false precision. See ``research-notes.md`` §6-7.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from typing import Any

from clients.five11 import TAP_MEDIA, FareData

# A network whose single-ride product set is larger than this is treated as
# distance/zone-based (e.g. BART's station-pair matrix) -> estimated.
_DISTANCE_BASED_THRESHOLD = 30


@dataclass
class FareOption:
    id: str  # "clipper" | "contactless"
    label: str
    amount: float
    currency: str
    estimated: bool
    leg_amounts: list[float | None] | None = None  # per-transit-leg cost, same order as the itinerary's transit legs
    unpriced_legs: int = 0  # legs with no fare data at all, assumed free/included in `amount`

    def to_choice(self) -> dict[str, Any]:
        secondary = f"${self.amount:.2f}" + (" (est.)" if self.estimated else "")
        if self.unpriced_legs:
            secondary += f" + {self.unpriced_legs} unpriced leg{'s' if self.unpriced_legs != 1 else ''}"
        return {"value": self.id, "label": self.label, "secondary_text": secondary}


def _amount_for(fd: FareData, product_id: str, rider: str, media_group: tuple[str, ...]) -> float | None:
    rows = fd.products.get(product_id, [])
    for want_rider in (rider, "adult", None):
        vals = [
            p.amount
            for p in rows
            if (want_rider is None or p.rider == want_rider) and p.media in media_group
        ]
        if vals:
            return min(vals)
    return None


def _single_product_ids(fd: FareData, network_id: str) -> list[str]:
    """Single-ride (non-pass, non-transfer) product ids referenced by a network."""
    ids: set[str] = set()
    for rule in fd.leg_rules.get(network_id, []):
        pid = rule.fare_product_id
        if rule.transfer_only or pid.startswith("transfer:") or pid.startswith("free"):
            continue
        if fd.products.get(pid) and not fd.products[pid][0].is_pass:
            ids.add(pid)
    return list(ids)


# `_leg_fare` statuses:
#  "priced"      - a real amount was found for this network + payment method
#  "free"        - the network has no single-ride fare product data at all (e.g. a
#                  free airport shuttle) - safe to treat the leg as $0 and keep going
#  "unsupported" - the network *has* priced products, but none of them accept this
#                  payment method (e.g. BART publishes no cash/ticket-media product
#                  at all - cash is genuinely not accepted, not just uncomputed)
def _leg_fare(
    fd: FareData, network_id: str, rider: str, media_group: tuple[str, ...]
) -> tuple[str, float | None, bool]:
    """Return (status, fare, estimated) for one leg on a network under one payment method."""
    pids = _single_product_ids(fd, network_id)
    if not pids:
        return "free", None, True
    amounts = [a for pid in pids if (a := _amount_for(fd, pid, rider, media_group)) is not None]
    if not amounts:
        return "unsupported", None, True
    # Distance/zone-based networks (BART, Caltrain) publish a per-station-pair or
    # per-zone-pair product matrix we can't join to the routing feed's stop IDs,
    # so price from a representative fare and flag the whole option Estimated.
    distance_based = any(":matrix:" in pid for pid in pids) or len(pids) > _DISTANCE_BASED_THRESHOLD
    if distance_based:
        return "priced", round(statistics.median(amounts), 2), True
    return "priced", min(amounts), False


def _transfer_amount(
    fd: FareData, from_lg: str, to_lg: str, rider: str, media_group: tuple[str, ...]
) -> float | None:
    amounts = []
    for tr in fd.transfer_rules:
        if tr.from_leg_group == from_lg and tr.to_leg_group == to_lg:
            a = _amount_for(fd, tr.fare_product_id, rider, media_group)
            if a is not None:
                amounts.append(a)
    return min(amounts) if amounts else None


def compute_fare_options(
    legs: list[dict[str, Any]], fd: FareData, *, rider: str = "adult"
) -> tuple[list[FareOption], list[str]]:
    """Price an itinerary's legs and return the ways to pay for it.

    Returns ``([], [])`` for a walking-only itinerary (no fare to pay), and two
    equally-priced options - Clipper and a contactless bank card - for anything
    that can be tapped through end to end.

    The second return value explains why the trip *can't* be tapped when that
    happens - e.g. "Capitol Corridor doesn't accept Clipper or a contactless bank
    card" - so an empty option list reads as a deliberate, verified answer rather
    than a silent computation failure (diagnosis.md issue 6).

    A single leg with no fare data at all (``_leg_fare`` status ``"free"`` - e.g. a
    free airport shuttle with no published fare product) doesn't discard the
    result; it's treated as a $0/included leg and the rest of the itinerary still
    prices normally. A leg on a network that publishes fares but accepts no tap
    media (status ``"unsupported"``) does drop the whole thing, since the rider
    genuinely cannot tap their way through this trip.
    """
    transit = [leg for leg in legs if leg.get("transitLeg")]
    if not transit:
        return [], []

    nets = [fd.route_network.get(leg.get("routeId", "")) for leg in transit]
    leg_groups = [fd.net_leg_group(n) if n else None for n in nets]

    notes: list[str] = []
    total = 0.0
    estimated = False
    unpriced_legs = 0
    leg_amounts: list[float | None] = []
    for i, net in enumerate(nets):
        agency = transit[i].get("agencyName") or net or "this route"
        if not net:
            notes.append(f"No fare data available for {agency}")
            return [], notes
        status, fare, est = _leg_fare(fd, net, rider, TAP_MEDIA)
        if status == "unsupported":
            notes.append(
                f"{agency} doesn't accept Clipper or a contactless bank card - "
                "buy a ticket from the operator for that leg"
            )
            return [], notes
        if status == "free":
            # No fare product at all for this leg (e.g. a free airport shuttle) -
            # assume no charge rather than discarding the whole trip over one leg
            # we simply have no data for.
            leg_amounts.append(None)
            unpriced_legs += 1
            estimated = True
            continue
        assert fare is not None
        estimated = estimated or est
        prev_lg = leg_groups[i - 1] if i > 0 else None
        cur_lg = leg_groups[i]
        if prev_lg and cur_lg:
            discount = _transfer_amount(fd, prev_lg, cur_lg, rider, TAP_MEDIA)
            # A transfer rule replaces the leg's own fare (free/discounted/credit).
            # Floor at 0 - some rows encode a credit as a negative amount - and
            # flag the total estimated, since transfer semantics are approximate.
            if discount is not None and discount < fare:
                fare = max(discount, 0.0)
                estimated = True
        leg_amounts.append(fare)
        total += fare

    # One computed fare, two ways to pay it. Clipper leads because it's the only
    # one that also carries discounted (senior/youth/disabled) fares; a bank card
    # always charges the full adult fare.
    options = [
        FareOption(
            opt_id,
            label,
            round(total, 2),
            "USD",
            estimated,
            leg_amounts=leg_amounts,
            unpriced_legs=unpriced_legs,
        )
        for opt_id, label in (("clipper", "Clipper card"), ("contactless", "Contactless bank card"))
    ]
    return options, notes
