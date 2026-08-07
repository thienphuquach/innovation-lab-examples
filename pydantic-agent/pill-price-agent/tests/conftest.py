"""Shared fixtures. Every test here runs offline.

Pydantic AI agents are driven by ``TestModel``/``FunctionModel``, openFDA by an
``httpx.MockTransport``, and NADAC by an in-memory SQLite store seeded with rows
copied verbatim from the 2026-07-22 CMS release - including the metformin ER
formulation split and the 750 MG outlier, so the awkward cases are exercised
rather than idealised away.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from typing import Any

import httpx
import pytest

os.environ.setdefault("ASI_ONE_API_KEY", "test-key-not-used")
os.environ.setdefault("STRIPE_SECRET_KEY", "sk_test_fixture")
os.environ.setdefault("STRIPE_PUBLISHABLE_KEY", "pk_test_fixture")

import nadac
from nadac import NadacStore


def pytest_configure() -> None:
    """Refuse to run against anything that looks like a live key."""
    for name, prefix in (
        ("STRIPE_SECRET_KEY", "sk_test_"),
        ("STRIPE_PUBLISHABLE_KEY", "pk_test_"),
    ):
        value = os.environ.get(name, "")
        if value and not value.startswith(prefix):
            raise RuntimeError(f"{name} must be a test key in tests; refusing to run.")


# (description, ndc, per_unit, effective_on, as_of, unit, otc, classification, generic)
SEED_ROWS: tuple[tuple[Any, ...], ...] = (
    ("METFORMIN HCL 1,000 MG TABLET", "00093104801", 0.02338, "2026-07-22", "2026-07-22"),
    ("METFORMIN HCL 1,000 MG TABLET", "00378018001", 0.02338, "2026-07-22", "2026-07-22"),
    ("METFORMIN ER 1,000 MG OSM-TAB", "00093721401", 0.31495, "2026-07-22", "2026-02-18"),
    ("METFORMIN ER 1,000 MG GASTR-TB", "27241024190", 0.34906, "2026-07-22", "2026-07-22"),
    ("METFORMIN ER 1,000 MG GASTR-TB", "27241024190", 0.42601, "2025-12-17", "2026-01-07"),
    ("METFORMIN ER 1,000 MG GASTR-TB", "27241024190", 0.37006, "2026-06-17", "2026-06-17"),
    ("METFORMIN HCL 500 MG TABLET", "00093104701", 0.01455, "2026-07-22", "2026-07-22"),
    # Two atorvastatin NDCs that agree within a few percent - the "tight" case.
    ("ATORVASTATIN 40 MG TABLET", "00093505698", 0.03739, "2026-07-22", "2026-07-22"),
    ("ATORVASTATIN 40 MG TABLET", "68180063309", 0.03900, "2026-07-22", "2026-07-22"),
    ("LISINOPRIL 10 MG TABLET", "00093113501", 0.01690, "2026-07-22", "2026-07-22"),
    # Combination product: must never be reachable through the lisinopril prefix.
    (
        "LISINOPRIL-HYDROCHLOROTHIAZIDE 20-12.5 MG TAB",
        "00093113601",
        0.03677,
        "2026-07-22",
        "2026-07-22",
    ),
    ("FUROSEMIDE 40 MG TABLET", "00054429725", 0.02071, "2026-07-22", "2026-07-22"),
    # Two package sizes of the same liquid, same description, genuinely
    # different price - confirmed live for ibuprofen and amoxicillin
    # suspensions, where bottle size isn't captured by "250 MG/5 ML" text.
    ("GABAPENTIN 250 MG/5 ML SOLN", "00093500001", 0.02011, "2026-07-22", "2026-07-22"),
    ("GABAPENTIN 250 MG/5 ML SOLN", "00093500002", 0.03746, "2026-07-22", "2026-07-22"),
    ("GABAPENTIN 250 MG/5 ML SOLN", "00093500001", 0.01984, "2026-06-17", "2026-06-17"),
    # Metoprolol tartrate (IR) and succinate (ER) - a different salt for the
    # ER side, filed under its own separate openFDA generic_name (confirmed
    # live), unlike metformin's split. Exercises drugs.Drug.also_priced_as.
    ("METOPROLOL TARTRATE 50 MG TABLET", "00051123401", 0.01500, "2026-07-22", "2026-07-22"),
    ("METOPROLOL SUCC ER 50 MG TABLET", "00051123402", 0.04500, "2026-07-22", "2026-07-22"),
)

BRAND_ROW = (
    "ATORVASTATIN 40 MG TABLET",
    "00071015523",
    9.87654,
    "2026-07-22",
    "2026-07-22",
)


@pytest.fixture
def store(tmp_path: Any) -> Iterator[NadacStore]:
    """A NadacStore seeded with real rows, no network involved."""
    cache = NadacStore(tmp_path / "test.sqlite3")
    rows: list[tuple[Any, ...]] = []
    for description, ndc, per_unit, effective_on, as_of in SEED_ROWS:
        base, strength, form = nadac.split_description(description)
        key = description.split()[0].lower().replace("-hydrochlorothiazide", "")
        if description.startswith("METFORMIN"):
            key = "metformin"
        elif description.startswith("ATORVASTATIN"):
            key = "atorvastatin"
        elif description.startswith("LISINOPRIL-"):
            key = "__combo__"
        elif description.startswith("LISINOPRIL"):
            key = "lisinopril"
        elif description.startswith("FUROSEMIDE"):
            key = "furosemide"
        rows.append(
            (
                key,
                base,
                strength,
                form,
                description,
                ndc,
                per_unit,
                effective_on,
                as_of,
                "EA",
                "N",
                "G",
                None,
            )
        )
    base, strength, form = nadac.split_description(BRAND_ROW[0])
    rows.append(
        (
            "atorvastatin",
            "LIPITOR",
            strength,
            form,
            "LIPITOR 40 MG TABLET",
            BRAND_ROW[1],
            BRAND_ROW[2],
            BRAND_ROW[3],
            BRAND_ROW[4],
            "EA",
            "N",
            "B",
            0.03739,
        )
    )
    with cache._conn:
        cache._conn.executemany("INSERT INTO prices VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    yield cache
    cache.close()


def fda_transport(responses: dict[str, dict[str, Any]]) -> httpx.MockTransport:
    """Mock openFDA, keyed by a substring of the search parameter.

    A miss returns openFDA's real "no matches" shape: HTTP 404 with an error
    body, not an empty result list.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        search = request.url.params.get("search", "")
        for needle, body in responses.items():
            if needle in search:
                return httpx.Response(200, json=body)
        return httpx.Response(
            404, json={"error": {"code": "NOT_FOUND", "message": "No matches found!"}}
        )

    return httpx.MockTransport(handler)


def fda_client(responses: dict[str, dict[str, Any]]) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=fda_transport(responses))


def label_body(
    field: str,
    text: str,
    *,
    set_id: str = "SET-DEFAULT",
    product_ndc: str = "0000-000-00",
    extra_ndcs: tuple[str, ...] = (),
    manufacturer: str = "Example Pharma",
    extra_fields: dict[str, str] | None = None,
) -> dict[str, Any]:
    """One openFDA label document. ``extra_fields`` puts more than one field on
    the *same* document, for tests that check a single resolved document is
    read for every section rather than one independent lookup per field.
    ``extra_ndcs`` appends more package NDCs after ``product_ndc``, for tests
    of a document that legitimately lists several package sizes of one real
    product under ``openfda.product_ndc``.
    """
    doc: dict[str, Any] = {
        field: [text],
        "set_id": set_id,
        "openfda": {
            "product_ndc": [product_ndc, *extra_ndcs],
            "manufacturer_name": [manufacturer],
        },
    }
    for extra_field, extra_text in (extra_fields or {}).items():
        doc[extra_field] = [extra_text]
    return {"meta": {"results": {"total": 1}}, "results": [doc]}


def shortage_body(**overrides: Any) -> dict[str, Any]:
    row = {
        "generic_name": "Furosemide Injection",
        "status": "Current",
        "availability": "Limited supply",
        "dosage_form": "Injection",
        "presentation": "Furosemide, Injection, 10 mg/mL",
        "company_name": "Example Pharma",
        "update_date": "07/16/2026",
    }
    row.update(overrides)
    return {"meta": {"results": {"total": 1}}, "results": [row]}


def selection_text(payload: dict[str, Any]) -> str:
    """A card selection as ASI:One delivers it on a direct @mention."""
    return json.dumps(payload)
