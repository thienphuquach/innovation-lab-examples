"""The curated drug list, and the name handling that keeps lookups honest.

Two rules encoded here, both from live NADAC data rather than documentation:

1. Drug-name search is *starts-with*, never *contains*. ``%METFORMIN%`` returns
   ``GLIPIZIDE-METFORMIN`` and ``ALOGLIPTIN-METFORMIN`` ahead of the real thing,
   because combination products bury the ingredient mid-string.
2. Starts-with alone is not enough. It only isolates single-ingredient products
   when the drug happens to sort first in every combo it appears in. Metformin
   does; amlodipine emphatically does not - ``AMLODIPINE%`` matches 33 distinct
   descriptions in the July 2026 file, 30 of which are ``AMLODIPINE-ATORVAST``,
   ``AMLODIPINE-BENAZEPRIL`` and friends. :func:`is_single_ingredient` rejects
   the ``DRUG-`` prefix, which brings that 33 back down to the correct 3.

Combination products are out of scope for v1 (see README), so the filter is a
hard exclusion, not a ranking preference.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Drug:
    """One curated generic.

    ``nadac_prefix`` is matched against ``ndc_description`` with starts-with;
    ``fda_generic_name`` is the exact value openFDA indexes under
    ``openfda.generic_name.exact`` (often the salt form, e.g. metformin is
    filed as ``METFORMIN HYDROCHLORIDE``).
    """

    key: str
    display: str
    nadac_prefix: str
    fda_generic_name: str
    brands: tuple[str, ...] = field(default=())

    # Set only for the two curated drugs (confirmed live) where NADAC prices an
    # extended-release product under a genuinely different salt name than the
    # one this drug's own fda_generic_name uses - metoprolol succinate ER vs the
    # curated tartrate IR, carvedilol phosphate ER vs the curated plain
    # carvedilol. openFDA's exact-match query can never return that other
    # product at all (it is filed under a different generic_name entirely), so
    # the FDA info feature silently only ever answers about the salt named
    # here - a coverage gap, not a resolution ambiguity, and the two need
    # different disclosures (see openfda.py and nadac.formulation_ambiguity).
    # A short noun phrase naming the other product - see chat_proto.py's
    # drug_info_card for how it's woven into a full sentence on the card.
    also_priced_as: str | None = None


CURATED: tuple[Drug, ...] = (
    Drug("metformin", "Metformin", "METFORMIN", "METFORMIN HYDROCHLORIDE", ("glucophage",)),
    Drug("lisinopril", "Lisinopril", "LISINOPRIL", "LISINOPRIL", ("prinivil", "zestril")),
    Drug("atorvastatin", "Atorvastatin", "ATORVASTATIN", "ATORVASTATIN CALCIUM", ("lipitor",)),
    Drug(
        "levothyroxine",
        "Levothyroxine",
        "LEVOTHYROXINE",
        "LEVOTHYROXINE SODIUM",
        ("synthroid", "levoxyl", "unithroid", "euthyrox"),
    ),
    Drug("amlodipine", "Amlodipine", "AMLODIPINE", "AMLODIPINE BESYLATE", ("norvasc",)),
    Drug("omeprazole", "Omeprazole", "OMEPRAZOLE", "OMEPRAZOLE", ("prilosec",)),
    Drug("sertraline", "Sertraline", "SERTRALINE", "SERTRALINE HYDROCHLORIDE", ("zoloft",)),
    Drug("gabapentin", "Gabapentin", "GABAPENTIN", "GABAPENTIN", ("neurontin",)),
    Drug("losartan", "Losartan", "LOSARTAN", "LOSARTAN POTASSIUM", ("cozaar",)),
    Drug("simvastatin", "Simvastatin", "SIMVASTATIN", "SIMVASTATIN", ("zocor",)),
    Drug(
        "hydrochlorothiazide",
        "Hydrochlorothiazide",
        "HYDROCHLOROTHIAZIDE",
        "HYDROCHLOROTHIAZIDE",
        ("microzide", "hctz"),
    ),
    Drug("pantoprazole", "Pantoprazole", "PANTOPRAZOLE", "PANTOPRAZOLE SODIUM", ("protonix",)),
    Drug("montelukast", "Montelukast", "MONTELUKAST", "MONTELUKAST SODIUM", ("singulair",)),
    Drug("escitalopram", "Escitalopram", "ESCITALOPRAM", "ESCITALOPRAM OXALATE", ("lexapro",)),
    Drug("rosuvastatin", "Rosuvastatin", "ROSUVASTATIN", "ROSUVASTATIN CALCIUM", ("crestor",)),
    Drug(
        "bupropion",
        "Bupropion",
        "BUPROPION",
        "BUPROPION HYDROCHLORIDE",
        ("wellbutrin", "zyban"),
    ),
    Drug("prednisone", "Prednisone", "PREDNISONE", "PREDNISONE", ("deltasone", "rayos")),
    Drug("fluoxetine", "Fluoxetine", "FLUOXETINE", "FLUOXETINE HYDROCHLORIDE", ("prozac",)),
    Drug(
        "trazodone",
        "Trazodone",
        "TRAZODONE",
        "TRAZODONE HYDROCHLORIDE",
        ("desyrel", "oleptro"),
    ),
    Drug("tamsulosin", "Tamsulosin", "TAMSULOSIN", "TAMSULOSIN HYDROCHLORIDE", ("flomax",)),
    Drug(
        "carvedilol",
        "Carvedilol",
        "CARVEDILOL",
        "CARVEDILOL",
        ("coreg",),
        also_priced_as="extended-release carvedilol phosphate (brand Coreg CR)",
    ),
    Drug(
        "metoprolol",
        "Metoprolol",
        "METOPROLOL",
        "METOPROLOL TARTRATE",
        ("lopressor", "toprol", "toprol-xl"),
        also_priced_as="extended-release metoprolol succinate (brand Toprol-XL)",
    ),
    Drug("furosemide", "Furosemide", "FUROSEMIDE", "FUROSEMIDE", ("lasix",)),
    Drug(
        "citalopram",
        "Citalopram",
        "CITALOPRAM",
        "CITALOPRAM HYDROBROMIDE",
        ("celexa",),
    ),
    Drug("ibuprofen", "Ibuprofen", "IBUPROFEN", "IBUPROFEN", ("motrin", "advil")),
    Drug("amoxicillin", "Amoxicillin", "AMOXICILLIN", "AMOXICILLIN", ("amoxil",)),
    Drug("alprazolam", "Alprazolam", "ALPRAZOLAM", "ALPRAZOLAM", ("xanax",)),
    Drug(
        "duloxetine",
        "Duloxetine",
        "DULOXETINE",
        "DULOXETINE HYDROCHLORIDE",
        ("cymbalta",),
    ),
    Drug(
        "clopidogrel",
        "Clopidogrel",
        "CLOPIDOGREL",
        "CLOPIDOGREL BISULFATE",
        ("plavix",),
    ),
    Drug("allopurinol", "Allopurinol", "ALLOPURINOL", "ALLOPURINOL", ("zyloprim",)),
)

BY_KEY: dict[str, Drug] = {d.key: d for d in CURATED}
_BY_BRAND: dict[str, Drug] = {b: d for d in CURATED for b in d.brands}


_DUAL_STRENGTH = re.compile(
    r"\d[\d,.]*\s*-\s*\d[\d,.]*\s*(?:MCG|MG|ML|GM|G|UNIT|%)\b", re.IGNORECASE
)
_HAS_STRENGTH = re.compile(r"\d[\d,]*(?:\.\d+)?\s*(?:MCG|MG|ML|GM|G|UNIT|%)\b", re.IGNORECASE)


def is_single_ingredient(ndc_description: str, nadac_prefix: str) -> bool:
    """True when ``ndc_description`` is the plain drug, not a combination product.

    Three tells, all confirmed against live NADAC data rather than assumed:

    1. The obvious case: a combination description hyphenates the ingredients
       together in the leading token run (``LISINOPRIL-HYDROCHLOROTHIAZIDE
       20-12.5 MG TAB``) - rejected by refusing the ``DRUG-`` prefix.
    2. Some OTC combinations instead space-separate the second ingredient and
       only reveal themselves in the strength, which co-lists both doses with
       a hyphen (``IBUPROFEN PM 200-38 MG CAPLET`` is ibuprofen 200 mg plus
       diphenhydramine 38 mg, confirmed live) - rejected by refusing any
       dual-number strength, wherever in the description it falls.
    3. A few multi-symptom OTC names carry no strength at all (``IBUPROFEN
       COLD-SINUS CPLT``, confirmed live as ibuprofen plus a decongestant;
       plain ``IBUPROFEN PM CAPLET``), because NADAC doesn't reduce a compound
       formulation to one number. Every genuine single-ingredient row in the
       curated list has one, so a missing strength is rejected too rather than
       build a price group with no defensible per-unit number behind it.
    """
    upper = ndc_description.upper()
    if not upper.startswith(nadac_prefix) or upper.startswith(f"{nadac_prefix}-"):
        return False
    if _DUAL_STRENGTH.search(upper):
        return False
    return bool(_HAS_STRENGTH.search(upper))


def resolve(text: str) -> Drug | None:
    """Resolve free text to a curated drug, by generic name or by brand.

    Deliberately not fuzzy: a partial or misremembered name returns ``None`` so
    the caller can ask, rather than silently answering about a different drug.
    """
    cleaned = re.sub(r"[^a-z0-9 \-]", " ", text.lower())
    tokens = [t for t in cleaned.split() if t]
    for token in tokens:
        if token in BY_KEY:
            return BY_KEY[token]
        if token in _BY_BRAND:
            return _BY_BRAND[token]
    return None


_STRENGTH_RE = re.compile(
    r"(\d[\d,]*(?:\.\d+)?)\s*(mcg|mg|ml|gm|g|unit|%)(?:\s*/\s*(\d[\d,]*(?:\.\d+)?)\s*(mcg|mg|ml|g))?",
    re.IGNORECASE,
)


def parse_strength(text: str) -> str | None:
    """Pull a dose strength out of free text, normalized the way NADAC writes it.

    NADAC uses a comma thousands separator (``1,000 MG``) and a space before the
    unit, so ``1000mg`` and ``1,000 mg`` both normalize to ``1,000 MG`` and match.
    """
    match = _STRENGTH_RE.search(text)
    if not match:
        return None
    value = f"{float(match.group(1).replace(',', '')):,g}"
    unit = match.group(2).upper()
    if match.group(3):
        denom = f"{float(match.group(3).replace(',', '')):,g}"
        return f"{value} {unit}/{denom} {match.group(4).upper()}"
    return f"{value} {unit}"


_NDC_RE = re.compile(r"\b(\d{4,5})[- ](\d{3,4})[- ](\d{1,2})\b|\b(\d{11})\b")


def parse_ndc(text: str) -> str | None:
    """Extract an NDC and normalize it to NADAC's 11-digit, no-dash form.

    Packaging and the FDA's own NDC directory print one of three 10-digit
    segment layouts - 4-4-2, 5-3-2, or 5-4-1 - not the 11-digit form NADAC
    stores (e.g. Advil's real NDC is printed ``0573-0154-60``, 10 digits).
    Zero-padding the labeler segment to 5, the product segment to 4, and the
    package segment to 2 normalizes any of those three layouts - and leaves an
    already-11-digit, dash-separated NDC unchanged - the same way the HIPAA
    11-digit standard does. A bare 10-digit run with no separators is refused
    rather than guessed, since which segment is short can't be told without
    the dashes marking where the three parts split.
    """
    match = _NDC_RE.search(text)
    if not match:
        return None
    if match.group(4):
        return match.group(4)
    labeler, product, package = match.group(1), match.group(2), match.group(3)
    return f"{labeler.zfill(5)}{product.zfill(4)}{package.zfill(2)}"
