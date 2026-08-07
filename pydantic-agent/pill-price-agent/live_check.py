"""End-to-end check against the real APIs. No mocks, no fixtures.

Exercises every path the agent can take: all three price tiers, a drug-info
lookup, a shortage cross-check, and the Stripe-gated paid feature. Steps whose
credentials are missing are reported as skipped rather than faked, so the output
always says exactly which claims were verified live.

    python live_check.py
"""

from __future__ import annotations

import asyncio
import os

import httpx
from dotenv import load_dotenv

load_dotenv()

import conversation as conv
import nadac
import pydantic_agent as ai
from chat_proto import price_detail_card, price_list_card, store
from session_state import SessionState

RULE = "=" * 78


def banner(title: str) -> None:
    print(f"\n{RULE}\n{title}\n{RULE}")


def have(name: str, prefix: str = "") -> bool:
    """True when the variable holds a real credential, not an .env.example stub."""
    value = (os.getenv(name) or "").strip()
    return bool(value) and "your_" not in value and value.startswith(prefix)


def show(replies: list[conv.Reply]) -> None:
    for reply in replies:
        if isinstance(reply, conv.PriceList):
            print(f"narration: {reply.narration}\n")
            print(f"tier {reply.tier} | {reply.quantity} units | tight={reply.tight}")
            for group in reply.groups:
                total_low = group.fill_cost(reply.quantity) + nadac.DISPENSING_FEE_LOW_USD
                total_high = group.fill_cost(reply.quantity) + nadac.DISPENSING_FEE_HIGH_USD
                print(
                    f"  {group.label():<34} ${group.per_unit:.5f}/unit  "
                    f"${total_low:6.2f}-${total_high:6.2f} all-in  "
                    f"as of {group.as_of}  ({group.ndc_count} NDCs)"
                )
            if reply.shortage:
                print(f"  SHORTAGE: {reply.shortage.generic_name} - {reply.shortage.availability}")
            card = price_list_card(reply)
            print(f"  card_kind={card['card_kind']} payload={len(card['card_payload'])} bytes")
        elif isinstance(reply, conv.PriceDetail):
            card = price_detail_card(reply)
            print(f"  detail card for {reply.group.label()} ({len(card['card_payload'])} bytes)")
        elif isinstance(reply, conv.DrugInfo):
            if reply.label:
                print(f"source field: {reply.label.field}  ({reply.label.source})")
                print(f"  pinned to set_id={reply.label.set_id}  ndc={reply.label.product_ndc}")
                print(f"  {reply.label.text[:280].strip()}...")
            else:
                print("  no patient-facing label section on file")
            if reply.shortage:
                print(f"  SHORTAGE: {reply.shortage.generic_name} - {reply.shortage.availability}")
        elif isinstance(reply, conv.DrugDetail):
            set_ids = {s.set_id for s in reply.sections}
            print(f"  {len(reply.sections)} sections, distinct set_ids={set_ids}")
            print(f"  ONE DOCUMENT ONLY: {len(set_ids) <= 1}")
            for section in reply.sections:
                print(f"  --- {section.field} ({section.source}) | ndc={section.product_ndc}")
                print(f"      {section.text[:160].strip()}...")
                if section.formulation_note:
                    print(f"      DISCLOSURE: {section.formulation_note}")
        elif isinstance(reply, conv.AskUpfrontPayment):
            print(f"  payment requested: {reply.description} (${reply.amount_cents / 100:.2f})")
        elif isinstance(reply, conv.TrendResult):
            print(f"  {len(reply.points)} price points for {reply.group.label()}:")
            for point in reply.points:
                print(f"   {point.effective_on}  ${point.per_unit:.5f}")
        elif isinstance(reply, conv.CompareResult):
            print(f"  {len(reply.rows)} brand products compared for {reply.drug.display}")
            for row in reply.rows:
                generic = row.generic_per_unit or 0.0
                multiple = (row.per_unit / generic) if generic else 0.0
                print(
                    f"    {row.description:<28} brand ${row.per_unit:.5f} vs "
                    f"generic ${generic:.5f}  ({multiple:.1f}x)  as of {row.as_of}"
                )
        elif isinstance(reply, conv.Say):
            print(f"  {reply.text}")
        elif isinstance(reply, conv.ChooseDrug):
            print(f"  {reply.reason}")
            print(f"  offered {len(reply.options)} curated drugs")


async def turn(
    state: SessionState, text: str, client: httpx.AsyncClient, **kw: object
) -> conv.TurnDeps:
    deps = conv.TurnDeps(
        text=text,
        selection=kw.get("selection") or {},  # type: ignore[arg-type]
        store=store(),
        http=client,
        intake=kw.get("intake"),  # type: ignore[arg-type]
    )
    if deps.intake is None and have("ASI_ONE_API_KEY"):
        try:
            deps.intake = await ai.classify(text)
        except Exception as exc:  # noqa: BLE001 - report, don't abort the whole run
            print(f"  (asi1-mini intake unavailable: {type(exc).__name__})")
    await conv.advance(state, deps)
    return deps


async def main() -> None:
    cache = store()
    banner("NADAC cache")
    print(f"release modified : {cache.loaded_release()}")
    print(f"cached rows      : {cache.row_count():,}")
    release = await asyncio.to_thread(nadac.resolve_distribution)
    print(f"resolved URL     : {release.download_url}")
    print(f"distribution id  : {release.distribution_id}")

    asi = have("ASI_ONE_API_KEY")
    stripe_ready = have("STRIPE_SECRET_KEY", "sk_test_")
    print(f"\nASI:One key      : {'present' if asi else 'MISSING - narration/intake skipped'}")
    print(f"Stripe test key  : {'present' if stripe_ready else 'MISSING - checkout skipped'}")

    async with httpx.AsyncClient(timeout=30.0) as client:
        banner("PAYWALL - fires on the very first message, before any drug/intent processing")
        state = SessionState()
        deps = await turn(state, "hi there", client)
        show(deps.outbox)
        print(f"  next node: {state.node}  (must be AwaitingAccessPayment - nothing was absorbed)")

        banner("TIER 1 - exact NDC off a bottle (session already unlocked from here on)")
        state = SessionState(quantity=30, stripe_paid=True)
        deps = await turn(
            state,
            "my bottle says NDC 27241024190, 30 tablets",
            client,
            intake=ai.Intake(intent="price", ndc_text="27241024190", quantity=30),
        )
        show(deps.outbox)

        banner("TIER 2 - name + strength, the formulation-divergence case")
        state = SessionState(quantity=30, stripe_paid=True)
        deps = await turn(
            state,
            "how much is metformin 1000 mg for 30 tablets",
            client,
            intake=ai.Intake(
                intent="price", drug_text="metformin", strength_text="1000 mg", quantity=30
            ),
        )
        show(deps.outbox)

        banner("TIER 2 - the tap-through to detail (list -> detail card pattern)")
        group_id = state.groups[-1].group_id
        deps = await turn(
            state, "", client, selection={"action": "pick_group", "group_id": group_id}
        )
        show(deps.outbox)

        banner("TIER 3 - name only")
        state = SessionState(quantity=30, stripe_paid=True)
        deps = await turn(
            state,
            "what does levothyroxine cost",
            client,
            intake=ai.Intake(intent="price", drug_text="levothyroxine", quantity=30),
        )
        show(deps.outbox)

        banner("QUANTITY GATE - no fill size, no total")
        state = SessionState(stripe_paid=True)
        deps = await turn(
            state,
            "price of lisinopril 10mg",
            client,
            intake=ai.Intake(intent="price", drug_text="lisinopril", strength_text="10mg"),
        )
        print(f"  replies: {[type(r).__name__ for r in deps.outbox]}  next node: {state.node}")

        banner("SHORTAGE CROSS-CHECK - furosemide is on the current FDA list")
        state = SessionState(quantity=30, stripe_paid=True)
        deps = await turn(
            state,
            "furosemide 40 mg, 30 tablets",
            client,
            intake=ai.Intake(
                intent="price", drug_text="furosemide", strength_text="40 mg", quantity=30
            ),
        )
        show(deps.outbox)

        banner("PATH B - drug info, no Medication Guide available (falls back)")
        state = SessionState(drug_key="lisinopril", stripe_paid=True)
        deps = await turn(
            state,
            "what is lisinopril",
            client,
            intake=ai.Intake(intent="info", drug_text="lisinopril"),
        )
        show(deps.outbox)

        banner("PATH B - drug info where a Medication Guide does exist")
        state = SessionState(drug_key="gabapentin", stripe_paid=True)
        deps = await turn(
            state,
            "tell me about gabapentin",
            client,
            intake=ai.Intake(intent="info", drug_text="gabapentin"),
        )
        show(deps.outbox)

        banner("PATH B - full clinical detail, pinned to one document")
        state = SessionState(drug_key="metformin", stripe_paid=True)
        deps = await turn(
            state,
            "what is metformin",
            client,
            intake=ai.Intake(intent="info", drug_text="metformin"),
        )
        info_set_id = state.label_set_id
        print(f"  info card pinned set_id: {info_set_id}")
        deps = await turn(
            state,
            "show me the full clinical detail",
            client,
            intake=ai.Intake(intent="more_detail", drug_text="metformin"),
        )
        show(deps.outbox)
        print(
            f"  detail reused the same document as the info card: {state.label_set_id == info_set_id}"
        )

        banner("PATH B - NDC already in hand pins the exact formulation, no guessing")
        state = SessionState(quantity=30, stripe_paid=True)
        deps = await turn(
            state,
            "my bottle says 27241024190, 30 tablets",
            client,
            intake=ai.Intake(intent="price", ndc_text="27241024190", quantity=30),
        )
        deps = await turn(
            state,
            "show me the full clinical detail",
            client,
            intake=ai.Intake(intent="more_detail", drug_text="metformin"),
        )
        show(deps.outbox)
        print(f"  resolved from a real NDC, not a guess: {state.label_exact}")

        banner("PATH B - no NDC yet, and metformin genuinely has two formulations")
        state = SessionState(drug_key="metformin", stripe_paid=True)
        deps = await turn(
            state,
            "show me the full clinical detail for metformin",
            client,
            intake=ai.Intake(intent="more_detail", drug_text="metformin"),
        )
        show(deps.outbox)
        print(f"  resolved from a real NDC, not a guess: {state.label_exact}")

        banner("TIER 3 - carvedilol's price list must disclose the other-salt mix")
        state = SessionState(quantity=30, stripe_paid=True)
        deps = await turn(
            state,
            "what does carvedilol cost",
            client,
            intake=ai.Intake(intent="price", drug_text="carvedilol", quantity=30),
        )
        show(deps.outbox)
        card = price_list_card(next(r for r in deps.outbox if isinstance(r, conv.PriceList)))
        print(f"  disclosure present: {'phosphate' in card['card_payload']}")

        banner("BRAND VS GENERIC - a real brand row must actually be loaded, not silently empty")
        state = SessionState(drug_key="atorvastatin", stripe_paid=True)
        deps = await turn(
            state,
            "brand versus generic for atorvastatin",
            client,
            intake=ai.Intake(intent="brand_compare", drug_text="atorvastatin"),
        )
        show(deps.outbox)

        banner("BRAND VS GENERIC - metoprolol must show Lopressor, never Toprol-XL (wrong salt)")
        state = SessionState(drug_key="metoprolol", stripe_paid=True)
        deps = await turn(
            state,
            "brand versus generic for metoprolol",
            client,
            intake=ai.Intake(intent="brand_compare", drug_text="metoprolol"),
        )
        show(deps.outbox)

        banner("HARD BOUNDARY - dosing question, checked before any model call")
        for question in ("should i take metformin", "is this dose right for me"):
            print(f"  {question!r} -> blocked={ai.crosses_medical_boundary(question)}")
        print(f"\n  {ai.BOUNDARY_REPLY.splitlines()[0]}")

        banner("EDGE - a drug that is not on the curated list")
        state = SessionState(stripe_paid=True)
        deps = await turn(
            state,
            "how much is warfarin",
            client,
            intake=ai.Intake(intent="price", drug_text="warfarin"),
        )
        show(deps.outbox)

    banner("PAID TIER - one upfront Stripe charge, then everything is free")
    await paid_tier(cache, stripe_ready=stripe_ready)


async def paid_tier(cache: nadac.NadacStore, *, stripe_ready: bool) -> None:
    """Create a real Stripe checkout, then prove a paid session runs premium features free."""
    if not stripe_ready:
        print("Stripe checkout SKIPPED - set STRIPE_SECRET_KEY to an sk_test_ key in .env.")
    else:
        import payment

        payment.assert_stripe_test_keys()
        try:
            checkout = await asyncio.to_thread(
                payment.create_checkout_session,
                "agent1qtest",
                "live-check-session",
                int(os.getenv("STRIPE_AMOUNT_CENTS", "200")),
                conv.UPFRONT_DESCRIPTION,
            )
        except Exception as exc:  # noqa: BLE001 - report the reason, don't dump a traceback
            print(f"Stripe checkout FAILED  : {type(exc).__name__}: {exc}")
        else:
            print(f"Stripe checkout created : {checkout['checkout_session_id']}")
            print(f"  ui_mode               : {checkout['ui_mode']}")
            print(f"  amount                : ${int(checkout['amount_cents']) / 100:.2f}")
            print(f"  client_secret present : {bool(checkout['client_secret'])}")
            print(
                f"  verify_paid (unpaid)  : {payment.verify_paid(checkout['checkout_session_id'])}"
            )

    print()
    group = next(g for g in cache.current_groups("metformin", "1,000 MG") if g.form == "GASTR-TB")
    unlocked = SessionState(
        node="ShowPrices",
        stripe_paid=True,
        drug_key="metformin",
        groups=[group],
        selected_group_id=group.group_id,
    )
    async with httpx.AsyncClient(timeout=30.0) as client:
        deps = await turn(unlocked, "", client, selection={"action": "price_trend"})
    print("price trend, session already marked stripe_paid (no second charge, no approval step):")
    show(deps.outbox)


if __name__ == "__main__":
    asyncio.run(main())
