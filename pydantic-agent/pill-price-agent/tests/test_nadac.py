"""NADAC: description parsing, tiered lookups, spread handling, refresh."""

from __future__ import annotations

import csv
from pathlib import Path

import httpx
import pytest

import nadac
from nadac import NadacStore, PriceGroup


def _group(base_name: str, form: str = "TABLET") -> PriceGroup:
    """A minimal PriceGroup for tests that only care about base_name/form."""
    return PriceGroup(
        drug_key="x",
        base_name=base_name,
        strength="10 MG",
        form=form,
        description=f"{base_name} 10 MG {form}",
        per_unit=0.05,
        effective_on="2026-01-01",
        as_of="2026-01-01",
        pricing_unit="EA",
        ndc_count=1,
        example_ndc="00000000000",
        generic_per_unit=None,
    )


class TestSplitDescription:
    @pytest.mark.parametrize(
        ("description", "expected"),
        [
            ("METFORMIN ER 1,000 MG GASTR-TB", ("METFORMIN ER", "1,000 MG", "GASTR-TB")),
            ("METFORMIN HCL 500 MG TABLET", ("METFORMIN HCL", "500 MG", "TABLET")),
            ("LEVOTHYROXINE 175 MCG TABLET", ("LEVOTHYROXINE", "175 MCG", "TABLET")),
            ("GABAPENTIN 250 MG/5 ML SOLN", ("GABAPENTIN", "250 MG/5 ML", "SOLN")),
            ("ALPRAZOLAM ODT 1 MG TAB", ("ALPRAZOLAM ODT", "1 MG", "TAB")),
        ],
    )
    def test_splits_name_strength_form(
        self, description: str, expected: tuple[str, str, str]
    ) -> None:
        assert nadac.split_description(description) == expected

    def test_release_qualifier_stays_in_the_name(self) -> None:
        """ER and IR are different products, so the qualifier must not be dropped."""
        er, _, _ = nadac.split_description("METFORMIN ER 1,000 MG OSM-TAB")
        ir, _, _ = nadac.split_description("METFORMIN HCL 1,000 MG TABLET")
        assert er != ir


class TestFormulationAmbiguity:
    """Whether one curated drug's own priced formulations mix release
    mechanisms or routes - the gate for the FDA-info dosing disclosure.
    Confirmed live for metformin (oral IR/ER) and pantoprazole (oral/IV).
    """

    def test_release_tag_alongside_plain_is_ambiguous(self) -> None:
        groups = [_group("METFORMIN HCL"), _group("METFORMIN HCL ER")]
        assert nadac.formulation_ambiguity(groups) is True

    def test_injectable_alongside_oral_is_ambiguous(self) -> None:
        groups = [
            _group("PANTOPRAZOLE SODIUM", form="TAB"),
            _group("PANTOPRAZOLE SODIUM", form="VIAL"),
        ]
        assert nadac.formulation_ambiguity(groups) is True

    def test_all_plain_oral_tablets_is_not_ambiguous(self) -> None:
        groups = [_group("LISINOPRIL", form="TABLET"), _group("LISINOPRIL", form="TABLET")]
        assert nadac.formulation_ambiguity(groups) is False

    def test_capsule_vs_tablet_alone_is_not_ambiguous(self) -> None:
        """A different administrable form isn't the same risk as a release-
        mechanism or route split - no dosing-frequency mismatch either way.
        """
        groups = [_group("OMEPRAZOLE", form="CAPSULE"), _group("OMEPRAZOLE", form="TABLET")]
        assert nadac.formulation_ambiguity(groups) is False

    def test_single_group_is_never_ambiguous(self) -> None:
        assert nadac.formulation_ambiguity([_group("METFORMIN HCL ER")]) is False


class TestOtherSaltGroups:
    """The helper that flags which priced entries are actually the *other*
    salt for metoprolol/carvedilol (see nadac.other_salt_groups docstring) -
    confirmed live these leak into the plain price list via NADAC's own
    description prefix, not a hypothetical.
    """

    def test_finds_the_release_tagged_entries(self, store: NadacStore) -> None:
        groups = store.current_groups("metoprolol")
        other = nadac.other_salt_groups(groups)
        assert {g.base_name for g in other} == {"METOPROLOL SUCC ER"}

    def test_drug_with_no_release_tag_split_returns_nothing(self, store: NadacStore) -> None:
        assert nadac.other_salt_groups(store.current_groups("lisinopril")) == []


class TestTiers:
    def test_tier1_exact_ndc(self, store: NadacStore) -> None:
        group = store.by_ndc("27241024190")
        assert group is not None
        assert group.description == "METFORMIN ER 1,000 MG GASTR-TB"
        # The newest row wins, not whichever was inserted last.
        assert group.effective_on == "2026-07-22"
        assert group.per_unit == pytest.approx(0.34906)

    def test_tier1_unknown_ndc(self, store: NadacStore) -> None:
        assert store.by_ndc("99999999999") is None

    def test_tier2_returns_every_formulation_at_that_strength(self, store: NadacStore) -> None:
        groups = store.current_groups("metformin", "1,000 MG")
        labels = {g.label() for g in groups}
        assert labels == {
            "METFORMIN HCL 1,000 MG TABLET",
            "METFORMIN ER 1,000 MG OSM-TAB",
            "METFORMIN ER 1,000 MG GASTR-TB",
        }

    def test_tier2_uses_current_price_only(self, store: NadacStore) -> None:
        """Three historical rows exist for the GASTR-TB NDC; only today's counts."""
        gastr = next(
            g for g in store.current_groups("metformin", "1,000 MG") if g.form == "GASTR-TB"
        )
        assert gastr.per_unit == pytest.approx(0.34906)

    def test_tier3_spans_strengths(self, store: NadacStore) -> None:
        strengths = {g.strength for g in store.current_groups("metformin")}
        assert strengths == {"1,000 MG", "500 MG"}

    def test_combination_products_are_unreachable(self, store: NadacStore) -> None:
        """The combo row is seeded but must never surface under lisinopril."""
        descriptions = {g.description for g in store.current_groups("lisinopril")}
        assert descriptions == {"LISINOPRIL 10 MG TABLET"}

    def test_strengths_listing(self, store: NadacStore) -> None:
        assert store.strengths("metformin") == ["1,000 MG", "500 MG"]


class TestSpread:
    def test_divergent_formulations_are_not_tight(self, store: NadacStore) -> None:
        """$0.023 IR against $0.349 ER is a real difference, not noise."""
        groups = store.current_groups("metformin", "1,000 MG")
        assert not nadac.is_tight(groups)
        assert nadac.spread(groups) > 10

    def test_close_prices_are_tight(self, store: NadacStore) -> None:
        """Two manufacturers of the identical tablet, 4% apart."""
        groups = [g for g in store.current_groups("atorvastatin") if g.base_name == "ATORVASTATIN"]
        assert nadac.is_tight(groups)

    def test_single_group_is_tight(self, store: NadacStore) -> None:
        assert nadac.is_tight(store.current_groups("lisinopril"))
        assert nadac.spread(store.current_groups("lisinopril")) == 0.0

    def test_threshold_boundary(self) -> None:
        assert nadac.TIGHT_SPREAD == 0.10


class TestTotals:
    def test_dispensing_fee_is_always_added(self, store: NadacStore) -> None:
        """The bare acquisition cost is never what the user is shown."""
        groups = [g for g in store.current_groups("metformin", "500 MG")]
        low, high = nadac.total_range(groups, 30)
        acquisition = groups[0].fill_cost(30)
        assert low == pytest.approx(acquisition + nadac.DISPENSING_FEE_LOW_USD)
        assert high == pytest.approx(acquisition + nadac.DISPENSING_FEE_HIGH_USD)
        assert low > acquisition

    def test_fill_cost_scales_with_quantity(self, store: NadacStore) -> None:
        group = store.current_groups("metformin", "500 MG")[0]
        assert group.fill_cost(90) == pytest.approx(group.per_unit * 90)


class TestBrandVsGeneric:
    def test_returns_brand_rows_with_a_priced_generic(self, store: NadacStore) -> None:
        rows = store.brand_vs_generic("atorvastatin")
        assert len(rows) == 1
        generic = rows[0].generic_per_unit
        assert generic is not None
        assert generic == pytest.approx(0.03739)
        assert rows[0].per_unit > generic

    def test_empty_when_no_brand_equivalent(self, store: NadacStore) -> None:
        """No brand row seeded for lisinopril; that is not an error."""
        assert store.brand_vs_generic("lisinopril") == []


class TestHistory:
    def test_history_is_chronological(self, store: NadacStore) -> None:
        points = store.history("27241024190")
        assert [p.effective_on for p in points] == ["2025-12-17", "2026-06-17", "2026-07-22"]
        assert points[0].per_unit > points[-1].per_unit

    def test_history_tracks_one_real_ndc_not_a_blended_average(self, store: NadacStore) -> None:
        """The two gabapentin solution NDCs are priced $0.020 and $0.037 - miles
        apart. History for one of them must never drift toward the other's
        price, the way averaging by base_name/strength/form used to.
        """
        points = store.history("00093500001")
        assert [p.per_unit for p in points] == pytest.approx([0.01984, 0.02011])
        # The sibling NDC's price is never blended in.
        assert all(p.per_unit < 0.03 for p in points)

    def test_unknown_ndc_has_no_history(self, store: NadacStore) -> None:
        assert store.history("99999999999") == []


class TestDivergentPackageSizes:
    """A liquid's description doesn't capture bottle size, so two package
    sizes of the identical drug/strength/form can carry two real prices -
    confirmed live for ibuprofen and amoxicillin suspensions.
    """

    def test_two_real_prices_under_one_label_become_two_groups(self, store: NadacStore) -> None:
        groups = store.current_groups("gabapentin")
        assert len(groups) == 2
        assert {g.label() for g in groups} == {"GABAPENTIN 250 MG/5 ML SOLN"}
        prices = sorted(g.per_unit for g in groups)
        assert prices == pytest.approx([0.02011, 0.03746])

    def test_each_package_size_keeps_its_own_ndc_and_count(self, store: NadacStore) -> None:
        groups = store.current_groups("gabapentin")
        cheap = next(g for g in groups if g.per_unit < 0.03)
        assert cheap.example_ndc == "00093500001"
        assert cheap.ndc_count == 1

    def test_group_ids_differ_so_both_are_individually_addressable(self, store: NadacStore) -> None:
        groups = store.current_groups("gabapentin")
        assert groups[0].group_id != groups[1].group_id

    def test_the_split_is_not_tight(self, store: NadacStore) -> None:
        """$0.020 vs $0.037 is an 86% gap - the opposite of noise."""
        groups = store.current_groups("gabapentin")
        assert not nadac.is_tight(groups)


_CSV_HEADER = (
    "NDC Description",
    "NDC",
    "NADAC Per Unit",
    "Effective Date",
    "As of Date",
    "Pricing Unit",
    "OTC",
    "Classification for Rate Setting",
    "Corresponding Generic Drug NADAC Per Unit",
    "Corresponding Generic Drug Effective Date",
)


def _write_csv(path: Path, rows: list[dict[str, str]]) -> None:
    """A CSV shaped exactly like the real NADAC file's columns, for testing
    the actual load path (:func:`nadac._curated_rows`) rather than seeding
    the cache directly - the gap that let brand rows silently never load
    for real (see TestCuratedRowsLoading).
    """
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=_CSV_HEADER)
        writer.writeheader()
        for row in rows:
            full = {col: row.get(col, "") for col in _CSV_HEADER}
            writer.writerow(full)


def _row(
    description: str,
    ndc: str,
    per_unit: str,
    *,
    classification: str = "G",
    generic_per_unit: str = "",
    effective_on: str = "07/22/2026",
    as_of: str = "07/22/2026",
    otc: str = "N",
) -> dict[str, str]:
    return {
        "NDC Description": description,
        "NDC": ndc,
        "NADAC Per Unit": per_unit,
        "Effective Date": effective_on,
        "As of Date": as_of,
        "Pricing Unit": "EA",
        "OTC": otc,
        "Classification for Rate Setting": classification,
        "Corresponding Generic Drug NADAC Per Unit": generic_per_unit,
        "Corresponding Generic Drug Effective Date": effective_on if generic_per_unit else "",
    }


class TestCuratedRowsLoading:
    """The real CSV-load path (:func:`nadac._curated_rows`), not a hand-seeded
    cache - this is the path that let brand rows silently never reach the
    cache for any drug (confirmed live, see nadac.py module docstring)
    despite every query-side test on a hand-seeded cache passing regardless.
    """

    def _load(self, tmp_path: Path, rows: list[dict[str, str]]) -> NadacStore:
        csv_path = tmp_path / "nadac.csv"
        _write_csv(csv_path, rows)
        store = NadacStore(tmp_path / "loaded.sqlite3")
        store._load(csv_path, nadac.Release("http://x", "2026-07-22", "d"))
        return store

    def test_generic_row_loads_under_its_curated_drug_key(self, tmp_path: Path) -> None:
        store = self._load(tmp_path, [_row("ATORVASTATIN 40 MG TABLET", "00093505698", "0.03739")])
        assert [g.per_unit for g in store.current_groups("atorvastatin")] == pytest.approx(
            [0.03739]
        )

    def test_brand_row_loads_under_the_same_curated_drug_key(self, tmp_path: Path) -> None:
        """The actual bug: a real Lipitor row, described by its trade name
        only, must still end up under drug_key='atorvastatin' - confirmed
        live this previously never happened because the loader only matched
        the generic-name prefix.
        """
        store = self._load(
            tmp_path,
            [
                _row("ATORVASTATIN 40 MG TABLET", "00093505698", "0.03739"),
                _row(
                    "LIPITOR 40 MG TABLET",
                    "00071015523",
                    "19.11383",
                    classification="B",
                    generic_per_unit="0.03739",
                ),
            ],
        )
        rows = store.brand_vs_generic("atorvastatin")
        assert len(rows) == 1
        assert rows[0].description == "LIPITOR 40 MG TABLET"
        assert rows[0].generic_per_unit == pytest.approx(0.03739)

    def test_brand_row_is_excluded_from_the_plain_generic_price_list(self, tmp_path: Path) -> None:
        """A brand row reaching the cache must not leak into the regular
        Tier 2/3 list - that list answers "what does my generic cost," not
        "here is also a much more expensive brand you didn't ask about."
        """
        store = self._load(
            tmp_path,
            [
                _row("ATORVASTATIN 40 MG TABLET", "00093505698", "0.03739"),
                _row(
                    "LIPITOR 40 MG TABLET",
                    "00071015523",
                    "19.11383",
                    classification="B",
                    generic_per_unit="0.03739",
                ),
            ],
        )
        groups = store.current_groups("atorvastatin")
        assert {g.base_name for g in groups} == {"ATORVASTATIN"}
        assert store.strengths("atorvastatin") == ["40 MG"]

    def test_combination_product_never_loads_under_either_ingredient(self, tmp_path: Path) -> None:
        store = self._load(
            tmp_path,
            [_row("LISINOPRIL-HYDROCHLOROTHIAZIDE 20-12.5 MG TAB", "00093113601", "0.03677")],
        )
        assert store.current_groups("lisinopril") == []

    def test_non_curated_drug_is_dropped_silently(self, tmp_path: Path) -> None:
        store = self._load(tmp_path, [_row("WARFARIN SODIUM 5 MG TABLET", "00000000001", "0.05")])
        assert store.row_count() == 0

    def test_metoprolol_succinate_er_brand_is_rejected_not_a_wrong_salt_guess(
        self, tmp_path: Path
    ) -> None:
        """Confirmed live: "TOPROL XL" - the real succinate ER brand, a
        different salt than the curated tartrate - is also a strict prefix
        match for the "toprol" brand token. It must not load under
        drug_key='metoprolol' and silently misrepresent itself as that
        drug's brand equivalent.
        """
        store = self._load(
            tmp_path,
            [
                _row("METOPROLOL TARTRATE 50 MG TABLET", "00051123401", "0.01500"),
                _row(
                    "LOPRESSOR 50 MG TABLET",
                    "30698045801",
                    "2.41634",
                    classification="B",
                    generic_per_unit="0.01500",
                ),
                _row(
                    "TOPROL XL 50 MG TABLET",
                    "00186071054",
                    "2.30000",
                    classification="B",
                    generic_per_unit="0.05874",
                ),
            ],
        )
        assert store.row_count() == 2
        brand_descriptions = {g.description for g in store.brand_vs_generic("metoprolol")}
        assert brand_descriptions == {"LOPRESSOR 50 MG TABLET"}

    def test_wellbutrin_sr_loads_safely_bupropion_has_no_salt_split(self, tmp_path: Path) -> None:
        """Unlike metoprolol/carvedilol, bupropion's IR and SR/XL forms are
        confirmed to file under the one same generic name - no
        ``also_priced_as`` is set, so the release-tag guard does not apply
        and a real SR brand row is expected to load normally.
        """
        store = self._load(
            tmp_path,
            [
                _row("BUPROPION HCL SR 150 MG TABLET", "00093721401", "0.05"),
                _row(
                    "WELLBUTRIN SR 150 MG TABLET",
                    "00173071012",
                    "3.00",
                    classification="B",
                    generic_per_unit="0.05",
                ),
            ],
        )
        assert store.brand_vs_generic("bupropion") != []


class TestBrandVsGenericSnapshotDedup:
    """One brand NDC's own price can hold steady for months while its
    weekly-tracked ``corresponding_generic_drug_nadac_per_unit`` companion
    value keeps changing - confirmed live for a real Lopressor NDC, which
    carried seven distinct weekly generic-comparison values all under one
    unchanged ``effective_on``. Grouping on that value, the previous
    approach, turned each weekly snapshot into its own phantom price split.
    """

    def _store_with_weekly_snapshots(self, tmp_path: Path) -> NadacStore:
        store = NadacStore(tmp_path / "snap.sqlite3")
        rows = []
        for as_of, generic in (
            ("2026-06-17", 0.01891),
            ("2026-07-22", 0.01857),
            ("2026-08-05", 0.01857),
        ):
            rows.append(
                (
                    "metoprolol",
                    "LOPRESSOR",
                    "50 MG",
                    "TABLET",
                    "LOPRESSOR 50 MG TABLET",
                    "30698045801",
                    2.41634,
                    "2026-01-21",
                    as_of,
                    "EA",
                    "N",
                    "B",
                    generic,
                )
            )
        with store._conn:
            store._conn.executemany("INSERT INTO prices VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
        return store

    def test_one_row_per_formulation_not_one_per_weekly_snapshot(self, tmp_path: Path) -> None:
        store = self._store_with_weekly_snapshots(tmp_path)
        rows = store.brand_vs_generic("metoprolol")
        assert len(rows) == 1

    def test_the_most_recently_surveyed_generic_value_wins(self, tmp_path: Path) -> None:
        store = self._store_with_weekly_snapshots(tmp_path)
        rows = store.brand_vs_generic("metoprolol")
        assert rows[0].generic_per_unit == pytest.approx(0.01857)
        assert rows[0].as_of == "2026-08-05"


class TestRefresh:
    def test_url_is_resolved_from_metadata_not_templated(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The filename carries a rotating release date and must never be built."""
        payload = {
            "modified": "2026-07-21T12:19:01+00:00",
            "distribution": [
                {
                    "identifier": "907f0776-e5b4-5671-bbb7-7b00be92d116",
                    "data": {
                        "downloadURL": "https://download.medicaid.gov/data/nadac-x-07-22-2026.csv"
                    },
                }
            ],
        }
        monkeypatch.setattr(
            nadac.httpx,
            "get",
            lambda *a, **k: httpx.Response(200, json=payload, request=httpx.Request("GET", "x")),
        )
        release = nadac.resolve_distribution()
        assert release.download_url.endswith("nadac-x-07-22-2026.csv")
        assert release.distribution_id == "907f0776-e5b4-5671-bbb7-7b00be92d116"
        assert release.modified == "2026-07-21T12:19:01+00:00"

    def test_missing_distribution_is_an_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            nadac.httpx,
            "get",
            lambda *a, **k: httpx.Response(
                200, json={"distribution": []}, request=httpx.Request("GET", "x")
            ),
        )
        with pytest.raises(nadac.NadacError):
            nadac.resolve_distribution()

    def test_unchanged_release_skips_the_download(
        self, store: NadacStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Daily polling is cheap because an unchanged timestamp short-circuits."""
        with store._conn:
            store._conn.execute(
                "INSERT OR REPLACE INTO meta VALUES ('modified', '2026-07-21T12:19:01+00:00')"
            )
        monkeypatch.setattr(
            nadac,
            "resolve_distribution",
            lambda **k: nadac.Release("http://never-fetched", "2026-07-21T12:19:01+00:00", "d"),
        )

        def explode(*args: object, **kwargs: object) -> None:
            raise AssertionError("download must not happen when the release is unchanged")

        monkeypatch.setattr(nadac.httpx, "stream", explode)
        reloaded, _ = store.refresh()
        assert reloaded is False
