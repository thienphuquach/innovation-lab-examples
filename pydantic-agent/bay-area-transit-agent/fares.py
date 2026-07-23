"""Fare computation over an itinerary's leg sequence (GTFS-Fares v2).

Given the transit legs of a chosen itinerary, compute the total cost by payment
method - Clipper (electronic), Cash, and a single-operator Day pass when one
applies - using the real 511 regional Fares v2 data (:mod:`clients.five11`).

The leg sequence matters: consecutive legs get Clipper transfer discounts via
``fare_transfer_rules`` (which is why Clipper usually beats cash on multi-leg
trips). Distance/zone-based fares (BART's ~2,450 station-pair matrix) can't be
joined to the routing feed's stop IDs across feeds, so those legs are priced from
a representative fare and the whole option is flagged **Estimated** rather than
presenting false precision. See ``research-notes.md`` §6-7.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from typing import Any

from clients.five11 import (
    CASH_MEDIA,
    CLIPPER_MEDIA,
    FareData,
)

# A network whose single-ride product set is larger than this is treated as
# distance/zone-based (e.g. BART's station-pair matrix) -> estimated.
_DISTANCE_BASED_THRESHOLD = 30


@dataclass
class FareOption:
    id: str  # "clipper" | "cash" | "daypass"
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


def _day_pass(fd: FareData, network_id: str, rider: str) -> tuple[float, str] | None:
    pids = {
        r.fare_product_id
        for r in fd.leg_rules.get(network_id, [])
        if any(k in r.fare_product_id.lower() for k in ("daypass", "1-day", ":day"))
    }
    best: tuple[float, str] | None = None
    for pid in pids:
        for mg in (CLIPPER_MEDIA, CASH_MEDIA):
            a = _amount_for(fd, pid, rider, mg)
            if a is not None and (best is None or a < best[0]):
                best = (a, pid)
    return (round(best[0], 2), best[1]) if best else None


def compute_fare_options(
    legs: list[dict[str, Any]], fd: FareData, *, rider: str = "adult"
) -> tuple[list[FareOption], list[str]]:
    """Compute payment options for an itinerary's legs, cheapest first.

    Returns ``([], [])`` for a walking-only itinerary (no fare to pay).

    The second return value explains why any payment method that was *considered*
    isn't offered - e.g. "Cash isn't accepted on Bay Area Rapid Transit" - so a
    missing option reads as a deliberate, verified answer rather than a silent
    computation failure (diagnosis.md issue 6).

    A single leg with no fare data at all (``_leg_fare`` status ``"free"`` - e.g. a
    free airport shuttle with no published fare product) no longer discards the
    whole option; it's treated as a $0/included leg and the rest of the itinerary
    still prices normally. A leg where the network *does* publish fares but none of
    them accept the payment method being priced (status ``"unsupported"`` - e.g.
    BART has no cash/ticket-media product at all) still correctly drops that whole
    option, since that payment method genuinely cannot be used for this trip - but
    now says so via ``notes`` instead of just vanishing.
    """
    transit = [leg for leg in legs if leg.get("transitLeg")]
    if not transit:
        return [], []

    nets = [fd.route_network.get(leg.get("routeId", "")) for leg in transit]
    leg_groups = [fd.net_leg_group(n) if n else None for n in nets]

    options: list[FareOption] = []
    notes: list[str] = []
    for opt_id, media_group in (("clipper", CLIPPER_MEDIA), ("cash", CASH_MEDIA)):
        label = "Clipper" if opt_id == "clipper" else "Cash"
        total = 0.0
        estimated = False
        priced = True
        unpriced_legs = 0
        leg_amounts: list[float | None] = []
        for i, net in enumerate(nets):
            agency = transit[i].get("agencyName") or net or "this route"
            if not net:
                notes.append(f"{label}: no fare data available for {agency}")
                priced = False
                break
            status, fare, est = _leg_fare(fd, net, rider, media_group)
            if status == "unsupported":
                notes.append(f"{label} isn't accepted on {agency}")
                priced = False
                break
            if status == "free":
                # No fare product at all for this leg (e.g. a free airport shuttle) -
                # assume no charge rather than discarding the whole option over one
                # leg we simply have no data for.
                leg_amounts.append(None)
                unpriced_legs += 1
                estimated = True
                continue
            assert fare is not None
            estimated = estimated or est
            prev_lg = leg_groups[i - 1] if i > 0 else None
            cur_lg = leg_groups[i]
            if prev_lg and cur_lg:
                discount = _transfer_amount(fd, prev_lg, cur_lg, rider, media_group)
                # A transfer rule replaces the leg's own fare (free/discounted/credit).
                # Floor at 0 - some rows encode a credit as a negative amount - and
                # flag the option estimated, since transfer semantics are approximate.
                if discount is not None and discount < fare:
                    fare = max(discount, 0.0)
                    estimated = True
            leg_amounts.append(fare)
            total += fare
        if priced:
            options.append(
                FareOption(
                    opt_id,
                    label,
                    round(total, 2),
                    "USD",
                    estimated,
                    leg_amounts=leg_amounts,
                    unpriced_legs=unpriced_legs,
                )
            )

    uniq_nets = {n for n in nets if n}
    if len(uniq_nets) == 1:
        dp = _day_pass(fd, next(iter(uniq_nets)), rider)
        if dp:
            options.append(FareOption("daypass", "Day pass", dp[0], "USD", False))
    elif len(uniq_nets) > 1:
        notes.append("Day pass: not offered - this trip uses more than one operator")

    options.sort(key=lambda o: o.amount)
    return options, notes
