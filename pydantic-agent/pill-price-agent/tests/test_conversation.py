"""The conversation machine, driven end to end without a network."""

from __future__ import annotations

from typing import Any, TypeVar

import httpx
import pytest
from conftest import fda_client, label_body, shortage_body
from pydantic_ai.models.test import TestModel

import conversation as conv
import nadac
import pydantic_agent as ai
from nadac import NadacStore
from session_state import SessionState


def deps(
    text: str,
    store: NadacStore,
    *,
    client: httpx.AsyncClient | None = None,
    selection: dict[str, Any] | None = None,
    intake: ai.Intake | None = None,
) -> conv.TurnDeps:
    """One turn's inputs, with narration stubbed to a deterministic model."""
    return conv.TurnDeps(
        text=text,
        selection=selection or {},
        store=store,
        http=client or fda_client({}),
        intake=intake,
        narration_model=TestModel(custom_output_text="Here is the estimate."),
    )


ReplyT = TypeVar("ReplyT", bound=conv.Reply)


def only(replies: list[conv.Reply], kind: type[ReplyT]) -> ReplyT:
    found = [r for r in replies if isinstance(r, kind)]
    assert found, f"expected a {kind.__name__}, got {[type(r).__name__ for r in replies]}"
    return found[0]


class TestQuantityGate:
    async def test_a_total_is_never_computed_without_a_fill_size(self, store: NadacStore) -> None:
        """NADAC prices per tablet, so 30 days is asked for, never assumed."""
        state = SessionState(stripe_paid=True)
        turn = deps(
            "metformin 1000mg",
            store,
            intake=ai.Intake(intent="price", drug_text="metformin", strength_text="1000mg"),
        )
        await conv.advance(state, turn)
        only(turn.outbox, conv.AskQuantityReply)
        assert not [r for r in turn.outbox if isinstance(r, conv.PriceList)]
        assert state.node == "AskQuantity"

    async def test_quantity_from_the_form_completes_the_lookup(self, store: NadacStore) -> None:
        state = SessionState(node="AskQuantity", drug_key="metformin", strength="1,000 MG")
        turn = deps("", store, selection={"action": "submit_quantity", "quantity": 90})
        await conv.advance(state, turn)
        result = only(turn.outbox, conv.PriceList)
        assert result.quantity == 90


class TestTiers:
    async def test_tier1_exact_ndc(self, store: NadacStore) -> None:
        state = SessionState(quantity=30, stripe_paid=True)
        turn = deps(
            "my bottle says 27241024190",
            store,
            intake=ai.Intake(intent="price", ndc_text="27241024190"),
        )
        await conv.advance(state, turn)
        result = only(turn.outbox, conv.PriceList)
        assert result.tier == 1
        assert len(result.groups) == 1
        assert result.groups[0].description == "METFORMIN ER 1,000 MG GASTR-TB"

    async def test_tier1_unknown_ndc_asks_rather_than_guessing(self, store: NadacStore) -> None:
        state = SessionState(quantity=30, stripe_paid=True)
        turn = deps("99999999999", store, intake=ai.Intake(intent="price", ndc_text="99999999999"))
        await conv.advance(state, turn)
        assert only(turn.outbox, conv.Say)

    async def test_retyping_the_ndc_after_a_miss_still_gets_the_ndc_answer(
        self, store: NadacStore
    ) -> None:
        """A real NDC (e.g. a brand product NADAC never surveys, like Advil's
        0573-0154-60) that misses parks the machine at AskDrug. Sending the same
        NDC again there must repeat the real "not in the NADAC file" answer, not
        "couldn't match to a drug I cover" - that message is only true of a name.
        """
        state = SessionState(node="AskDrug", quantity=30, stripe_paid=True)
        turn = deps("0573-0154-60", store)
        await conv.advance(state, turn)
        reply = only(turn.outbox, conv.Say)
        assert "nadac" in reply.text.lower()
        assert not [r for r in turn.outbox if isinstance(r, conv.ChooseDrug)]

    async def test_ndc_recognized_even_when_the_classifier_misses_it(
        self, store: NadacStore
    ) -> None:
        """The single point of failure this agent's Tier 1 depended on: with a
        drug already active in state, AskDrug's own parse_ndc fallback is
        unreachable (Start only routes there when drug_key is still None), so
        a classifier that fails to populate intake.ndc_text for a natural-
        language-wrapped NDC previously fell straight through to whatever
        drug/strength was already in state - confirmed live for "I saw this
        NDC please check 27241-0241-90" landing on Tier 2 metformin instead
        of the real product. Start now runs drugs.parse_ndc on the raw text
        unconditionally whenever intake didn't already flag an NDC attempt.
        """
        state = SessionState(quantity=30, stripe_paid=True, drug_key="metformin")
        turn = deps(
            "I saw this NDC please check 27241-0241-90",
            store,
            intake=ai.Intake(intent="info", drug_text=None, ndc_text=None),
        )
        await conv.advance(state, turn)
        result = only(turn.outbox, conv.PriceList)
        assert result.tier == 1
        assert result.groups[0].description == "METFORMIN ER 1,000 MG GASTR-TB"

    async def test_ndc_recognized_even_when_classification_fails_outright(
        self, store: NadacStore
    ) -> None:
        """intake is None whenever the classify() call itself raises (see
        chat_proto.py) - the backstop must not depend on getting an Intake
        object at all, only on the raw text.
        """
        state = SessionState(quantity=30, stripe_paid=True, drug_key="metformin")
        turn = deps("27241-0241-90", store, intake=None)
        await conv.advance(state, turn)
        result = only(turn.outbox, conv.PriceList)
        assert result.tier == 1

    async def test_backstop_does_not_fire_on_ordinary_text_with_no_ndc(
        self, store: NadacStore
    ) -> None:
        """The backstop must not manufacture an NDC out of unrelated numbers -
        confirmed here that a plain drug question with a quantity in it still
        resolves by name, not by misreading "30" as part of an NDC.
        """
        state = SessionState(quantity=30, stripe_paid=True)
        turn = deps(
            "how much for 30 metformin",
            store,
            intake=ai.Intake(intent="price", drug_text="metformin", quantity=30),
        )
        await conv.advance(state, turn)
        result = only(turn.outbox, conv.PriceList)
        assert result.tier in {2, 3}

    async def test_tier2_divergent_formulations_show_every_option(self, store: NadacStore) -> None:
        """The metformin case: three real products, no single confident number."""
        state = SessionState(quantity=30, stripe_paid=True)
        turn = deps(
            "metformin 1000 mg",
            store,
            intake=ai.Intake(intent="price", drug_text="metformin", strength_text="1000 mg"),
        )
        await conv.advance(state, turn)
        result = only(turn.outbox, conv.PriceList)
        assert result.tier == 2
        assert len(result.groups) == 3
        assert result.tight is False

    async def test_tier2_close_prices_stay_two_groups_not_one_arbitrary_pick(
        self, store: NadacStore
    ) -> None:
        """Two real generic manufacturer NDCs, ~4% apart. Close enough that,
        considered on their own, they would be "tight" - but that must never
        come at the cost of silently dropping one manufacturer's real product
        from the list: each is its own group with its own real price.
        """
        state = SessionState(quantity=30, stripe_paid=True)
        turn = deps(
            "atorvastatin 40mg",
            store,
            intake=ai.Intake(intent="price", drug_text="atorvastatin", strength_text="40mg"),
        )
        await conv.advance(state, turn)
        result = only(turn.outbox, conv.PriceList)
        # The brand row is a separate base_name, so filter to the generic.
        generics = [g for g in result.groups if g.base_name == "ATORVASTATIN"]
        assert len(generics) == 2
        assert sorted(g.per_unit for g in generics) == pytest.approx([0.03739, 0.03900])
        assert nadac.is_tight(generics)

    async def test_tier3_name_only_spans_strengths(self, store: NadacStore) -> None:
        state = SessionState(quantity=30, stripe_paid=True)
        turn = deps("metformin", store, intake=ai.Intake(intent="price", drug_text="metformin"))
        await conv.advance(state, turn)
        result = only(turn.outbox, conv.PriceList)
        assert result.tier == 3
        assert len({g.strength for g in result.groups}) > 1

    async def test_unavailable_strength_lists_what_exists(self, store: NadacStore) -> None:
        state = SessionState(quantity=30, stripe_paid=True)
        turn = deps(
            "metformin 850mg",
            store,
            intake=ai.Intake(intent="price", drug_text="metformin", strength_text="850mg"),
        )
        await conv.advance(state, turn)
        message = only(turn.outbox, conv.Say)
        assert "1,000 MG" in message.text


class TestUnknownDrugs:
    async def test_uncovered_drug_says_so_plainly(self, store: NadacStore) -> None:
        state = SessionState(stripe_paid=True)
        turn = deps(
            "what does warfarin cost", store, intake=ai.Intake(intent="price", drug_text="warfarin")
        )
        await conv.advance(state, turn)
        chooser = only(turn.outbox, conv.ChooseDrug)
        assert chooser.options
        assert state.node == "AskDrug"

    async def test_brand_name_resolves_to_the_generic(self, store: NadacStore) -> None:
        state = SessionState(quantity=30, stripe_paid=True)
        turn = deps(
            "how much is Glucophage",
            store,
            intake=ai.Intake(intent="price", drug_text="Glucophage"),
        )
        await conv.advance(state, turn)
        result = only(turn.outbox, conv.PriceList)
        assert result.drug.key == "metformin"

    async def test_partial_name_is_not_fuzzy_matched(self, store: NadacStore) -> None:
        state = SessionState(stripe_paid=True)
        turn = deps("metfor", store, intake=ai.Intake(intent="price", drug_text="metfor"))
        await conv.advance(state, turn)
        only(turn.outbox, conv.ChooseDrug)


class TestMedicalBoundary:
    @pytest.mark.parametrize(
        "text",
        [
            "should i take metformin",
            "is this dose right for me",
            "can i take it with alcohol",
            "how much should i take",
            "should i stop taking this",
        ],
    )
    def test_dosing_questions_are_caught(self, text: str) -> None:
        assert ai.crosses_medical_boundary(text)

    @pytest.mark.parametrize(
        "text",
        [
            "how much is metformin",
            "what does the label say",
            "price for 90 tablets",
            "what is atorvastatin used for",
        ],
    )
    def test_ordinary_questions_pass(self, text: str) -> None:
        assert not ai.crosses_medical_boundary(text)

    def test_boundary_redirects_to_a_person(self) -> None:
        assert "pharmacist" in ai.BOUNDARY_REPLY.lower()


class TestDrugInfo:
    async def test_label_text_is_returned_verbatim(self, store: NadacStore) -> None:
        text = "Take exactly as prescribed. Do not crush the tablet."
        client = fda_client({"_exists_:spl_medguide": label_body("spl_medguide", text)})
        state = SessionState(drug_key="metformin", stripe_paid=True)
        turn = deps(
            "what is metformin",
            store,
            client=client,
            intake=ai.Intake(intent="info", drug_text="metformin"),
        )
        await conv.advance(state, turn)
        info = only(turn.outbox, conv.DrugInfo)
        assert info.label is not None
        assert info.label.text == text

    async def test_missing_label_is_admitted_not_filled_in(self, store: NadacStore) -> None:
        state = SessionState(drug_key="metformin", stripe_paid=True)
        turn = deps(
            "what is metformin", store, intake=ai.Intake(intent="info", drug_text="metformin")
        )
        await conv.advance(state, turn)
        info = only(turn.outbox, conv.DrugInfo)
        assert info.label is None

    async def test_info_lookup_pins_the_chosen_document_into_state(self, store: NadacStore) -> None:
        """The fix for the metformin IR/ER splice bug: the resolved document's
        set_id must be persisted so a later "full clinical detail" follow-up
        reads the same product instead of resolving independently.
        """
        doc = label_body("spl_medguide", "Patient guide.", set_id="PINNED-SET")
        client = fda_client({"_exists_:spl_medguide": doc})
        state = SessionState(drug_key="metformin", stripe_paid=True)
        turn = deps(
            "what is metformin",
            store,
            client=client,
            intake=ai.Intake(intent="info", drug_text="metformin"),
        )
        await conv.advance(state, turn)
        assert state.label_set_id == "PINNED-SET"

    async def test_full_detail_after_info_reuses_the_pinned_document(
        self, store: NadacStore
    ) -> None:
        """A different, independently-resolvable document must NOT be used for
        the detail follow-up once an info card already pinned one - the exact
        bug reported: boxed_warning and drug_interactions from two different
        real products spliced under one "Metformin" card.
        """
        pinned = label_body(
            "spl_medguide",
            "Patient guide.",
            set_id="ER-SET",
            extra_fields={"drug_interactions": "ER product's own Table 2."},
        )
        other_ir_product = label_body(
            "drug_interactions", "IR product's own Table 3.", set_id="IR-SET"
        )
        client = fda_client(
            {"_exists_:spl_medguide": pinned, "_exists_:drug_interactions": other_ir_product}
        )
        state = SessionState(drug_key="metformin", stripe_paid=True)

        info_turn = deps(
            "what is metformin",
            store,
            client=client,
            intake=ai.Intake(intent="info", drug_text="metformin"),
        )
        await conv.advance(state, info_turn)
        assert state.label_set_id == "ER-SET"

        detail_turn = deps(
            "full clinical detail",
            store,
            client=client,
            intake=ai.Intake(intent="more_detail", drug_text="metformin"),
        )
        await conv.advance(state, detail_turn)
        detail = only(detail_turn.outbox, conv.DrugDetail)
        interactions = next(s for s in detail.sections if s.field == "drug_interactions")
        assert interactions.text == "ER product's own Table 2."
        assert interactions.set_id == "ER-SET"

    async def test_switching_drugs_clears_the_pinned_label_document(
        self, store: NadacStore
    ) -> None:
        state = SessionState(
            drug_key="metformin", label_set_id="OLD-METFORMIN-SET", stripe_paid=True
        )
        turn = deps(
            "ibuprofen",
            store,
            intake=ai.Intake(intent="price", drug_text="ibuprofen"),
        )
        await conv.advance(state, turn)
        assert state.drug_key == "ibuprofen"
        assert state.label_set_id is None


class TestFormulationDisambiguation:
    """The IR/ER problem one level up from pinning: pinning alone stops a
    card from splicing two products together, but says nothing about *which*
    formulation gets pinned when a curated fda_generic_name genuinely covers
    more than one (confirmed live: metformin, gabapentin, pantoprazole,
    bupropion, fluoxetine, alprazolam). These exercise the two-part answer:
    resolve to the user's own exact product whenever an NDC is already in
    hand, and disclose plainly when it is a guess and the guess can matter.
    """

    async def test_tier1_ndc_lookup_then_info_pins_that_exact_product(
        self, store: NadacStore
    ) -> None:
        """The real NDC from a Tier 1 price check (27241024190, the ER
        gastro-resistant tablet) must drive the FDA lookup too - not a fresh,
        independently-ambiguous priority-walk guess.
        """
        exact_doc = label_body(
            "spl_medguide",
            "ER product's own guide.",
            set_id="EXACT-ER-SET",
            extra_fields={"dosage_and_administration": "Take once daily."},
        )
        # LookupInfo resolves once by ndc to learn the set_id, then
        # fetch_detail_sections re-resolves by that set_id within the same
        # turn (the same two-step pin-then-fetch already used for the
        # splicing fix) - both queries must find this document.
        client = fda_client({"openfda.product_ndc:": exact_doc, 'set_id:"EXACT-ER-SET"': exact_doc})
        state = SessionState(quantity=30, stripe_paid=True)

        price_turn = deps(
            "my bottle says 27241024190",
            store,
            intake=ai.Intake(intent="price", ndc_text="27241024190"),
        )
        await conv.advance(state, price_turn)
        assert state.drug_key == "metformin"

        info_turn = deps(
            "show me the full clinical detail",
            store,
            client=client,
            intake=ai.Intake(intent="more_detail", drug_text="metformin"),
        )
        await conv.advance(state, info_turn)
        assert state.label_exact is True
        assert state.label_set_id == "EXACT-ER-SET"
        detail = only(info_turn.outbox, conv.DrugDetail)
        dosing = next(s for s in detail.sections if s.field == "dosage_and_administration")
        assert dosing.formulation_note is None

    async def test_no_ndc_known_gets_a_disclosure_next_to_the_dosing_section(
        self, store: NadacStore
    ) -> None:
        """Drug known only by name (no price check yet, no NDC) - metformin's
        real ambiguity (confirmed live) must be disclosed, not silently
        guessed at as if only one metformin existed.
        """
        doc = label_body(
            "spl_medguide",
            "Guide text.",
            set_id="GUESSED-SET",
            extra_fields={
                "dosage_and_administration": "Extended-release tablets taken once daily."
            },
        )
        client = fda_client({'generic_name.exact:"METFORMIN HYDROCHLORIDE"': doc})
        state = SessionState(drug_key="metformin", stripe_paid=True)
        turn = deps(
            "show me the full clinical detail",
            store,
            client=client,
            intake=ai.Intake(intent="more_detail", drug_text="metformin"),
        )
        await conv.advance(state, turn)
        assert state.label_exact is False
        detail = only(turn.outbox, conv.DrugDetail)
        dosing = next(s for s in detail.sections if s.field == "dosage_and_administration")
        assert dosing.formulation_note is not None

    async def test_a_drug_with_no_real_split_never_gets_a_disclosure(
        self, store: NadacStore
    ) -> None:
        """Lisinopril has no formulation split in NADAC (see conftest seed
        data) - the disclosure must not fire just because no NDC is known;
        that alone isn't evidence of a real mismatch risk.
        """
        doc = label_body(
            "spl_medguide",
            "Guide text.",
            set_id="LISINOPRIL-SET",
            extra_fields={"dosage_and_administration": "Take once daily."},
        )
        client = fda_client({'generic_name.exact:"LISINOPRIL"': doc})
        state = SessionState(drug_key="lisinopril", stripe_paid=True)
        turn = deps(
            "show me the full clinical detail",
            store,
            client=client,
            intake=ai.Intake(intent="more_detail", drug_text="lisinopril"),
        )
        await conv.advance(state, turn)
        detail = only(turn.outbox, conv.DrugDetail)
        dosing = next(s for s in detail.sections if s.field == "dosage_and_administration")
        assert dosing.formulation_note is None

    async def test_metoprolol_price_list_discloses_the_other_salt_is_mixed_in(
        self, store: NadacStore
    ) -> None:
        """The plain Tier 3 price list for metoprolol - not just the FDA-info
        card - must not present the succinate ER rows as an unexplained,
        >100x-priced formulation choice; confirmed live this split reaches
        the price list via NADAC's own description prefix, with no
        classification distinguishing salts the way generic-vs-brand does.
        """
        import chat_proto

        state = SessionState(drug_key="metoprolol", quantity=30, stripe_paid=True)
        turn = deps(
            "how much is metoprolol",
            store,
            intake=ai.Intake(intent="price", drug_text="metoprolol"),
        )
        await conv.advance(state, turn)
        price_list = only(turn.outbox, conv.PriceList)
        assert any(g.base_name == "METOPROLOL SUCC ER" for g in price_list.groups)
        card = chat_proto.price_list_card(price_list)
        assert "extended-release metoprolol succinate" in card["card_payload"]

    async def test_metoprolol_gets_a_coverage_note_not_a_resolution_disclosure(
        self, store: NadacStore
    ) -> None:
        """Metoprolol's NADAC split is a different salt (tartrate IR vs
        succinate ER) filed under a different openFDA generic_name entirely
        (confirmed live) - the info lookup can never actually land on the ER
        product, so the IR/ER disclosure would be misleading; the real gap is
        coverage, named via Drug.also_priced_as instead.
        """
        doc = label_body(
            "spl_medguide",
            "Tartrate guide.",
            set_id="TARTRATE-SET",
            extra_fields={"dosage_and_administration": "Take twice daily."},
        )
        client = fda_client({'generic_name.exact:"METOPROLOL TARTRATE"': doc})
        state = SessionState(drug_key="metoprolol", stripe_paid=True)
        turn = deps(
            "show me the full clinical detail",
            store,
            client=client,
            intake=ai.Intake(intent="more_detail", drug_text="metoprolol"),
        )
        await conv.advance(state, turn)
        detail = only(turn.outbox, conv.DrugDetail)
        dosing = next(s for s in detail.sections if s.field == "dosage_and_administration")
        assert dosing.formulation_note is None


class TestShortageCrossCheck:
    async def test_current_shortage_rides_along_with_the_price(self, store: NadacStore) -> None:
        client = fda_client({"FUROSEMIDE": shortage_body()})
        state = SessionState(quantity=30, stripe_paid=True)
        turn = deps(
            "furosemide 40mg",
            store,
            client=client,
            intake=ai.Intake(intent="price", drug_text="furosemide", strength_text="40mg"),
        )
        await conv.advance(state, turn)
        result = only(turn.outbox, conv.PriceList)
        assert result.shortage is not None
        assert result.shortage.status == "Current"

    async def test_a_shortage_outage_does_not_fail_the_price_check(self, store: NadacStore) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(503, json={"error": {"code": "BOOM", "message": "down"}})

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        state = SessionState(quantity=30, stripe_paid=True)
        turn = deps(
            "metformin 500mg",
            store,
            client=client,
            intake=ai.Intake(intent="price", drug_text="metformin", strength_text="500mg"),
        )
        await conv.advance(state, turn)
        result = only(turn.outbox, conv.PriceList)
        assert result.shortage is None


class TestQuoteComparison:
    async def test_a_quote_is_measured_not_judged(self, store: NadacStore) -> None:
        state = SessionState(
            node="ShowPrices",
            drug_key="metformin",
            strength="500 MG",
            quantity=30,
            groups=store.current_groups("metformin", "500 MG"),
        )
        turn = deps("my pharmacy quoted me $45", store)
        await conv.advance(state, turn)
        comparison = only(turn.outbox, conv.QuoteComparison)
        assert comparison.quoted_usd == 45.0
        assert comparison.low_usd < comparison.high_usd


class TestNavigation:
    async def test_tapping_a_formulation_opens_its_detail(self, store: NadacStore) -> None:
        groups = store.current_groups("metformin", "1,000 MG")
        state = SessionState(node="ShowPrices", drug_key="metformin", quantity=30, groups=groups)
        turn = deps("", store, selection={"action": "pick_group", "group_id": groups[0].group_id})
        await conv.advance(state, turn)
        detail = only(turn.outbox, conv.PriceDetail)
        assert detail.group.group_id == groups[0].group_id
        assert state.node == "ShowPriceDetail"

    async def test_back_returns_to_the_list(self, store: NadacStore) -> None:
        groups = store.current_groups("metformin", "1,000 MG")
        state = SessionState(
            node="ShowPriceDetail",
            drug_key="metformin",
            quantity=30,
            groups=groups,
            selected_group_id=groups[0].group_id,
        )
        turn = deps("", store, selection={"action": "back_to_prices"})
        await conv.advance(state, turn)
        only(turn.outbox, conv.PriceList)
        assert state.node == "ShowPrices"

    async def test_switching_drugs_clears_the_previous_strength(self, store: NadacStore) -> None:
        state = SessionState(
            node="ShowPrices", drug_key="metformin", strength="1,000 MG", quantity=30
        )
        turn = deps(
            "what about lisinopril",
            store,
            intake=ai.Intake(intent="price", drug_text="lisinopril"),
        )
        await conv.advance(state, turn)
        result = only(turn.outbox, conv.PriceList)
        assert result.drug.key == "lisinopril"
        assert state.strength is None

    async def test_malformed_ndc_gets_explicit_feedback_not_a_stale_reprint(
        self, store: NadacStore
    ) -> None:
        """A broken NDC (e.g. only two of three segments) must be named as broken,
        not silently dropped in favour of re-running the previous drug's lookup -
        which looks exactly like the new input was ignored.
        """
        state = SessionState(drug_key="metformin", quantity=30, stripe_paid=True)
        turn = deps("0002-0152", store, intake=ai.Intake(intent="price", ndc_text="0002-0152"))
        await conv.advance(state, turn)
        reply = only(turn.outbox, conv.Say)
        assert "ndc" in reply.text.lower()
        assert not [r for r in turn.outbox if isinstance(r, conv.PriceList)]
        assert state.node == "AskDrug"

    async def test_vague_switch_request_asks_which_drug_instead_of_repeating(
        self, store: NadacStore
    ) -> None:
        """ "let's search another drug" names no drug, so the machine must ask
        rather than fall through to re-showing the drug already in state.
        """
        state = SessionState(drug_key="metformin", quantity=30, stripe_paid=True)
        turn = deps("let search another drug", store, intake=ai.Intake(intent="new_search"))
        await conv.advance(state, turn)
        chooser = only(turn.outbox, conv.ChooseDrug)
        assert chooser.options
        assert not [r for r in turn.outbox if isinstance(r, conv.PriceList)]
        assert state.node == "AskDrug"

    async def test_new_search_intent_with_a_named_drug_switches_right_away(
        self, store: NadacStore
    ) -> None:
        """If the same message that says "search another" also names one, no need
        to ask - the drug-resolution branch above already handled the switch.
        """
        state = SessionState(drug_key="metformin", quantity=30, stripe_paid=True)
        turn = deps(
            "let's check lisinopril instead",
            store,
            intake=ai.Intake(intent="new_search", drug_text="lisinopril"),
        )
        await conv.advance(state, turn)
        result = only(turn.outbox, conv.PriceList)
        assert result.drug.key == "lisinopril"

    async def test_a_named_drug_still_switches_when_intake_classification_failed(
        self, store: NadacStore
    ) -> None:
        """Reproduces a live bug: asking about a new drug by name while another
        was active kept returning the old one, because intake extraction (an LLM
        call) can miss a name in idiomatic phrasing like "how about X?" on any
        given turn, and intake=None is indistinguishable here from "classification
        raised" (chat_proto.py swallows that exception). With no intake to read,
        Start.run() must still catch the switch from the raw text itself, not
        silently keep the old drug.
        """
        state = SessionState(drug_key="metformin", quantity=30, stripe_paid=True)
        turn = deps("how about lisinopril?", store, intake=None)
        await conv.advance(state, turn)
        result = only(turn.outbox, conv.PriceList)
        assert result.drug.key == "lisinopril"

    async def test_tapping_a_drug_button_on_the_post_payment_welcome_card_works(
        self, store: NadacStore
    ) -> None:
        """The welcome card shown right after payment (state.node == "Start",
        no drug picked yet) offers one button per curated drug carrying
        {"drug_key": ..., "action": "pick_drug"}. A direct @mention delivers
        that selection verbatim as the message text too - Start.run() has no
        dedicated drug_key selection reader, so this is the very first thing
        every paying user's first tap depends on.
        """
        state = SessionState(quantity=30, stripe_paid=True)  # node defaults to "Start"
        text = '{"drug_key": "hydrochlorothiazide", "action": "pick_drug"}'
        turn = deps(
            text, store, selection={"drug_key": "hydrochlorothiazide", "action": "pick_drug"}
        )
        await conv.advance(state, turn)
        assert state.drug_key == "hydrochlorothiazide"
        assert not [r for r in turn.outbox if isinstance(r, conv.ChooseDrug)]

    async def test_switching_via_raw_text_clears_the_old_formulation_choice(
        self, store: NadacStore
    ) -> None:
        """The raw-text fallback switch must reset strength/groups exactly like
        the intake-driven switch does, so a stale formulation from the previous
        drug can't leak into the new drug's results.
        """
        state = SessionState(
            drug_key="metformin",
            strength="1000 mg",
            selected_group_id="some-old-group",
            quantity=30,
            stripe_paid=True,
        )
        turn = deps("how about lisinopril?", store, intake=None)
        await conv.advance(state, turn)
        assert state.strength is None
        assert state.selected_group_id is None


class TestPaywall:
    """The whole agent is gated behind one upfront charge, checked before anything else."""

    async def test_a_bare_greeting_triggers_the_charge_before_any_intent_processing(
        self, store: NadacStore
    ) -> None:
        state = SessionState()
        turn = deps("hi there", store)
        await conv.advance(state, turn)
        ask = only(turn.outbox, conv.AskUpfrontPayment)
        assert ask.amount_cents > 0
        assert state.node == "AwaitingAccessPayment"

    async def test_a_real_drug_question_also_hits_the_paywall_first(
        self, store: NadacStore
    ) -> None:
        """Even a message that looks fully answerable gets the paywall, not an answer."""
        state = SessionState()
        turn = deps(
            "how much is metformin", store, intake=ai.Intake(intent="price", drug_text="metformin")
        )
        await conv.advance(state, turn)
        only(turn.outbox, conv.AskUpfrontPayment)
        assert not [r for r in turn.outbox if isinstance(r, conv.PriceList)]
        assert state.drug_key is None  # never reached Start, so nothing was absorbed

    async def test_awaiting_payment_reminds_rather_than_re_asks(self, store: NadacStore) -> None:
        state = SessionState(node="AwaitingAccessPayment")
        turn = deps("are you there", store)
        await conv.advance(state, turn)
        only(turn.outbox, conv.Say)
        assert not [r for r in turn.outbox if isinstance(r, conv.AskUpfrontPayment)]
        assert state.node == "AwaitingAccessPayment"

    async def test_once_paid_the_same_kind_of_message_flows_straight_through(
        self, store: NadacStore
    ) -> None:
        state = SessionState(stripe_paid=True)
        turn = deps(
            "how much is metformin", store, intake=ai.Intake(intent="price", drug_text="metformin")
        )
        await conv.advance(state, turn)
        assert not [r for r in turn.outbox if isinstance(r, conv.AskUpfrontPayment)]


class TestUnlockedFeatures:
    """Price trend and brand-vs-generic run directly once the upfront charge has cleared."""

    async def test_brand_compare_runs_immediately_no_payment_prompt(
        self, store: NadacStore
    ) -> None:
        groups = store.current_groups("atorvastatin")
        state = SessionState(
            node="ShowPrices",
            drug_key="atorvastatin",
            quantity=30,
            groups=groups,
            stripe_paid=True,
        )
        turn = deps("", store, selection={"action": "brand_compare"})
        await conv.advance(state, turn)
        assert not [r for r in turn.outbox if isinstance(r, conv.AskUpfrontPayment)]
        only(turn.outbox, conv.CompareResult)

    async def test_price_trend_needs_a_chosen_formulation_first(self, store: NadacStore) -> None:
        groups = store.current_groups("metformin", "1,000 MG")
        state = SessionState(
            node="ShowPrices",
            drug_key="metformin",
            quantity=30,
            groups=groups,
            stripe_paid=True,
        )
        turn = deps("", store, selection={"action": "price_trend"})
        await conv.advance(state, turn)
        assert not [r for r in turn.outbox if isinstance(r, conv.AskUpfrontPayment)]
        only(turn.outbox, conv.Say)

    async def test_price_trend_runs_once_a_formulation_is_selected(self, store: NadacStore) -> None:
        groups = store.current_groups("metformin", "1,000 MG")
        group = next(g for g in groups if g.form == "GASTR-TB")
        state = SessionState(
            node="ShowPrices",
            drug_key="metformin",
            quantity=30,
            groups=groups,
            selected_group_id=group.group_id,
            stripe_paid=True,
        )
        turn = deps("", store, selection={"action": "price_trend"})
        await conv.advance(state, turn)
        result = only(turn.outbox, conv.TrendResult)
        assert len(result.points) == 3


class TestGraphRunner:
    async def test_the_machine_always_stops_on_a_wait_node(self, store: NadacStore) -> None:
        state = SessionState(stripe_paid=True)
        turn = deps("hello", store, intake=ai.Intake(intent="other"))
        await conv.advance(state, turn)
        node = conv.NODES[state.node]
        assert issubclass(node, conv.WaitNode)

    async def test_state_survives_a_serialization_round_trip(self, store: NadacStore) -> None:
        """Every turn is persisted to ctx.storage as JSON and rebuilt from it."""
        from session_state import _ADAPTER

        state = SessionState(quantity=30, stripe_paid=True)
        turn = deps(
            "metformin 1000mg",
            store,
            intake=ai.Intake(intent="price", drug_text="metformin", strength_text="1000mg"),
        )
        await conv.advance(state, turn)
        restored = _ADAPTER.validate_json(_ADAPTER.dump_json(state))
        assert restored.node == state.node
        assert [g.group_id for g in restored.groups] == [g.group_id for g in state.groups]
