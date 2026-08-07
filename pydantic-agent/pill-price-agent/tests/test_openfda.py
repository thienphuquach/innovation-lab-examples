"""openFDA: the label fallback chain, shortage semantics, and text handling."""

from __future__ import annotations

import httpx
import pytest
from conftest import fda_client, label_body, shortage_body

import openfda


class TestLabelFallback:
    async def test_prefers_the_medication_guide(self) -> None:
        async with fda_client(
            {"_exists_:spl_medguide": label_body("spl_medguide", "Plain language guide.")}
        ) as client:
            label = await openfda.fetch_label(client, "METFORMIN HYDROCHLORIDE")
        assert label is not None
        assert label.field == "spl_medguide"
        assert label.source == "FDA Medication Guide"

    async def test_falls_back_when_no_medication_guide_exists(self) -> None:
        """Lisinopril, atorvastatin and levothyroxine have zero medguides.

        Medication Guides are only required for drugs with particular serious
        risks, so without a fallback the most commonly prescribed generics would
        return nothing at all.
        """
        async with fda_client(
            {
                "_exists_:information_for_patients": label_body(
                    "information_for_patients", "17 PATIENT COUNSELING INFORMATION Advise..."
                )
            }
        ) as client:
            label = await openfda.fetch_label(client, "LISINOPRIL")
        assert label is not None
        assert label.field == "information_for_patients"
        assert "Patient Counseling" in label.source

    async def test_falls_through_to_indications(self) -> None:
        async with fda_client(
            {"_exists_:indications_and_usage": label_body("indications_and_usage", "Treats X.")}
        ) as client:
            label = await openfda.fetch_label(client, "SOMETHING")
        assert label is not None
        assert label.field == "indications_and_usage"

    async def test_no_patient_text_at_all(self) -> None:
        async with fda_client({}) as client:
            assert await openfda.fetch_label(client, "NOTHING") is None

    async def test_uses_exact_match_never_a_loose_one(self) -> None:
        """A loose match on 'metformin' pulls in sitagliptin-metformin combos."""
        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request.url.params.get("search", ""))
            return httpx.Response(404, json={"error": {"code": "NOT_FOUND", "message": "x"}})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await openfda.fetch_label(client, "METFORMIN HYDROCHLORIDE")

        assert seen
        for search in seen:
            assert 'openfda.generic_name.exact:"METFORMIN HYDROCHLORIDE"' in search


class TestLabelPinning:
    """The metformin IR/ER splice bug: every section for one turn must come
    from the one document resolve_label_document chose, never from an
    independent per-field query that could each rank a different product top.
    """

    async def test_detail_sections_all_come_from_one_document_not_a_splice(self) -> None:
        # The ER product carries the Medication Guide, so the priority walk
        # resolves to it first. It also happens to answer drug_interactions -
        # with its own "Table 2" - so that must be what's returned, never the
        # differently-numbered "Table 3" that a stale per-field IR mock offers.
        er_doc = label_body(
            "spl_medguide",
            "Medication guide for extended-release metformin.",
            set_id="ER-SET",
            product_ndc="0000-111-11",
            manufacturer="ER Manufacturer",
            extra_fields={
                "boxed_warning": (
                    "Immediately discontinue metformin hydrochloride extended-release "
                    "tablets if lactic acidosis is suspected."
                ),
                "drug_interactions": (
                    "Table 2: Clinically Significant Drug Interactions with Metformin "
                    "Hydrochloride Extended-Release Tablets"
                ),
            },
        )
        ir_only_doc = label_body(
            "drug_interactions",
            "Table 3: Clinically Significant Drug Interactions with Metformin Hydrochloride Tablets",
            set_id="IR-SET",
            product_ndc="0000-222-22",
            manufacturer="IR Manufacturer",
        )
        async with fda_client(
            {"_exists_:spl_medguide": er_doc, "_exists_:drug_interactions": ir_only_doc}
        ) as client:
            sections = await openfda.fetch_detail_sections(client, "METFORMIN HYDROCHLORIDE")

        assert sections
        assert {s.set_id for s in sections} == {"ER-SET"}, "sections must all be one document"
        interactions = next(s for s in sections if s.field == "drug_interactions")
        assert "Table 2" in interactions.text
        assert "Table 3" not in interactions.text
        assert interactions.manufacturer == "ER Manufacturer"

    async def test_missing_field_on_the_locked_document_is_reported_missing(self) -> None:
        """The IR product has no boxed_warning of its own - it must not borrow
        the ER product's, even though a query for boxed_warning alone would
        have found it.
        """
        ir_doc = label_body(
            "drug_interactions",
            "Table 3: ...",
            set_id="IR-SET",
            extra_fields={},
        )
        er_boxed = label_body("boxed_warning", "ER-only warning.", set_id="ER-SET")
        # drug_interactions is IR-only's sole field, so the priority walk (which
        # only checks PATIENT_FIELDS) never sees it; resolution falls through to
        # the unconstrained fallback and picks IR-SET as the document to pin.
        async with fda_client(
            {
                "_exists_:boxed_warning": er_boxed,
                'openfda.generic_name.exact:"METFORMIN HYDROCHLORIDE"': ir_doc,
            }
        ) as client:
            sections = await openfda.fetch_detail_sections(client, "METFORMIN HYDROCHLORIDE")

        fields = {s.field for s in sections}
        assert "drug_interactions" in fields
        assert "boxed_warning" not in fields, "must not borrow another product's section"

    async def test_detail_reuses_a_pinned_set_id_instead_of_resolving_fresh(self) -> None:
        """A caller that already locked a document (from an earlier info card)
        must get sections from that exact document, not a fresh resolution that
        could land on a different one.
        """
        pinned = label_body(
            "drug_interactions",
            "Pinned product's own table.",
            set_id="PINNED-SET",
            extra_fields={"boxed_warning": "Pinned product's own warning."},
        )
        other = label_body("spl_medguide", "A different product entirely.", set_id="OTHER-SET")
        async with fda_client(
            {'set_id:"PINNED-SET"': pinned, "_exists_:spl_medguide": other}
        ) as client:
            sections = await openfda.fetch_detail_sections(
                client, "METFORMIN HYDROCHLORIDE", set_id="PINNED-SET"
            )

        assert sections
        assert {s.set_id for s in sections} == {"PINNED-SET"}

    async def test_info_and_detail_share_the_same_pin_across_two_calls(self) -> None:
        """Simulates the real conversation flow: resolve once, then fetch the
        regular info label and the detail sections separately but pinned to
        the same set_id - what conversation.py's LookupInfo now does.
        """
        doc = label_body(
            "spl_medguide",
            "Patient guide text.",
            set_id="ONE-SET",
            extra_fields={"drug_interactions": "One document's own table."},
        )
        async with fda_client({"_exists_:spl_medguide": doc}) as client:
            resolved, _ = await openfda.resolve_label_document(client, "METFORMIN HYDROCHLORIDE")
            assert resolved is not None
            set_id = str(resolved.get("set_id"))

        async with fda_client({'set_id:"ONE-SET"': doc}) as client:
            label = await openfda.fetch_label(client, "METFORMIN HYDROCHLORIDE", set_id=set_id)
            sections = await openfda.fetch_detail_sections(
                client, "METFORMIN HYDROCHLORIDE", set_id=set_id
            )

        assert label is not None and label.set_id == "ONE-SET"
        assert sections and sections[0].set_id == "ONE-SET"

    async def test_stale_pinned_set_id_falls_through_to_a_fresh_choice(self) -> None:
        """If the pinned document no longer resolves (index churn), fall back
        to choosing fresh rather than silently returning nothing.
        """
        fresh = label_body("spl_medguide", "Fresh document.", set_id="FRESH-SET")
        async with fda_client({"_exists_:spl_medguide": fresh}) as client:
            doc, matched = await openfda.resolve_label_document(
                client, "METFORMIN HYDROCHLORIDE", set_id="LONG-GONE-SET"
            )
        assert doc is not None
        assert doc.get("set_id") == "FRESH-SET"
        assert matched is False


class TestNdcCandidates:
    """The reverse of drugs.parse_ndc's padding: which labeler-product width
    an 11-digit NADAC NDC could have come from. Confirmed live against four
    real metformin NDCs - see ARCHITECTURE.md.
    """

    @pytest.mark.parametrize(
        ("ndc", "expected"),
        [
            ("27241024190", ["27241-0241", "27241-241"]),
            ("00378718505", ["00378-7185", "0378-7185"]),
            ("29300038901", ["29300-0389", "29300-389"]),
            ("00378600191", ["00378-6001", "0378-6001"]),
        ],
    )
    def test_generates_the_confirmed_correct_reconstruction(
        self, ndc: str, expected: list[str]
    ) -> None:
        assert openfda._product_ndc_candidates(ndc) == expected

    def test_malformed_ndc_yields_no_candidates(self) -> None:
        assert openfda._product_ndc_candidates("not-an-ndc") == []
        assert openfda._product_ndc_candidates("123") == []


class TestNdcExactPinning:
    """The zero-guessing path: an NDC already in hand (Tier 1, or a tapped
    price-list formulation) resolves to the user's own real product, not a
    priority-walk guess among products that can be formulation-mismatched.
    """

    async def test_ndc_resolves_to_the_exact_product_not_a_guess(self) -> None:
        er_doc = label_body("drug_interactions", "ER product's own table.", set_id="ER-SET")
        async with fda_client({"openfda.product_ndc:": er_doc}) as client:
            doc, matched = await openfda.resolve_label_document(
                client, "METFORMIN HYDROCHLORIDE", ndc="27241024190"
            )
        assert matched is True
        assert doc is not None
        assert doc.get("set_id") == "ER-SET"

    async def test_ndc_with_no_spl_on_file_falls_through_to_priority_walk(self) -> None:
        """Confirmed real: some retail/OTC NDCs have no SPL document at all
        (e.g. Advil's real bottle NDC). Must still answer from the ordinary
        path rather than returning nothing.
        """
        fallback = label_body("spl_medguide", "Fallback document.", set_id="FALLBACK-SET")
        async with fda_client({"_exists_:spl_medguide": fallback}) as client:
            doc, matched = await openfda.resolve_label_document(
                client, "METFORMIN HYDROCHLORIDE", ndc="99999999999"
            )
        assert matched is False
        assert doc is not None
        assert doc.get("set_id") == "FALLBACK-SET"

    async def test_exact_ndc_pin_suppresses_the_dosing_disclosure(self) -> None:
        doc = label_body(
            "spl_medguide",
            "Guide text.",
            set_id="ER-SET",
            extra_fields={"dosage_and_administration": "Take once daily."},
        )
        async with fda_client({"openfda.product_ndc:": doc}) as client:
            sections = await openfda.fetch_detail_sections(
                client, "METFORMIN HYDROCHLORIDE", ndc="27241024190", disclose=True
            )
        dosing = next(s for s in sections if s.field == "dosage_and_administration")
        assert dosing.formulation_note is None


class TestProvenanceNdcDisplay:
    """One SPL document can legitimately list several package-size NDCs under
    ``openfda.product_ndc`` (confirmed live: a real Mylan metformin ER
    document lists both ``0378-6001`` and ``0378-6002`` - two bottle sizes of
    the identical product, same manufacturer, same set_id). The provenance
    note must name the package that was actually queried/tapped, not
    whichever happens to sort first in the array - otherwise a card can
    claim to show "the exact product this text came from" while displaying
    a different NDC than the one on the bottle it was pinned from.
    """

    async def test_shows_the_queried_package_not_the_first_listed(self) -> None:
        doc = label_body(
            "spl_medguide",
            "Guide text.",
            set_id="ER-SET",
            product_ndc="0378-6001",
            extra_ndcs=("0378-6002",),
        )
        async with fda_client({"openfda.product_ndc:": doc}) as client:
            label = await openfda.fetch_label(client, "METFORMIN HYDROCHLORIDE", ndc="00378600291")
        assert label is not None
        assert label.product_ndc == "0378-6002"

    async def test_detail_sections_show_the_queried_package_too(self) -> None:
        doc = label_body(
            "spl_medguide",
            "Guide text.",
            set_id="ER-SET",
            product_ndc="0378-6001",
            extra_ndcs=("0378-6002",),
            extra_fields={"boxed_warning": "Warning text."},
        )
        async with fda_client({"openfda.product_ndc:": doc}) as client:
            sections = await openfda.fetch_detail_sections(
                client, "METFORMIN HYDROCHLORIDE", ndc="00378600291"
            )
        assert sections
        assert all(s.product_ndc == "0378-6002" for s in sections)

    async def test_falls_back_to_the_first_listed_when_not_resolved_by_ndc(self) -> None:
        """Resolved by the priority walk, not by NDC - no query NDC exists to
        prefer, so the first listed package is still the best available
        answer, unchanged from before this fix.
        """
        doc = label_body(
            "spl_medguide",
            "Guide text.",
            set_id="SOME-SET",
            product_ndc="0378-6001",
            extra_ndcs=("0378-6002",),
        )
        async with fda_client({"_exists_:spl_medguide": doc}) as client:
            label = await openfda.fetch_label(client, "METFORMIN HYDROCHLORIDE")
        assert label is not None
        assert label.product_ndc == "0378-6001"


class TestFormulationDisclosure:
    """When no NDC pins the document, and the caller has confirmed (from
    NADAC, not from here) that this drug's formulations can genuinely differ,
    the dosing section names the mismatch risk plainly - attached to that one
    section, not spread across the card.
    """

    async def test_extended_release_document_names_itself(self) -> None:
        doc = label_body(
            "spl_medguide",
            "Guide text.",
            set_id="ER-SET",
            extra_fields={
                "dosage_and_administration": (
                    "Extended-release tablets are taken once daily with the evening meal."
                )
            },
        )
        async with fda_client({'generic_name.exact:"METFORMIN HYDROCHLORIDE"': doc}) as client:
            sections = await openfda.fetch_detail_sections(
                client, "METFORMIN HYDROCHLORIDE", disclose=True
            )
        dosing = next(s for s in sections if s.field == "dosage_and_administration")
        assert dosing.formulation_note is not None
        assert "extended-release" in dosing.formulation_note.lower()

    async def test_non_oral_route_is_named_plainly(self) -> None:
        """Pantoprazole's real case: an IV product resolved under a query
        that has no oral/IV distinction of its own - route, not release type.
        """
        doc = label_body(
            "spl_medguide",
            "Guide text.",
            set_id="IV-SET",
            extra_fields={"dosage_and_administration": "Administer by intravenous infusion."},
        )
        doc["results"][0]["openfda"]["route"] = ["INTRAVENOUS"]
        async with fda_client({'generic_name.exact:"PANTOPRAZOLE SODIUM"': doc}) as client:
            sections = await openfda.fetch_detail_sections(
                client, "PANTOPRAZOLE SODIUM", disclose=True
            )
        dosing = next(s for s in sections if s.field == "dosage_and_administration")
        assert dosing.formulation_note is not None
        assert "intravenous" in dosing.formulation_note.lower()

    async def test_silent_document_still_gets_a_generic_caveat(self) -> None:
        """A plain immediate-release label usually never says "immediate
        release" - confirmed live. disclose=True must still warn, generically,
        rather than staying silent just because this document doesn't
        self-describe.
        """
        doc = label_body(
            "spl_medguide",
            "Guide text.",
            set_id="IR-SET",
            extra_fields={"dosage_and_administration": "Take 500 mg twice daily with meals."},
        )
        async with fda_client({'generic_name.exact:"METFORMIN HYDROCHLORIDE"': doc}) as client:
            sections = await openfda.fetch_detail_sections(
                client, "METFORMIN HYDROCHLORIDE", disclose=True
            )
        dosing = next(s for s in sections if s.field == "dosage_and_administration")
        assert dosing.formulation_note is not None

    async def test_disclose_false_means_no_note_at_all(self) -> None:
        doc = label_body(
            "spl_medguide",
            "Guide text.",
            set_id="ER-SET",
            extra_fields={
                "dosage_and_administration": "Extended-release tablets are taken once daily."
            },
        )
        async with fda_client({'generic_name.exact:"METFORMIN HYDROCHLORIDE"': doc}) as client:
            sections = await openfda.fetch_detail_sections(
                client, "METFORMIN HYDROCHLORIDE", disclose=False
            )
        dosing = next(s for s in sections if s.field == "dosage_and_administration")
        assert dosing.formulation_note is None

    async def test_note_never_attaches_to_an_unrelated_section(self) -> None:
        """Must sit next to the dosing sentence it qualifies, never as a
        generic footnote bleeding into boxed_warning or interactions.
        """
        doc = label_body(
            "spl_medguide",
            "Guide text.",
            set_id="ER-SET",
            extra_fields={
                "dosage_and_administration": "Extended-release tablets are taken once daily.",
                "boxed_warning": "Some real warning.",
            },
        )
        async with fda_client({'generic_name.exact:"METFORMIN HYDROCHLORIDE"': doc}) as client:
            sections = await openfda.fetch_detail_sections(
                client, "METFORMIN HYDROCHLORIDE", disclose=True
            )
        boxed = next(s for s in sections if s.field == "boxed_warning")
        assert boxed.formulation_note is None


class TestShortages:
    async def test_not_found_means_no_shortage_not_an_error(self) -> None:
        """openFDA answers an empty set with HTTP 404 and an error body."""
        async with fda_client({}) as client:
            assert await openfda.fetch_shortage(client, "METFORMIN HYDROCHLORIDE") is None

    async def test_current_shortage_is_returned(self) -> None:
        async with fda_client({"FUROSEMIDE": shortage_body()}) as client:
            shortage = await openfda.fetch_shortage(client, "FUROSEMIDE")
        assert shortage is not None
        assert shortage.status == "Current"
        assert shortage.availability == "Limited supply"

    async def test_only_current_records_are_requested(self) -> None:
        """1,175 of 1,622 records are Current; resolved history must not surface."""
        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request.url.params.get("search", ""))
            return httpx.Response(404, json={"error": {"code": "NOT_FOUND", "message": "x"}})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await openfda.fetch_shortage(client, "FUROSEMIDE")

        assert 'status:"Current"' in seen[0]

    async def test_server_error_is_raised_not_swallowed(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, json={"error": {"code": "BOOM", "message": "x"}})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(openfda.OpenFdaError):
                await openfda.fetch_shortage(client, "FUROSEMIDE")


class TestReadable:
    def test_reformats_without_rewording(self) -> None:
        raw = "17  PATIENT   COUNSELING\nINFORMATION Advise patients. Tell them more."
        out = openfda.readable(raw)
        for word in ("PATIENT", "COUNSELING", "INFORMATION", "Advise", "patients", "Tell"):
            assert word in out
        assert "  " not in out

    def test_drops_only_the_leading_section_number(self) -> None:
        assert openfda.readable("17 PATIENT COUNSELING").startswith("PATIENT")
        assert "17" in openfda.readable("Take 17 tablets per the label")

    def test_truncation_cuts_at_a_sentence(self) -> None:
        raw = "First sentence here. Second sentence here. Third sentence here."
        out = openfda.readable(raw, limit=40)
        assert out.rstrip().endswith((".", "..."))
        assert len(out) <= 60

    def test_adds_no_new_words(self) -> None:
        raw = "Do not take with alcohol. Call your doctor."
        assert set(openfda.readable(raw).split()) <= set(raw.split())


class TestParagraphs:
    """Cards render each element separately, so this must split into a list -
    a single string with embedded newlines is exactly the "wall of text" bug.
    """

    def test_splits_into_multiple_blocks_not_one_string(self) -> None:
        raw = "First sentence here. Second sentence here. Third sentence here."
        blocks = openfda.paragraphs(raw)
        assert len(blocks) == 3
        assert blocks[0] == "First sentence here."

    def test_explodes_bullet_markers_into_their_own_items(self) -> None:
        raw = (
            "NSAIDs can cause serious side effects, including: "
            "\u2022 Increased risk of a heart attack or stroke that can lead to death."
        )
        blocks = openfda.paragraphs(raw)
        assert blocks[0] == "NSAIDs can cause serious side effects, including:"
        assert (
            blocks[1] == "\u2022 Increased risk of a heart attack or stroke that can lead to death."
        )

    def test_multiple_bullets_in_one_run_each_become_their_own_item(self) -> None:
        raw = "This may increase: \u2022 with increasing doses \u2022 with longer use."
        blocks = openfda.paragraphs(raw)
        assert blocks[0] == "This may increase:"
        assert blocks[1] == "\u2022 with increasing doses"
        assert blocks[2] == "\u2022 with longer use."

    def test_adds_no_new_words(self) -> None:
        raw = "Do not take with alcohol. \u2022 Call your doctor. \u2022 Avoid driving."
        joined = " ".join(openfda.paragraphs(raw)).replace("\u2022", "")
        assert set(joined.split()) <= set(raw.replace("\u2022", "").split())

    def test_no_bullets_behaves_like_readable_split_on_newlines(self) -> None:
        raw = "Advise patients. Tell them more."
        assert openfda.paragraphs(raw) == openfda.readable(raw).split("\n\n")
