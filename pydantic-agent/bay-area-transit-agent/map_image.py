"""Confirmed-trip map image (diagnosis.md issue 7).

Renders a static map of the confirmed itinerary - one coloured line per transit
leg (grey for walking) plus a marker at every board/alight point - from data the
routing response already carries (``legGeometry.points``, an encoded polyline,
and each leg's ``from``/``to`` coordinates). No paid map API is involved:

* Tiles come from OpenStreetMap's own public raster tile server, fetched
  through the ``staticmap`` library with a descriptive ``User-Agent`` (OSM's
  tile usage policy: low-volume, self-identifying, non-bulk use - this renders
  at most one map per *confirmed* trip, not per carousel item or per glance at
  a route, so volume stays low by construction).
* The rendered PNG is uploaded to Agentverse's ``ExternalStorage`` (already
  used for exactly this purpose by the Innovation Lab's own reference image
  agents) and delivered as a Chat Protocol ``ResourceContent`` with an
  ``agent-storage://`` URI - the documented pattern for sharing an image in
  ASI:One chat, not an unverified ``hero_image`` card field.

Every step here is best-effort: a tile-server hiccup or a storage-upload
failure must never block or fail the trip confirmation itself, so
:func:`build_trip_map_resource` returns ``None`` on any error instead of
raising, and the caller (``chat_proto._send_trip_map``) treats a ``None`` as
"skip the map, the trip confirmation already went out."
"""

from __future__ import annotations

import asyncio
import os
from typing import Any
from uuid import UUID, uuid4

import staticmap
from uagents import Context
from uagents_core.contrib.protocols.chat import Resource, ResourceContent
from uagents_core.storage import ExternalStorage

_USER_AGENT = os.getenv(
    "OSM_TILE_USER_AGENT", "bay-area-transit-agent/1.0 (Fetch.ai Innovation Lab example)"
)
_LEG_COLORS = ("#d32f2f", "#1976d2", "#388e3c", "#f57c00", "#7b1fa2")
_WALK_COLOR = "#9e9e9e"
_MAP_SIZE = (640, 420)


def decode_polyline(encoded: str, precision: int = 5) -> list[tuple[float, float]]:
    """Decode a Google/OTP-style encoded polyline into ``[(lat, lon), ...]``."""
    factor = 10**precision
    coords: list[tuple[float, float]] = []
    index = lat = lon = 0
    length = len(encoded)
    while index < length:
        for is_lat in (True, False):
            shift = result = 0
            while True:
                b = ord(encoded[index]) - 63
                index += 1
                result |= (b & 0x1F) << shift
                shift += 5
                if b < 0x20:
                    break
            delta = ~(result >> 1) if (result & 1) else (result >> 1)
            if is_lat:
                lat += delta
            else:
                lon += delta
        coords.append((lat / factor, lon / factor))
    return coords


def render_itinerary_map(itinerary: dict[str, Any]) -> bytes:
    """Render the itinerary to a PNG image (bytes). Synchronous - fetches tiles."""
    m = staticmap.StaticMap(*_MAP_SIZE, headers={"User-Agent": _USER_AGENT})

    transit_idx = 0
    any_geometry = False
    for leg in itinerary.get("legs", []):
        points = ((leg.get("legGeometry") or {}).get("points") or "").strip()
        if not points:
            continue
        coords = [(lon, lat) for lat, lon in decode_polyline(points)]  # staticmap wants (lon, lat)
        if len(coords) < 2:
            continue
        any_geometry = True
        if leg.get("transitLeg"):
            color = _LEG_COLORS[transit_idx % len(_LEG_COLORS)]
            transit_idx += 1
            width = 4
        else:
            color = _WALK_COLOR
            width = 3
        m.add_line(staticmap.Line(coords, color, width))

    if not any_geometry:
        raise ValueError("itinerary has no usable leg geometry to map")

    legs = itinerary.get("legs", [])
    for i, leg in enumerate(legs):
        frm = leg.get("from") or {}
        if frm.get("lat") is not None and frm.get("lon") is not None:
            m.add_marker(staticmap.CircleMarker((frm["lon"], frm["lat"]), "#212121", 7))
    last_to = (legs[-1].get("to") if legs else None) or {}
    if last_to.get("lat") is not None and last_to.get("lon") is not None:
        m.add_marker(staticmap.CircleMarker((last_to["lon"], last_to["lat"]), "#212121", 9))

    image = m.render()
    from io import BytesIO

    buf = BytesIO()
    image.save(buf, format="PNG")
    return buf.getvalue()


def _storage_url() -> str:
    base = (os.getenv("AGENTVERSE_URL") or "https://agentverse.ai").rstrip("/")
    return f"{base}/v1/storage"


async def build_trip_map_resource(
    ctx: Context, sender: str, itinerary: dict[str, Any]
) -> ResourceContent | None:
    """Render, upload, and wrap the confirmed itinerary's map as chat-ready content.

    Returns ``None`` (never raises) if the agent has no Agentverse API key
    configured, or if rendering/upload fails for any reason - a missing map is a
    silent degrade, never a broken trip confirmation.
    """
    api_token = (os.getenv("AGENTVERSE_API_KEY") or "").strip()
    if not api_token:
        ctx.logger.debug("[map] AGENTVERSE_API_KEY not set - skipping trip map")
        return None

    storage_url = _storage_url()
    try:
        png_bytes = await asyncio.to_thread(render_itinerary_map, itinerary)
        storage = ExternalStorage(api_token=api_token, storage_url=storage_url)
        asset_id = await asyncio.to_thread(
            storage.create_asset,
            name=f"trip-map-{uuid4()}",
            content=png_bytes,
            mime_type="image/png",
            lifetime_hours=24,
        )
        await asyncio.to_thread(
            storage.set_permissions, asset_id=asset_id, agent_address=sender, read=True, write=False
        )
    except Exception as exc:
        ctx.logger.error(f"[map] render/upload failed: {exc}")
        return None

    return ResourceContent(
        type="resource",
        resource_id=UUID(asset_id),
        resource=Resource(
            uri=f"agent-storage://{storage_url}/{asset_id}",
            metadata={"mime_type": "image/png", "role": "trip-map"},
        ),
    )
