"""Vet the curated list against live data. Run this before adding any drug.

Two checks per drug, both of which have caught real problems:

1. NDC clustering. Group the drug's current products by strength and dosage
   form and report the price spread. Metformin is the famous case - ER 1,000 MG
   splits into a gastric-retentive tablet and an osmotic tablet at very
   different prices - but it is not special. Every drug on the list diverges
   somewhere, which is why the agent treats a range as the normal answer.
2. openFDA resolution. Confirm ``openfda.generic_name.exact`` matches the name
   in ``drugs.py``, and report which patient-facing field is actually present.
   Medication Guides are only required for drugs with particular serious risks,
   so most ordinary generics fall through to Patient Counseling Information.

    python verify_drugs.py            # both checks
    python verify_drugs.py --nadac    # skip the network calls
"""

from __future__ import annotations

import argparse
import asyncio

import httpx
from dotenv import load_dotenv

load_dotenv()

import nadac
import openfda
from chat_proto import store
from drugs import CURATED


def check_clustering() -> int:
    """Print the per-drug formulation spread. Returns the number of problems."""
    cache = store()
    if cache.row_count() == 0:
        print(
            "Cache is empty - run a refresh first (python -c 'from chat_proto import store; store().refresh()')."
        )
        return 1

    print(f"NADAC release {cache.loaded_release()} | {cache.row_count():,} cached rows\n")
    header = f"{'drug':<22}{'products':>9}{'strengths':>11}{'split':>8}{'worst':>9}  worst case"
    print(header)
    print("-" * 108)

    problems = 0
    for drug in CURATED:
        groups = cache.current_groups(drug.key)
        if not groups:
            print(f"{drug.display:<22}{'MISSING from NADAC - remove from the curated list':>60}")
            problems += 1
            continue

        by_strength: dict[str, list[nadac.PriceGroup]] = {}
        for group in groups:
            by_strength.setdefault(group.strength, []).append(group)

        split = sum(1 for gs in by_strength.values() if not nadac.is_tight(gs))
        worst_key = max(by_strength, key=lambda s: nadac.spread(by_strength[s]))
        worst = nadac.spread(by_strength[worst_key])
        detail = ""
        if worst > 0:
            ranked = sorted(by_strength[worst_key], key=lambda g: g.per_unit)
            low, high = ranked[0], ranked[-1]
            detail = (
                f"{worst_key}: ${low.per_unit:.5f} ({low.form or 'n/a'}) "
                f"vs ${high.per_unit:.5f} ({high.form or 'n/a'})"
            )
        print(
            f"{drug.display:<22}{len(groups):>9}{len(by_strength):>11}"
            f"{split:>8}{worst * 100:>8.0f}%  {detail[:52]}"
        )
    return problems


async def check_openfda() -> int:
    """Confirm each drug resolves on openFDA and report the field in use."""
    print(f"\n{'drug':<22}{'generic_name.exact':<28}{'patient field in use':<40}shortage")
    print("-" * 108)
    problems = 0
    async with httpx.AsyncClient(timeout=30.0) as client:
        for drug in CURATED:
            try:
                label = await openfda.fetch_label(client, drug.fda_generic_name)
                shortage = await openfda.fetch_shortage(client, drug.fda_generic_name)
            except openfda.OpenFdaError as exc:
                print(f"{drug.display:<22}{drug.fda_generic_name:<28}ERROR {exc}")
                problems += 1
                continue
            field = label.field if label else "NONE - no patient text at all"
            if label is None:
                problems += 1
            flag = "CURRENT" if shortage else "-"
            print(f"{drug.display:<22}{drug.fda_generic_name:<28}{field:<40}{flag}")
    return problems


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--nadac", action="store_true", help="clustering check only")
    args = parser.parse_args()

    problems = check_clustering()
    if not args.nadac:
        problems += asyncio.run(check_openfda())

    print(f"\n{problems} problem(s) found." if problems else "\nAll curated drugs verified.")


if __name__ == "__main__":
    main()
