"""openFDA access: label text for drug info, and the active-shortage cross-check.

Live per request - openFDA is built for that and needs no caching layer. A free
key raises the daily ceiling from 1,000 requests per IP to 120,000 per key; the
per-minute limit is 240 either way.

Three behaviours here are load-bearing:

* Searches use ``openfda.generic_name.exact``. A loose match on "metformin"
  returns Zituvimet - a sitagliptin/metformin combination with an entirely
  different boxed-warning profile - mixed in among true single-ingredient
  results, which is exactly the kind of quiet wrongness this agent must not ship.
* One generic name covers many real, distinct products (different
  manufacturers, and for some drugs different release mechanisms entirely -
  ``METFORMIN HYDROCHLORIDE`` alone covers both the immediate-release and
  extended-release tablet). Every section quoted for one drug-info turn is
  therefore read from exactly one chosen document (:func:`resolve_label_document`),
  never from an independent per-field query - confirmed live: querying
  ``boxed_warning`` and ``drug_interactions`` for "METFORMIN HYDROCHLORIDE"
  as two separate calls can each rank a different manufacturer's product top,
  producing a card whose boxed warning names the extended-release product
  while its interactions table ("Table 3") is verbatim from a different,
  immediate-release-only product that never printed that warning at all. Every
  sentence would still be real FDA text; the single coherent document they are
  presented as belonging to would not exist. Pinning to one document's
  ``set_id`` for the whole turn - and persisting it in ``SessionState`` so a
  later "show more detail" reuses the same product - is what closes that gap.
* Nothing on this module generates, summarises or paraphrases medical content.
  :func:`readable` normalises whitespace and paragraph breaks and does nothing
  else. If the label does not say it, the agent does not say it.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Any

import httpx

LABEL_URL = "https://api.fda.gov/drug/label.json"
SHORTAGE_URL = "https://api.fda.gov/drug/shortages.json"

# Ordered fallback for the patient-facing answer. `spl_medguide` is the FDA's own
# plain-language Medication Guide and is the best text available - but Medication
# Guides are only required for drugs carrying particular serious risks, so the
# field is simply absent for most ordinary generics. Measured against the live
# API: gabapentin has one on 391 of 405 labels, while lisinopril, atorvastatin,
# levothyroxine and simvastatin have one on *zero*. Falling back keeps those
# drugs answerable with real label text instead of returning nothing.
PATIENT_FIELDS: tuple[tuple[str, str], ...] = (
    ("spl_medguide", "FDA Medication Guide"),
    ("information_for_patients", "FDA label - Patient Counseling Information"),
    ("indications_and_usage", "FDA label - Indications and Usage"),
)

# Clinician-depth sections, shown only when the user explicitly asks for more.
DETAIL_FIELDS: tuple[tuple[str, str], ...] = (
    ("dosage_and_administration", "Dosage and administration"),
    ("boxed_warning", "Boxed warning"),
    ("warnings_and_cautions", "Warnings and cautions"),
    ("drug_interactions", "Drug interactions"),
    ("adverse_reactions", "Adverse reactions"),
)

# Real, structural release-mechanism tokens FDA labels use in their own words
# (never invented wording - just recognised in text already being quoted
# verbatim). Confirmed live across the curated list: metformin, gabapentin,
# bupropion, fluoxetine and alprazolam each have one true single-ingredient
# generic name spanning both an immediate-release and one of these.
_RELEASE_RE = re.compile(
    r"\b(extended[- ]release|delayed[- ]release|sustained[- ]release|controlled[- ]release)\b",
    re.IGNORECASE,
)


class OpenFdaError(RuntimeError):
    """openFDA could not be reached, or returned an unusable response."""


@dataclass(frozen=True)
class LabelText:
    """One section of label text, tagged with the field and the exact product
    it actually came from.

    ``source`` is surfaced in the UI rather than hidden: a Medication Guide and a
    Patient Counseling Information section are both FDA text but read very
    differently, and the user is entitled to know which one they are looking at.
    ``set_id``, ``product_ndc`` and ``manufacturer`` identify the one specific
    labeled product this text is verbatim from - never blended with any other
    product's version of a different section, and shown in the card so a user
    whose own bottle differs (a different manufacturer, or immediate- versus
    extended-release) knows this describes a real but possibly different product.
    """

    field: str
    source: str
    text: str
    set_id: str
    product_ndc: str
    manufacturer: str

    # Set only on the ``dosage_and_administration`` section, and only when the
    # caller asked for it (``disclose=True`` on fetch_label/fetch_detail_sections) -
    # deliberately placed on the one section it actually qualifies rather than
    # as a generic footnote elsewhere on the card. See _formulation_note.
    formulation_note: str | None = None


@dataclass(frozen=True)
class Shortage:
    """An FDA drug-shortage record with status ``Current``."""

    generic_name: str
    status: str
    availability: str
    dosage_form: str
    presentation: str
    company_name: str
    updated_on: str


def _params(extra: dict[str, Any]) -> dict[str, Any]:
    key = (os.getenv("OPENFDA_API_KEY") or "").strip()
    return {**extra, "api_key": key} if key else extra


async def _get(client: httpx.AsyncClient, url: str, params: dict[str, Any]) -> dict[str, Any]:
    """GET one openFDA endpoint, mapping "no matches" to an empty result.

    openFDA answers an empty result set with HTTP 404 and an error body rather
    than an empty ``results`` list, so a drug with no shortage looks like a
    failure unless NOT_FOUND is normalised here.
    """
    response = await client.get(url, params=_params(params))
    if response.status_code == 404:
        return {"results": []}
    if response.status_code >= 400:
        raise OpenFdaError(f"openFDA {response.status_code} for {url}")
    body: dict[str, Any] = response.json()
    if "error" in body:
        if body["error"].get("code") == "NOT_FOUND":
            return {"results": []}
        raise OpenFdaError(str(body["error"]))
    return body


_WS = re.compile(r"[ \t]+")
_LEADING_SECTION_NUMBER = re.compile(r"^\s*\d+(\.\d+)*\s+")
_SENTENCE_SPLIT = re.compile(r"(?<=[.:])\s+(?=[A-Z])")
_BULLET_SPLIT = re.compile(r"\s*\u2022\s*")


def _normalize(raw: str, *, limit: int | None) -> str:
    text = _LEADING_SECTION_NUMBER.sub("", _WS.sub(" ", raw.replace("\n", " "))).strip()
    if limit and len(text) > limit:
        cut = text.rfind(". ", 0, limit)
        text = text[: cut + 1] if cut > limit // 2 else text[:limit].rstrip() + "..."
    return text


def readable(raw: str, *, limit: int | None = None) -> str:
    """Make label text legible without changing a word of it.

    Collapses runs of whitespace, drops the leading SPL section number, and
    breaks the blob into paragraphs at sentence ends. Purely typographic: no
    summarising, no rewording, no reordering.
    """
    return _SENTENCE_SPLIT.sub("\n\n", _normalize(raw, limit=limit))


def paragraphs(raw: str, *, limit: int | None = None) -> list[str]:
    """Same normalisation as :func:`readable`, as a list of renderable blocks.

    A card is built from discrete elements, not a single string a chat bubble
    would wrap - embedding "\\n\\n" in one block, as ``readable`` does, renders
    as one undifferentiated run of text in the drawer. This splits at the same
    sentence boundaries plus the SPL label's own "\u2022" bullet markers, so
    each list item becomes its own element instead of disappearing into a wall
    of text. Still no word changed, no section reordered.
    """
    blocks: list[str] = []
    for chunk in _SENTENCE_SPLIT.split(_normalize(raw, limit=limit)):
        if "\u2022" not in chunk:
            blocks.append(chunk)
            continue
        head, *bullets = _BULLET_SPLIT.split(chunk)
        if head.strip():
            blocks.append(head.strip())
        for bullet in bullets:
            bullet = bullet.strip()
            if not bullet:
                continue
            sentences = _SENTENCE_SPLIT.split(bullet)
            blocks.append(f"\u2022 {sentences[0]}")
            blocks.extend(sentences[1:])
    return [b for b in blocks if b.strip()]


def _product_ndc_candidates(ndc: str) -> list[str]:
    """The 2-3 ``labeler-product`` reconstructions an 11-digit NADAC NDC could
    have come from - the mirror image of :func:`drugs.parse_ndc`'s padding.

    The 11-digit HIPAA form is always labeler(5) + product(4) + package(2),
    but that is a normalization NADAC/HIPAA applies on top of one of three
    original printed widths (4-4-2, 5-3-2, 5-4-1), and which one was padded
    cannot be told from the 11-digit string alone. Confirmed live against
    four real metformin NDCs (see ARCHITECTURE.md): trying labeler-product as
    printed, then with one padding zero stripped from the product segment,
    then from the labeler segment, found the exact correct openFDA document
    every time.
    """
    if len(ndc) != 11 or not ndc.isdigit():
        return []
    labeler, product = ndc[0:5], ndc[5:9]
    candidates = [f"{labeler}-{product}"]
    if product.startswith("0"):
        candidates.append(f"{labeler}-{product[1:]}")
    if labeler.startswith("0"):
        candidates.append(f"{labeler[1:]}-{product}")
    return candidates


async def resolve_label_document(
    client: httpx.AsyncClient,
    generic_name: str,
    *,
    set_id: str | None = None,
    ndc: str | None = None,
) -> tuple[dict[str, Any] | None, bool]:
    """The one product document every section for this turn is read from.

    Returns ``(document, matched_by_ndc)``. ``matched_by_ndc`` is True only
    when ``ndc`` was given and one of its reconstructions actually resolved -
    meaning this is the user's own real product, not a guess - which callers
    use to decide whether a dosing-ambiguity disclosure is warranted at all.

    Pass ``ndc`` (an 11-digit NADAC-format NDC already in hand - an exact
    Tier 1 lookup, or a formulation the user tapped from a price list) for a
    zero-guessing match to that exact real product; confirmed live to resolve
    correctly across every metformin formulation tried (see
    :func:`_product_ndc_candidates`). Pass ``set_id`` to re-fetch a document
    already chosen earlier in the same conversation - the mechanism that
    keeps a drug's regular info card and its "show more detail" follow-up
    describing the same real product instead of two independently-chosen
    ones. Leave both unset to choose one: walk ``PATIENT_FIELDS`` in the same
    priority :func:`fetch_label` always used and take the first product that
    has one, so the document chosen is at least patient-readable rather than
    an arbitrary one; fall back to any labelled product at all if none does.

    Passing both is expected on every call after the first one in a session -
    callers keep sending the same ``ndc`` alongside the now-known ``set_id``
    purely so :func:`_section` can display the exact package NDC (see
    :func:`_preferred_ndc`), not to re-decide the document. ``set_id`` is
    therefore tried first when both are given: it is the stronger signal ("we
    already chose"), and checking it first avoids paying for 1-3 redundant
    ``ndc``-candidate requests every subsequent turn when the outcome would
    be the same document anyway.
    """
    if set_id:
        body = await _get(client, LABEL_URL, {"search": f'set_id:"{set_id}"', "limit": 1})
        results = body.get("results") or []
        if results:
            return results[0], False
        # The pinned document no longer resolves (rare: index churn mid-session).
        # Fall through and choose fresh rather than silently returning nothing.
    if ndc:
        for candidate in _product_ndc_candidates(ndc):
            body = await _get(
                client, LABEL_URL, {"search": f'openfda.product_ndc:"{candidate}"', "limit": 1}
            )
            results = body.get("results") or []
            if results:
                return results[0], True
        # No reconstruction matched - e.g. this NDC has no SPL on file at all
        # (confirmed real for some retail/OTC products). Fall through rather
        # than returning nothing just because the exact path came up empty.
    for field, _source in PATIENT_FIELDS:
        body = await _get(
            client,
            LABEL_URL,
            {
                "search": f'openfda.generic_name.exact:"{generic_name}" AND _exists_:{field}',
                "limit": 1,
            },
        )
        results = body.get("results") or []
        if results:
            return results[0], False
    body = await _get(
        client, LABEL_URL, {"search": f'openfda.generic_name.exact:"{generic_name}"', "limit": 1}
    )
    results = body.get("results") or []
    return (results[0] if results else None), False


def _formulation_note(doc: dict[str, Any]) -> str:
    """A factual, evidence-derived description of what this document's own
    route/dosage text says, for the ambiguity disclosure - never invented
    wording, just naming what is already being quoted verbatim elsewhere on
    the card. Falls back to a generic caveat when the resolved document does
    not self-describe, which is the common case for a plain immediate-release
    label: it usually never uses the words "immediate release" at all -
    only the special formulation names itself.
    """
    route = ", ".join(r for r in ((doc.get("openfda") or {}).get("route") or []) if r)
    if route and "ORAL" not in route.upper():
        return (
            f"this specific label is for a {route.lower()} product, not an oral tablet or capsule"
        )
    text = " ".join(
        str(doc[f][0])
        for f in ("dosage_and_administration", "dosage_forms_and_strengths")
        if doc.get(f)
    )
    match = _RELEASE_RE.search(text)
    if match:
        return f"this specific label is for the {match.group(1).lower()} version"
    return "more than one real version of this drug exists with different dosing directions"


def _preferred_ndc(ndcs: list[Any], ndc: str | None) -> str:
    """The provenance NDC to show: the one actually queried, not an arbitrary
    sibling package.

    One SPL document can legitimately list several package-size NDCs under
    ``openfda.product_ndc`` (confirmed live: a real Mylan metformin ER
    document lists both ``0378-6001`` and ``0378-6002`` - two bottle sizes of
    the identical product). Always showing index 0 can therefore name a
    different package than the one that was actually resolved - not a wrong
    product (same manufacturer, same formulation, same document), but a
    provenance note claiming to show "the exact product this text came from"
    should show that exact one when it is known, not whichever happens to
    sort first in the array.
    """
    if not ndcs:
        return ""
    if ndc:
        candidates = set(_product_ndc_candidates(ndc))
        match = next((n for n in ndcs if str(n) in candidates), None)
        if match is not None:
            return str(match)
    return str(ndcs[0])


def _section(
    doc: dict[str, Any], field: str, source: str, *, disclose: bool, ndc: str | None = None
) -> LabelText | None:
    """Read one field off an already-resolved document - no network call.

    ``disclose`` attaches the formulation-ambiguity note to the one section it
    actually qualifies (``dosage_and_administration``) rather than as a
    generic footnote spread across the whole card. ``ndc`` (the caller's
    original lookup NDC, if any) is only used to choose which of the
    document's own package NDCs to display - see :func:`_preferred_ndc`.
    """
    value = doc.get(field)
    if not (isinstance(value, list) and value):
        return None
    meta = doc.get("openfda") or {}
    ndcs = meta.get("product_ndc") or []
    makers = meta.get("manufacturer_name") or []
    note = None
    if disclose and field == "dosage_and_administration":
        note = (
            f"Heads up: {_formulation_note(doc)}. If your bottle differs, the dosing "
            "frequency above may not apply - the NDC on it would confirm exactly "
            "which one you have."
        )
    return LabelText(
        field=field,
        source=source,
        text=str(value[0]),
        set_id=str(doc.get("set_id") or ""),
        product_ndc=_preferred_ndc(ndcs, ndc),
        manufacturer=str(makers[0]) if makers else "",
        formulation_note=note,
    )


async def fetch_label(
    client: httpx.AsyncClient,
    generic_name: str,
    *,
    set_id: str | None = None,
    ndc: str | None = None,
    disclose: bool = False,
) -> LabelText | None:
    """First available patient-facing section, from one resolved document.

    ``set_id``/``ndc`` pin to a specific document; see
    :func:`resolve_label_document``. A ``ndc`` match on *this* call always
    suppresses ``disclose`` regardless of what the caller passed - this call
    just proved exactness directly, so trusting a stale caller-side flag over
    that would be a needless way to show a wrong caveat. In practice this is
    moot here: none of ``PATIENT_FIELDS`` is the dosing-frequency section the
    note attaches to anyway.
    """
    doc, matched = await resolve_label_document(client, generic_name, set_id=set_id, ndc=ndc)
    if doc is None:
        return None
    for field, source in PATIENT_FIELDS:
        found = _section(doc, field, source, disclose=disclose and not matched, ndc=ndc)
        if found:
            return found
    return None


async def fetch_detail_sections(
    client: httpx.AsyncClient,
    generic_name: str,
    *,
    set_id: str | None = None,
    ndc: str | None = None,
    disclose: bool = False,
) -> list[LabelText]:
    """Every available clinician-depth section, from that same one document.

    Reading all ``DETAIL_FIELDS`` off a single resolved document - rather than
    independent per-field lookups, the previous approach - is what stops a
    boxed warning from one manufacturer's product being presented alongside a
    drug-interactions table verbatim from a different one (see module
    docstring; confirmed live for metformin IR/ER). ``disclose=True`` (decided
    by the caller from NADAC's own data, not from anything here - see
    ``nadac.formulation_ambiguity``) attaches a plain caveat to the
    ``dosage_and_administration`` section specifically when no NDC pinned this
    to the user's own actual product - but a ``ndc`` match on *this* call
    always suppresses it regardless of what the caller passed, since this
    call just proved exactness directly.
    """
    doc, matched = await resolve_label_document(client, generic_name, set_id=set_id, ndc=ndc)
    if doc is None:
        return []
    effective_disclose = disclose and not matched
    return [
        s
        for s in (
            _section(doc, field, source, disclose=effective_disclose, ndc=ndc)
            for field, source in DETAIL_FIELDS
        )
        if s
    ]


async def fetch_shortage(client: httpx.AsyncClient, generic_name: str) -> Shortage | None:
    """The current shortage record for ``generic_name``, if one exists.

    Filtered to ``status:"Current"`` because the endpoint also carries resolved
    and to-be-discontinued history (447 of 1,622 records), and reporting a
    resolved 2023 shortage as live would be worse than saying nothing.
    """
    body = await _get(
        client,
        SHORTAGE_URL,
        {
            "search": f'openfda.generic_name.exact:"{generic_name}" AND status:"Current"',
            "limit": 1,
        },
    )
    results = body.get("results") or []
    if not results:
        return None
    row = results[0]
    return Shortage(
        generic_name=str(row.get("generic_name", generic_name)),
        status=str(row.get("status", "")),
        availability=str(row.get("availability", "")),
        dosage_form=str(row.get("dosage_form", "")),
        presentation=str(row.get("presentation", "")),
        company_name=str(row.get("company_name", "")),
        updated_on=str(row.get("update_date", "")),
    )
