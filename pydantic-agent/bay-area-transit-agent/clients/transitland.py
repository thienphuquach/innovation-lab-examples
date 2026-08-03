"""Transitland Routing API client (OTP-compatible ``/plan`` endpoint).

Schedule-only multi-modal itineraries. Coordinates are sent as **lat,lon** (the
docs' table mislabels this as "lon,lat" but the example value and the response
``from``/``to`` objects are lat,lon - see ``research-notes.md`` §7).

Every search departs **now**: the moment a route search runs is the moment the
rider means, so there is no caller-supplied departure time.

Responses are cached by ``(origin, destination, 5-minute time bucket)`` for a few
minutes so a repeated or "Back" query never spends against the 1,000/month free
budget (the cache is also what makes Stage 4's "Back" free).
"""

from __future__ import annotations

import os
import time
from datetime import datetime
from typing import Any

import httpx

from models import now_local

ROUTING_URL = "https://transit.land/api/v2/routing/otp/plan"

_CACHE_TTL_S = 300.0
_cache: dict[str, tuple[float, dict[str, Any]]] = {}


class RoutingError(RuntimeError):
    """Raised when the routing API errors, times out, or is misconfigured."""


def _api_key() -> str:
    key = (os.getenv("Transitland_Routing_API") or "").strip()
    if not key:
        raise RoutingError("Transitland_Routing_API is not set.")
    return key


def _cache_key(origin: list[float], dest: list[float], dt: datetime) -> str:
    bucket = dt.strftime("%Y-%m-%d %H:") + str((dt.minute // 5) * 5)
    return (
        f"{round(origin[0], 4)},{round(origin[1], 4)}"
        f"|{round(dest[0], 4)},{round(dest[1], 4)}|{bucket}"
    )


async def plan(
    origin_coords: list[float],
    dest_coords: list[float],
    *,
    max_itineraries: int = 6,
) -> dict[str, Any]:
    """Return the raw Transitland ``plan`` object (``{itineraries: [...], ...}``).

    Departs at the current Bay Area time, evaluated here rather than by the
    caller, so a search always reflects the moment it actually runs.
    ``origin_coords``/``dest_coords`` are ``[lat, lon]``. Raises :class:`RoutingError`
    on network/API failure so the caller can surface an apologetic card.
    """
    dt = now_local()
    key = _cache_key(origin_coords, dest_coords, dt)
    cached = _cache.get(key)
    if cached and (time.monotonic() - cached[0]) < _CACHE_TTL_S:
        return cached[1]

    params = {
        "fromPlace": f"{origin_coords[0]},{origin_coords[1]}",
        "toPlace": f"{dest_coords[0]},{dest_coords[1]}",
        "date": dt.strftime("%Y-%m-%d"),
        "time": dt.strftime("%H:%M:%S"),
        "numItineraries": str(max_itineraries),
        "fallbackWalkingItinerary": "true",
        "api_key": _api_key(),
    }
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.get(ROUTING_URL, params=params)
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPError as exc:
        raise RoutingError(f"Routing request failed: {exc}") from exc

    plan_obj = data.get("plan") or {}
    _cache[key] = (time.monotonic(), plan_obj)
    return plan_obj


def transit_legs(itinerary: dict[str, Any]) -> list[dict[str, Any]]:
    """The non-walking legs of an itinerary, in order."""
    return [leg for leg in itinerary.get("legs", []) if leg.get("transitLeg")]
