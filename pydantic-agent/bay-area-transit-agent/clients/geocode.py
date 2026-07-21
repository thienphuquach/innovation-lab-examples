"""Geocoding for Stage 2.5 - free text -> (lat, lon).

Uses OpenStreetMap **Nominatim** as the primary (and only) geocoder. The project
brief proposed Transitland stop-name search as tier 1, but the Transitland v2
REST *stops* endpoint has no free-text name parameter (``search`` exists only on
agencies/operators/routes) - see ``research-notes.md`` §7. Nominatim is free,
well-documented, returns multiple candidates for disambiguation, and covers both
transit-stop names and street addresses/landmarks, so it serves both tiers here.

Nominatim usage policy is respected: a descriptive ``User-Agent``, a courtesy
rate limit of <=1 request/second, results biased to the Bay Area, and results
cached in-process for a short window (rate-limit discipline).
"""

from __future__ import annotations

import asyncio
import os
import time
from typing import Any

import httpx

from models import GeocodeCandidate

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
USER_AGENT = os.getenv(
    "NOMINATIM_USER_AGENT",
    "bay-area-transit-agent/1.0 (Fetch.ai Innovation Lab example)",
)

# Bounding box biasing results to the SF Bay Area: left,top,right,bottom.
_BAY_VIEWBOX = "-123.60,38.90,-121.20,36.90"

_MIN_INTERVAL_S = 1.1  # Nominatim courtesy limit
_CACHE_TTL_S = 300.0

_last_call_at = 0.0
_rate_lock = asyncio.Lock()
_cache: dict[str, tuple[float, list[GeocodeCandidate]]] = {}


async def _throttle() -> None:
    global _last_call_at
    async with _rate_lock:
        wait = _MIN_INTERVAL_S - (time.monotonic() - _last_call_at)
        if wait > 0:
            await asyncio.sleep(wait)
        _last_call_at = time.monotonic()


async def geocode(text: str, *, limit: int = 5) -> list[GeocodeCandidate]:
    """Return up to ``limit`` Bay-Area-biased candidates for ``text``."""
    key = text.strip().lower()
    if not key:
        return []
    cached = _cache.get(key)
    if cached and (time.monotonic() - cached[0]) < _CACHE_TTL_S:
        return cached[1]

    params: dict[str, Any] = {
        "q": text,
        "format": "json",
        "addressdetails": 1,
        "limit": limit,
        "countrycodes": "us",
        "viewbox": _BAY_VIEWBOX,
        "bounded": 1,
    }
    await _throttle()
    async with httpx.AsyncClient(timeout=10.0, headers={"User-Agent": USER_AGENT}) as client:
        resp = await client.get(NOMINATIM_URL, params=params)
        resp.raise_for_status()
        rows = resp.json()

    candidates = [
        GeocodeCandidate(
            label=row.get("display_name", text),
            lat=float(row["lat"]),
            lon=float(row["lon"]),
            kind=f"{row.get('class', '')}/{row.get('type', '')}".strip("/"),
        )
        for row in rows
        if row.get("lat") and row.get("lon")
    ]
    _cache[key] = (time.monotonic(), candidates)
    return candidates


async def resolve_place(text: str) -> tuple[str, list[GeocodeCandidate]]:
    """Resolve a place string into a status + candidate list.

    Status is one of:
    - ``"resolved"``  - exactly one confident candidate (``candidates[0]``),
    - ``"ambiguous"`` - multiple candidates; caller shows a disambiguation card,
    - ``"not_found"`` - zero candidates; caller shows a no-match card.

    A genuinely low-confidence single name (e.g. "Berkeley") naturally comes back
    from Nominatim as several candidates, so it lands in the ``"ambiguous"`` path
    and gets a confirmation step rather than being silently accepted.
    """
    candidates = await geocode(text)
    if not candidates:
        return "not_found", []
    if len(candidates) == 1:
        return "resolved", candidates
    return "ambiguous", candidates
