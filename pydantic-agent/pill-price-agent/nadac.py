"""NADAC access: the scheduled bulk refresh, and the cached price lookups.

CMS publishes NADAC as one CSV per calendar year on data.medicaid.gov (a DKAN
instance, not Socrata). The filename embeds a release date that rotates on CMS's
own cadence, so it is always resolved from the metastore at refresh time and
never hardcoded - see :func:`resolve_distribution`.

The file is ~73 MB / 877k rows and downloads in a few seconds, so the access
pattern is a scheduled download into SQLite, with every conversational lookup
served from that cache. The live datastore query API is not used per user turn.

Two properties of the data drive the schema:

* A year's file holds every weekly release, so one NDC carries many rows keyed
  by ``effective_date``. Current price is the row at the newest effective date;
  the older rows are what the price-trend feature reads.
* For solid oral dosage forms (tablets, capsules), every NDC sharing one
  description and effective date shares an identical price - confirmed live
  across every tablet/capsule row checked. That is *not* true for liquids:
  confirmed live, ``IBUPROFEN 100 MG/5 ML SUSP`` and every curated
  ``.../5 ML SUSP`` amoxicillin strength carry two or three genuinely different
  prices under the identical description at the identical effective date,
  because bottle/package size changes acquisition cost per mL but is not part
  of the strength text NADAC publishes. :class:`PriceGroup` is therefore keyed
  by base name + strength + form + **price** (not just the first three) - see
  :meth:`NadacStore.current_groups` - so a real price split under one label
  becomes two distinct, correctly-priced groups instead of one group silently
  carrying whichever of the two prices SQLite happened to pick.
* ``classification_for_rate_setting`` is ``G`` (generic), ``B`` (brand),
  ``B-ANDA`` or ``B-BIO`` (also brand, confirmed the only four values in the
  live file) - never anything else. The regular price list
  (:meth:`current_groups`, :meth:`strengths`) is generic-only by design, so it
  filters to ``= 'G'`` explicitly rather than relying on brand rows happening
  not to match; :meth:`by_ndc` (Tier 1, an exact NDC off a real bottle) is
  deliberately left unfiltered, since a user's own bottle can just as validly
  be a brand product. :meth:`brand_vs_generic` is the one place brand rows are
  the point, filtered the other way (``LIKE 'B%'``).
"""

from __future__ import annotations

import csv
import os
import re
import sqlite3
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import httpx

from drugs import CURATED, Drug, is_single_ingredient

DATASET_ID = "fbb83258-11c7-47f5-8b18-5f8e79f7e704"
METASTORE_URL = f"https://data.medicaid.gov/api/1/metastore/schemas/dataset/items/{DATASET_ID}"

# NADAC is an acquisition cost: what the pharmacy paid for the drug itself. It
# excludes the professional dispensing fee the pharmacy also bills, which CMS
# surveys separately per state (e.g. Alabama $10.64, Pennsylvania $10.00).
# Those per-state figures are published as a formatted page rather than a clean
# API, so v1 quotes the range instead of pretending to a precise state number.
DISPENSING_FEE_LOW_USD = 10.0
DISPENSING_FEE_HIGH_USD = 13.0

# Below this, two formulations are the same price for practical purposes and
# collapsing them to one number is more useful than showing a noise-level range.
# Above it, the user is being asked to care about a real difference.
TIGHT_SPREAD = 0.10

_DEFAULT_DB = Path(os.getenv("NADAC_DB_PATH", "nadac_cache.sqlite3"))

_SCHEMA = """
CREATE TABLE IF NOT EXISTS prices (
    drug_key      TEXT NOT NULL,
    base_name     TEXT NOT NULL,
    strength      TEXT NOT NULL,
    form          TEXT NOT NULL,
    description   TEXT NOT NULL,
    ndc           TEXT NOT NULL,
    per_unit      REAL NOT NULL,
    effective_on  TEXT NOT NULL,
    as_of         TEXT NOT NULL,
    pricing_unit  TEXT NOT NULL,
    otc           TEXT NOT NULL,
    classification TEXT NOT NULL,
    generic_per_unit REAL
);
CREATE INDEX IF NOT EXISTS ix_lookup ON prices (drug_key, strength, form, effective_on);
CREATE INDEX IF NOT EXISTS ix_ndc ON prices (ndc, effective_on);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""


class NadacError(RuntimeError):
    """A NADAC refresh or lookup could not be completed."""


@dataclass(frozen=True)
class Release:
    """The CSV distribution currently published for the dataset."""

    download_url: str
    modified: str
    distribution_id: str


@dataclass(frozen=True)
class PriceGroup:
    """One purchasable formulation at its current price.

    ``as_of`` is carried per group and always rendered next to the number: NADAC
    carries a price forward for any NDC it didn't re-survey that cycle, so two
    formulations of the same drug routinely quote from months apart, and part of
    an apparent price gap can simply be one side being stale.

    ``per_unit`` is part of the identity, not just a value read off the row: two
    groups can share base name, strength and form and still be genuinely
    different products at genuinely different prices (see the module docstring
    for the confirmed liquid-suspension case), so the price itself has to be
    part of what makes a group distinct rather than an incidental fact about it.
    """

    drug_key: str
    base_name: str
    strength: str
    form: str
    description: str
    per_unit: float
    effective_on: str
    as_of: str
    pricing_unit: str
    ndc_count: int
    example_ndc: str
    generic_per_unit: float | None

    @property
    def group_id(self) -> str:
        return f"{self.base_name}|{self.strength}|{self.form}|{self.per_unit:.5f}"

    def fill_cost(self, quantity: int) -> float:
        """Acquisition cost for ``quantity`` units - not the price of the fill."""
        return self.per_unit * quantity

    def label(self) -> str:
        """Human name for this formulation, e.g. ``METFORMIN ER 1,000 MG OSM-TAB``."""
        return " ".join(part for part in (self.base_name, self.strength, self.form) if part)


_RELEASE_TAG_RE = re.compile(r"\b(ER|XR|XL|SR|CR|LA|DR)\b")
_NON_ORAL_FORM_RE = re.compile(r"\bVIAL\b|\bINJ\b", re.IGNORECASE)


def other_salt_groups(groups: list[PriceGroup]) -> list[PriceGroup]:
    """The subset of ``groups`` whose base_name carries a release-mechanism
    tag - meaningful only for the two curated drugs flagged with
    ``Drug.also_priced_as`` (metoprolol, carvedilol). For those two, this is
    how the *other* salt's own rows leak into this drug's price list at all:
    confirmed live, NADAC describes metoprolol succinate ER as
    ``METOPROLOL SUCC ER ...`` and carvedilol phosphate ER as
    ``CARVEDILOL ER ...`` - both still start with the curated plain-salt
    prefix, so :func:`NadacStore.current_groups` cannot tell them apart from
    a real, same-salt extended-release option (which is exactly what this
    tag correctly identifies for metformin, gabapentin, etc.). Callers use
    this to disclose the mix rather than let a >100x price gap sit
    unexplained next to entries that look like mere formulation choices.
    """
    return [g for g in groups if _RELEASE_TAG_RE.search(g.base_name)]


def formulation_ambiguity(groups: list[PriceGroup]) -> bool:
    """True when one curated drug's own priced formulations mix release
    mechanisms or routes under what a user would call "one drug."

    Confirmed live, two distinct real ways this happens under a single
    openFDA generic name: metformin's oral immediate- and extended-release
    tablets (``METFORMIN HYDROCHLORIDE`` covers both), and pantoprazole's
    oral tablet and IV injection (``PANTOPRAZOLE SODIUM`` covers both too -
    a bigger mismatch than IR/ER if conflated). Checked against NADAC, which
    is already cached locally, rather than against the FDA label the agent
    happens to resolve: a plain immediate-release label almost never uses the
    words "immediate release" - only the special one names itself - so
    detecting ambiguity from the label text alone would miss exactly the case
    where resolution landed on the ordinary side.
    """
    tagged = any(_RELEASE_TAG_RE.search(g.base_name) for g in groups)
    plain = any(not _RELEASE_TAG_RE.search(g.base_name) for g in groups)
    non_oral = any(_NON_ORAL_FORM_RE.search(g.form) for g in groups)
    oral = any(not _NON_ORAL_FORM_RE.search(g.form) for g in groups)
    return (tagged and plain) or (non_oral and oral)


@dataclass(frozen=True)
class PricePoint:
    """One historical observation, for the paid trend feature."""

    effective_on: str
    per_unit: float


def resolve_distribution(*, timeout: float = 30.0) -> Release:
    """Read the current CSV URL from the DKAN metastore.

    Always called before a download. The published filename carries a date
    (``...-07-22-2026.csv``) that changes with CMS's release cadence, so a
    hardcoded URL silently rots into a 404 or, worse, stale prices.
    """
    # show-reference-ids exposes the distribution's own identifier alongside the
    # URL, which is what the refresh logs so a reload can be traced to a release.
    response = httpx.get(
        METASTORE_URL,
        params={"show-reference-ids": "true"},
        timeout=timeout,
        follow_redirects=True,
    )
    response.raise_for_status()
    body: dict[str, Any] = response.json()
    distributions = body.get("distribution") or []
    if not distributions:
        raise NadacError("Dataset metadata contained no distribution to download.")
    first = distributions[0]
    data = first.get("data", first)
    url = data.get("downloadURL")
    if not url:
        raise NadacError("Dataset distribution had no downloadURL.")
    return Release(
        download_url=url,
        modified=str(body.get("modified", "")),
        distribution_id=str(first.get("identifier", "")),
    )


def _iso(us_date: str) -> str:
    """NADAC writes MM/DD/YYYY in the CSV; store ISO so string sort is date sort."""
    text = us_date.strip()
    try:
        if "/" in text:
            month, day, year = (int(part) for part in text.split("/"))
            return date(year, month, day).isoformat()
        return date.fromisoformat(text).isoformat()
    except ValueError:
        return text


_STRENGTH_IN_DESC = re.compile(
    r"^(?P<name>.*?)\s*"
    r"(?P<strength>\d[\d,]*(?:\.\d+)?\s*(?:MCG|MG|ML|GM|G|UNIT|%)"
    r"(?:\s*/\s*[\d,.]*\s*(?:MCG|MG|ML|G))?)\s*"
    r"(?P<form>.*)$"
)


def split_description(description: str) -> tuple[str, str, str]:
    """Split ``METFORMIN ER 1,000 MG GASTR-TB`` into name, strength, form.

    The name half keeps qualifiers like ``ER`` and ``HCL`` because they are
    genuine product distinctions (extended vs immediate release), not noise.
    """
    upper = description.upper().strip()
    match = _STRENGTH_IN_DESC.match(upper)
    if not match:
        return upper, "", ""
    return (
        match.group("name").strip(),
        re.sub(r"\s+", " ", match.group("strength")).strip(),
        match.group("form").strip(),
    )


def _brand_match_is_safe(description: str, brand_prefix: str, drug: Drug) -> bool:
    """Reject a brand-name match that is actually a *different* salt's own brand.

    Confirmed live in the current file: metoprolol's brand "lopressor" is
    safe (only ever prefixes plain ``LOPRESSOR ...`` tartrate/IR rows), but
    its other brand "toprol" is also a strict prefix of ``TOPROL XL ...`` -
    the real product name of the succinate extended-release brand, a
    genuinely different salt this drug's own curated ``fda_generic_name``
    does not cover (see ``also_priced_as`` in drugs.py) - not a same-salt
    variant the way e.g. ``WELLBUTRIN SR`` legitimately is for bupropion
    (confirmed both IR and SR bupropion file under one ``BUPROPION
    HYDROCHLORIDE`` generic name, so no such rejection applies there). Only
    drugs already flagged with ``also_priced_as`` carry this specific risk,
    so the check is scoped to them: reject when the word right after the
    matched brand prefix is itself a release-mechanism tag, since that is
    exactly the shape of a same-brand-stem, different-salt product name.
    """
    if drug.also_priced_as is None:
        return True
    remainder = description[len(brand_prefix) :].strip()
    next_word = remainder.split(" ", 1)[0] if remainder else ""
    return not _RELEASE_TAG_RE.fullmatch(next_word)


def _curated_rows(csv_path: Path) -> Iterator[tuple[Any, ...]]:
    """Yield DB rows for curated single-ingredient products, generic or brand.

    Filtering at load time is what keeps the cache small (~130k of 877k rows)
    and means every later query is a plain indexed lookup.

    A row matches on the curated generic name (``nadac_prefix``) or, failing
    that, on one of the drug's own brand names - confirmed live this second
    path is not optional: real brand rows for curated drugs (Lipitor,
    Synthroid, Xanax, Crestor and 15 others, confirmed present in the current
    file) are described by their trade name, never the generic name, so they
    never match ``nadac_prefix`` at all. Skipping this path leaves
    :meth:`NadacStore.brand_vs_generic` structurally unable to find a single
    row for any drug, ever, regardless of what CMS actually published.
    """
    prefixes: list[tuple[str, Drug]] = sorted(
        ((d.nadac_prefix, d) for d in CURATED), key=lambda p: -len(p[0])
    )
    brand_prefixes: list[tuple[str, Drug]] = sorted(
        ((brand.upper(), d) for d in CURATED for brand in d.brands),
        key=lambda p: -len(p[0]),
    )
    with csv_path.open(newline="", encoding="utf-8-sig") as handle:
        for row in csv.DictReader(handle):
            description = row["NDC Description"].upper()
            drug = next(
                (d for prefix, d in prefixes if is_single_ingredient(description, prefix)),
                None,
            )
            if drug is None:
                drug = next(
                    (
                        d
                        for prefix, d in brand_prefixes
                        if is_single_ingredient(description, prefix)
                        and _brand_match_is_safe(description, prefix, d)
                    ),
                    None,
                )
            if drug is None:
                continue
            try:
                per_unit = float(row["NADAC Per Unit"])
            except (TypeError, ValueError):
                continue
            generic_raw = (row.get("Corresponding Generic Drug NADAC Per Unit") or "").strip()
            base_name, strength, form = split_description(description)
            yield (
                drug.key,
                base_name,
                strength,
                form,
                description,
                row["NDC"].strip(),
                per_unit,
                _iso(row["Effective Date"]),
                _iso(row["As of Date"]),
                row["Pricing Unit"].strip(),
                row["OTC"].strip(),
                row["Classification for Rate Setting"].strip(),
                float(generic_raw) if generic_raw else None,
            )


class NadacStore:
    """SQLite-backed NADAC cache: refreshed on a schedule, read per turn."""

    def __init__(self, db_path: Path | str = _DEFAULT_DB) -> None:
        self.db_path = Path(db_path)
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)

    def close(self) -> None:
        self._conn.close()

    # -- refresh ---------------------------------------------------------

    def loaded_release(self) -> str | None:
        row = self._conn.execute("SELECT value FROM meta WHERE key = 'modified'").fetchone()
        return row["value"] if row else None

    def row_count(self) -> int:
        return int(self._conn.execute("SELECT COUNT(*) AS n FROM prices").fetchone()["n"])

    def refresh(self, *, force: bool = False, timeout: float = 300.0) -> tuple[bool, Release]:
        """Resolve, download and load the current CSV.

        Returns ``(reloaded, release)``. When the metastore's ``modified``
        timestamp matches what is already cached, the download is skipped, so
        running this more often than CMS publishes is cheap.
        """
        release = resolve_distribution()
        if not force and release.modified and release.modified == self.loaded_release():
            return False, release

        with tempfile.NamedTemporaryFile(suffix=".csv", delete=False) as tmp:
            tmp_path = Path(tmp.name)
        try:
            with httpx.stream(
                "GET", release.download_url, timeout=timeout, follow_redirects=True
            ) as response:
                response.raise_for_status()
                with tmp_path.open("wb") as out:
                    for chunk in response.iter_bytes(1 << 20):
                        out.write(chunk)
            self._load(tmp_path, release)
        finally:
            tmp_path.unlink(missing_ok=True)
        return True, release

    def _load(self, csv_path: Path, release: Release) -> None:
        with self._conn:
            self._conn.execute("DELETE FROM prices")
            self._conn.executemany(
                "INSERT INTO prices VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                _curated_rows(csv_path),
            )
            self._conn.execute(
                "INSERT OR REPLACE INTO meta VALUES ('modified', ?)", (release.modified,)
            )
            self._conn.execute(
                "INSERT OR REPLACE INTO meta VALUES ('source_url', ?)", (release.download_url,)
            )
            self._conn.execute(
                "INSERT OR REPLACE INTO meta VALUES ('loaded_at', ?)",
                (datetime.now(UTC).date().isoformat(),),
            )

    # -- lookups ---------------------------------------------------------

    def _group_from_row(self, row: sqlite3.Row) -> PriceGroup:
        return PriceGroup(
            drug_key=row["drug_key"],
            base_name=row["base_name"],
            strength=row["strength"],
            form=row["form"],
            description=row["description"],
            per_unit=float(row["per_unit"]),
            effective_on=row["effective_on"],
            as_of=row["as_of"],
            pricing_unit=row["pricing_unit"],
            ndc_count=int(row["ndc_count"]),
            example_ndc=row["example_ndc"],
            generic_per_unit=row["generic_per_unit"],
        )

    # "newest" fixes one effective date per (base_name, strength, form) - the
    # normal, current-price case. "at_newest" is every row on that date. The
    # final GROUP BY then also splits on per_unit: almost always a no-op,
    # because almost every formulation is one price, but it is what stops a
    # genuine liquid-suspension price split (see module docstring) from being
    # collapsed into one group with an arbitrary one of the two real prices -
    # SQLite does not define which row a bare, non-aggregated column like
    # per_unit comes from when GROUP BY alone can't already guarantee it is
    # the same in every row of the group.
    #
    # ``classification = 'G'`` keeps this generic-only: since _curated_rows
    # now also loads brand rows (for brand_vs_generic), a plain "how much is
    # my atorvastatin" would otherwise list a Lipitor row alongside the
    # generic ones here, at a genuinely different price, under the tier logic
    # built for "different manufacturer of the same generic," not "different
    # product entirely."
    _CURRENT_SQL = """
        WITH newest AS (
            SELECT base_name, strength, form, MAX(effective_on) AS newest
            FROM prices
            WHERE drug_key = ? AND classification = 'G' {inner_strength}
            GROUP BY base_name, strength, form
        ),
        at_newest AS (
            SELECT p.*
            FROM prices p
            JOIN newest n
              ON n.base_name = p.base_name AND n.strength = p.strength
             AND n.form = p.form AND n.newest = p.effective_on
            WHERE p.drug_key = ? AND p.classification = 'G' {outer_strength}
        )
        SELECT base_name, strength, form, per_unit, effective_on, drug_key,
               MIN(description) AS description,
               MAX(as_of) AS as_of,
               MIN(pricing_unit) AS pricing_unit,
               MAX(generic_per_unit) AS generic_per_unit,
               COUNT(DISTINCT ndc) AS ndc_count,
               MIN(ndc) AS example_ndc
        FROM at_newest
        GROUP BY base_name, strength, form, per_unit
        ORDER BY per_unit
    """

    def current_groups(self, drug_key: str, strength: str | None = None) -> list[PriceGroup]:
        """Every current formulation of ``drug_key``, optionally at one strength.

        This is the shared engine behind Tier 2 (name + strength) and Tier 3
        (name only) - the tiers differ only in whether a strength is supplied
        and in how the result is framed to the user.
        """
        sql = self._CURRENT_SQL.format(
            inner_strength="AND strength = ?" if strength else "",
            outer_strength="AND p.strength = ?" if strength else "",
        )
        params: list[Any] = [drug_key]
        if strength:
            params.append(strength)
        params.append(drug_key)
        if strength:
            params.append(strength)
        rows = self._conn.execute(sql, params).fetchall()
        return [self._group_from_row(row) for row in rows]

    def by_ndc(self, ndc: str) -> PriceGroup | None:
        """Tier 1: the exact product on the bottle. No ambiguity to resolve."""
        row = self._conn.execute(
            """
            SELECT *, 1 AS ndc_count, ndc AS example_ndc
            FROM prices WHERE ndc = ?
            ORDER BY effective_on DESC LIMIT 1
            """,
            (ndc,),
        ).fetchone()
        return self._group_from_row(row) if row else None

    def strengths(self, drug_key: str) -> list[str]:
        """Generic-only, to match ``current_groups`` - a brand-only strength
        named here would read as "I do have this strength" when what's
        actually priced under it is a different, brand-only product."""
        rows = self._conn.execute(
            "SELECT DISTINCT strength FROM prices WHERE drug_key = ? AND classification = 'G' "
            "ORDER BY strength",
            (drug_key,),
        ).fetchall()
        return [r["strength"] for r in rows]

    def history(self, ndc: str) -> list[PricePoint]:
        """The price-trend feature: one real product's own price over the cached year.

        Keyed on the exact NDC the user was shown (``PriceGroup.example_ndc``),
        not on base name/strength/form. Averaging across every NDC under a
        label was the previous approach and is wrong whenever that label
        covers more than one real price - confirmed live for liquid
        suspensions (see module docstring) - because it blends two real
        products into a number that matches neither. A single NDC has exactly
        one price at each effective date, so there is nothing to average -
        ``GROUP BY`` here only collapses the CMS file's own repeat rows for
        one NDC/effective date carried across several as-of snapshots, and
        is safe as a bare column because they are the same price by definition.
        """
        rows = self._conn.execute(
            """
            SELECT effective_on, per_unit FROM prices
            WHERE ndc = ? GROUP BY effective_on ORDER BY effective_on
            """,
            (ndc,),
        ).fetchall()
        return [PricePoint(r["effective_on"], float(r["per_unit"])) for r in rows]

    def brand_vs_generic(self, drug_key: str) -> list[PriceGroup]:
        """Brand-classified rows for this drug that also carry a generic price.

        Two independent reasons this can come back empty, both real and
        distinct from an error: the drug's brand may simply not be in the
        current file at all (confirmed live: no Advil/Motrin, no Glucophage,
        no Coreg row exists in the file checked), or CMS's own
        ``corresponding_generic_drug_nadac_per_unit`` field may be blank on
        the brand row it does have (confirmed live: ~43% of all brand rows in
        the file are missing it, though the curated brand names specifically
        - Lipitor, Xanax, Crestor and the rest - had it populated on all but a
        handful when checked). Either way, callers must handle an empty
        result as "no priced generic equivalent," not an error.
        """
        # A brand NDC's own price can hold steady for months (one fixed
        # ``effective_on``) while ``corresponding_generic_drug_nadac_per_unit``
        # - a separate, independently live-tracked companion field - keeps
        # being re-surveyed every week regardless, confirmed live: one real
        # Lopressor NDC carried seven genuinely different weekly generic
        # comparison values, all sharing that same single ``effective_on``.
        # Grouping by ``generic_per_unit`` (the previous approach) treated
        # every one of those seven weekly snapshots as its own distinct price
        # split - the same kind of quiet wrongness the liquid-suspension fix
        # was for - so the correlated subquery picks only the single
        # most-recently-surveyed (``MAX(as_of)``) value per formulation.
        rows = self._conn.execute(
            """
            WITH newest AS (
                SELECT base_name, strength, form, MAX(effective_on) AS newest
                FROM prices
                WHERE drug_key = ? AND classification LIKE 'B%' AND generic_per_unit IS NOT NULL
                GROUP BY base_name, strength, form
            ),
            at_newest AS (
                SELECT p.*
                FROM prices p
                JOIN newest n ON n.base_name = p.base_name AND n.strength = p.strength
                              AND n.form = p.form AND n.newest = p.effective_on
                WHERE p.drug_key = ? AND p.classification LIKE 'B%'
                  AND p.generic_per_unit IS NOT NULL
            )
            SELECT base_name, strength, form, per_unit, effective_on, drug_key,
                   MIN(description) AS description,
                   MAX(as_of) AS as_of,
                   MIN(pricing_unit) AS pricing_unit,
                   COUNT(DISTINCT ndc) AS ndc_count,
                   MIN(ndc) AS example_ndc,
                   (
                       SELECT a2.generic_per_unit FROM at_newest a2
                       WHERE a2.base_name = at_newest.base_name AND a2.strength = at_newest.strength
                         AND a2.form = at_newest.form AND a2.per_unit = at_newest.per_unit
                       ORDER BY a2.as_of DESC LIMIT 1
                   ) AS generic_per_unit
            FROM at_newest
            GROUP BY base_name, strength, form, per_unit
            ORDER BY per_unit DESC
            """,
            (drug_key, drug_key),
        ).fetchall()
        return [self._group_from_row(row) for row in rows]


def spread(groups: list[PriceGroup]) -> float:
    """Relative gap between the cheapest and dearest formulation, 0.0 when one."""
    if len(groups) < 2:
        return 0.0
    prices = [g.per_unit for g in groups]
    low = min(prices)
    return (max(prices) - low) / low if low > 0 else 0.0


def is_tight(groups: list[PriceGroup]) -> bool:
    """True when the formulations agree closely enough to quote one number.

    The threshold exists because divergence is the norm, not the exception: a
    clustering run over the July 2026 file found every one of the 30 curated
    drugs has at least one strength whose formulations disagree by more than 5%.
    Without a floor, almost every answer would be a range, including ones where
    the range is meaningless noise (atorvastatin 40 MG varies 13% between
    manufacturers of the identical tablet).
    """
    return spread(groups) <= TIGHT_SPREAD


def total_range(groups: list[PriceGroup], quantity: int) -> tuple[float, float]:
    """Low and high all-in estimate: acquisition cost plus the dispensing fee.

    Never return acquisition cost alone. NADAC excludes the dispensing fee by
    design, so quoting the bare floor makes every honest pharmacy look like it
    is overcharging.
    """
    costs = [g.fill_cost(quantity) for g in groups]
    return min(costs) + DISPENSING_FEE_LOW_USD, max(costs) + DISPENSING_FEE_HIGH_USD
