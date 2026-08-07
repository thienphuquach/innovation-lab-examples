"""Name handling: the combination-product filter, resolution, and parsing."""

from __future__ import annotations

import pytest

import drugs


class TestCombinationFilter:
    """Starts-with alone leaks combos; the hyphen exclusion is what fixes it."""

    @pytest.mark.parametrize(
        "description",
        [
            "LISINOPRIL-HYDROCHLOROTHIAZIDE 20-12.5 MG TAB",
            "LISINOPRIL-HYDROCHLOROTHIAZIDE 10-12.5 MG TAB",
        ],
    )
    def test_rejects_combinations(self, description: str) -> None:
        assert not drugs.is_single_ingredient(description, "LISINOPRIL")

    @pytest.mark.parametrize(
        "description",
        ["LISINOPRIL 10 MG TABLET", "LISINOPRIL 2.5 MG TABLET", "LISINOPRIL 40 MG TABLET"],
    )
    def test_keeps_single_ingredient(self, description: str) -> None:
        assert drugs.is_single_ingredient(description, "LISINOPRIL")

    def test_amlodipine_is_the_worst_case(self) -> None:
        """30 of amlodipine's 33 NADAC descriptions are combinations."""
        assert not drugs.is_single_ingredient("AMLODIPINE-ATORVAST 10-20 MG", "AMLODIPINE")
        assert not drugs.is_single_ingredient("AMLODIPINE-BENAZEPRIL 5-20 MG CP", "AMLODIPINE")
        assert drugs.is_single_ingredient("AMLODIPINE BESYLATE 10 MG TAB", "AMLODIPINE")

    def test_metformin_combos_sort_the_other_way(self) -> None:
        """Metformin never appears first in a combo, which is why it looked clean."""
        assert not drugs.is_single_ingredient("GLIPIZIDE-METFORMIN 5-500 MG", "METFORMIN")
        assert drugs.is_single_ingredient("METFORMIN ER 1,000 MG OSM-TAB", "METFORMIN")

    def test_ibuprofen_combos_hide_behind_a_space_not_a_hyphen(self) -> None:
        """Confirmed live: Advil PM/Cold-Sinus are real NADAC rows that pass the
        plain prefix check because the second ingredient isn't hyphenated onto
        the drug name the way lisinopril/amlodipine combos are.
        """
        # ibuprofen 200 mg + diphenhydramine 38 mg - the dual strength gives it away.
        assert not drugs.is_single_ingredient("IBUPROFEN PM 200-38 MG CAPLET", "IBUPROFEN")
        # No strength at all - NADAC doesn't reduce a compound formulation to one number.
        assert not drugs.is_single_ingredient("IBUPROFEN PM CAPLET", "IBUPROFEN")
        assert not drugs.is_single_ingredient("IBUPROFEN COLD-SINUS CPLT", "IBUPROFEN")
        # Junior Strength is plain single-ingredient ibuprofen for kids - keep it.
        assert drugs.is_single_ingredient("IBUPROFEN JR STR 100 MG TB CHW", "IBUPROFEN")
        assert drugs.is_single_ingredient("IBUPROFEN 200 MG TABLET", "IBUPROFEN")


class TestResolve:
    def test_by_generic_name(self) -> None:
        assert drugs.resolve("how much is metformin") is drugs.BY_KEY["metformin"]

    def test_by_brand_name(self) -> None:
        """A brand the user says instead of the generic resolves, not guesses."""
        assert drugs.resolve("what does Glucophage cost") is drugs.BY_KEY["metformin"]
        assert drugs.resolve("price of Lipitor") is drugs.BY_KEY["atorvastatin"]
        assert drugs.resolve("Synthroid") is drugs.BY_KEY["levothyroxine"]

    def test_refuses_to_fuzzy_match(self) -> None:
        """A misremembered name returns nothing so the caller can ask."""
        assert drugs.resolve("metfor") is None
        assert drugs.resolve("lisinipril") is None
        assert drugs.resolve("something entirely different") is None

    def test_uncovered_drug_is_not_forced(self) -> None:
        assert drugs.resolve("warfarin") is None


class TestParsing:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("1000mg", "1,000 MG"),
            ("1,000 mg", "1,000 MG"),
            ("500 MG", "500 MG"),
            ("175 mcg", "175 MCG"),
            ("12.5mg", "12.5 MG"),
            ("250 mg/5 ml", "250 MG/5 ML"),
        ],
    )
    def test_strength_normalizes_to_nadac_spelling(self, text: str, expected: str) -> None:
        assert drugs.parse_strength(text) == expected

    def test_no_strength(self) -> None:
        assert drugs.parse_strength("just metformin please") is None

    @pytest.mark.parametrize(
        "text",
        ["27241024190", "27241-0241-90", "NDC 27241 0241 90"],
    )
    def test_ndc_parses_hyphenated_or_not(self, text: str) -> None:
        assert drugs.parse_ndc(text) == "27241024190"

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            # Advil's real NDC as printed on the label and in the FDA's own NDC
            # directory: 10 digits, 4-4-2, no leading zero on the labeler code.
            ("0573-0154-60", "00573015460"),
            # 5-3-2: the product segment is the short one this time.
            ("12345-123-12", "12345012312"),
            # 5-4-1: the package segment is the short one.
            ("12345-1234-1", "12345123401"),
            # Space-separated 10-digit still normalizes the same way.
            ("0573 0154 60", "00573015460"),
        ],
    )
    def test_ten_digit_ndc_layouts_normalize_to_eleven_digits(
        self, text: str, expected: str
    ) -> None:
        assert drugs.parse_ndc(text) == expected

    def test_bare_ten_digit_run_is_refused_not_guessed(self) -> None:
        """No dashes means no way to tell which segment is short - don't guess."""
        assert drugs.parse_ndc("0573015460") is None

    def test_short_number_is_not_an_ndc(self) -> None:
        assert drugs.parse_ndc("30 tablets") is None


def test_no_combination_products_on_the_curated_list() -> None:
    """v1 excludes combinations by design; nothing should sneak in."""
    for drug in drugs.CURATED:
        assert "-" not in drug.nadac_prefix
