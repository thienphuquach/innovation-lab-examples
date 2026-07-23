"""Confirmed-trip map image (diagnosis.md issue 7; improved per ux-diagnosis.md
issue D).

Renders a static map of the confirmed itinerary - one coloured line per transit
leg (grey for walking) plus a distinct marker style for the origin, each
transfer point, and the final destination - from data the routing response
already carries (``legGeometry.points``, an encoded polyline, and each leg's
``from``/``to`` coordinates). No paid map API is involved:

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

A verified-live-first investigation (ux-diagnosis.md issue D) confirmed no
live/interactive rendering surface exists in the interactive-cards protocol
for a third-party agent - so this stays a static image, improved two ways
instead: a plain-language colour legend (:func:`build_map_legend`, since the
image itself can't carry a key), and a free, keyless "open in your own maps
app" link (:func:`google_maps_url`, confirmed via Google's own docs to need
no API key) for a genuinely live, pannable view on the user's own device.

Every network step here is best-effort: a tile-server hiccup or a
storage-upload failure must never block or fail the trip confirmation itself,
so :func:`build_trip_map_resource` returns ``None`` on any error instead of
raising, and the caller (``chat_proto._send_trip_map``) treats a ``None`` as
"skip the map, the trip confirmation already went out."
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode
from uuid import UUID, uuid4

import staticmap
from uagents import Context
from uagents_core.contrib.protocols.chat import Resource, ResourceContent
from uagents_core.storage import ExternalStorage

from cards import plain_leg_label

_USER_AGENT = os.getenv(
    "OSM_TILE_USER_AGENT", "bay-area-transit-agent/1.0 (Fetch.ai Innovation Lab example)"
)
_LEG_COLORS = ("#d32f2f", "#1976d2", "#388e3c", "#f57c00", "#7b1fa2")
_LEG_COLOR_NAMES = ("red", "blue", "green", "orange", "purple")
_WALK_COLOR = "#9e9e9e"
_MAP_SIZE = (640, 420)
_ORIGIN_MARKER = "#2e7d32"  # green - "start here"
_TRANSFER_MARKER = "#757575"  # mid-grey, small - "just passing through"
_DEST_MARKER = "#000000"  # black, largest - "this is the end"


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


_COLOR_EMOJI = {"red": "🔴", "blue": "🔵", "green": "🟢", "orange": "🟠", "purple": "🟣"}


def _leg_render_specs(itinerary: dict[str, Any]) -> list[dict[str, Any]]:
    """One entry per leg with usable polyline geometry - shared by the renderer
    and the legend builder so their colours/descriptions can never drift apart.
    """
    specs: list[dict[str, Any]] = []
    transit_idx = 0
    for leg in itinerary.get("legs", []):
        points = ((leg.get("legGeometry") or {}).get("points") or "").strip()
        if not points:
            continue
        coords = [(lon, lat) for lat, lon in decode_polyline(points)]  # staticmap wants (lon, lat)
        if len(coords) < 2:
            continue
        if leg.get("transitLeg"):
            color = _LEG_COLORS[transit_idx % len(_LEG_COLORS)]
            color_name = _LEG_COLOR_NAMES[transit_idx % len(_LEG_COLOR_NAMES)]
            transit_idx += 1
            headsign = leg.get("headsign")
            desc = plain_leg_label(leg) + (f" toward {headsign}" if headsign else "")
            specs.append(
                {"coords": coords, "color": color, "color_name": color_name, "width": 4,
                 "desc": desc, "is_walk": False}
            )
        else:
            specs.append(
                {"coords": coords, "color": _WALK_COLOR, "color_name": "grey", "width": 3,
                 "desc": "Walking", "is_walk": True}
            )
    return specs


def _close(a: tuple[float, float], b: tuple[float, float], tol: float = 0.0015) -> bool:
    """True if two (lon, lat) points are close enough (~150m) that drawing a
    marker at both would visually collapse into what looks like one stop."""
    return abs(a[0] - b[0]) < tol and abs(a[1] - b[1]) < tol


def render_itinerary_map(itinerary: dict[str, Any]) -> bytes:
    """Render the itinerary to a PNG image (bytes). Synchronous - fetches tiles.

    Markers are no longer one identical dot per leg boundary (ux-diagnosis.md
    issue D - indistinguishable start/transfer/end). The origin, every genuine
    transfer point, and the final destination each get their own colour/size,
    and boundaries within ~150m of each other collapse to a single marker so a
    short walk leg doesn't render as two overlapping dots that look like one.
    """
    specs = _leg_render_specs(itinerary)
    if not specs:
        raise ValueError("itinerary has no usable leg geometry to map")

    m = staticmap.StaticMap(*_MAP_SIZE, headers={"User-Agent": _USER_AGENT})
    for spec in specs:
        m.add_line(staticmap.Line(spec["coords"], spec["color"], spec["width"]))

    boundary_points: list[tuple[float, float]] = []
    for leg in itinerary.get("legs", []):
        frm = leg.get("from") or {}
        if frm.get("lat") is not None and frm.get("lon") is not None:
            boundary_points.append((frm["lon"], frm["lat"]))
    legs = itinerary.get("legs", [])
    last_to = (legs[-1].get("to") if legs else None) or {}
    if last_to.get("lat") is not None and last_to.get("lon") is not None:
        boundary_points.append((last_to["lon"], last_to["lat"]))

    kept: list[tuple[float, float]] = []
    for pt in boundary_points:
        if kept and _close(pt, kept[-1]):
            continue
        kept.append(pt)

    for i, pt in enumerate(kept):
        if i == 0:
            m.add_marker(staticmap.CircleMarker(pt, _ORIGIN_MARKER, 8))
        elif i == len(kept) - 1:
            m.add_marker(staticmap.CircleMarker(pt, _DEST_MARKER, 9))
        else:
            m.add_marker(staticmap.CircleMarker(pt, _TRANSFER_MARKER, 6))

    image = m.render()
    from io import BytesIO

    buf = BytesIO()
    image.save(buf, format="PNG")
    return buf.getvalue()


def build_map_legend(itinerary: dict[str, Any]) -> str:
    """Plain-language colour key for the map (ux-diagnosis.md issue D) - the
    PNG can't carry a legend itself, so this text always accompanies it."""
    specs = _leg_render_specs(itinerary)
    parts = [
        f"{_COLOR_EMOJI.get(spec['color_name'], '⚫')} {spec['desc']}"
        for spec in specs
        if not spec["is_walk"]
    ]
    if not parts:
        return ""
    if any(spec["is_walk"] for spec in specs):
        parts.append("⚪ walking")
    return "Map key: " + " · ".join(parts) + " (🟢 start · ⚪ transfer · ⚫ end)"


def google_maps_url(itinerary: dict[str, Any]) -> str | None:
    """A free, keyless "open in your own maps app" link for a genuinely live,
    pannable view (ux-diagnosis.md issue D) - Google's own Maps URLs docs
    confirm no API key is required for this. Returns ``None`` if the itinerary
    has no usable origin/destination coordinates.
    """
    legs = itinerary.get("legs", [])
    if not legs:
        return None
    origin = legs[0].get("from") or {}
    dest = legs[-1].get("to") or {}
    if origin.get("lat") is None or origin.get("lon") is None:
        return None
    if dest.get("lat") is None or dest.get("lon") is None:
        return None
    travelmode = "transit" if any(leg.get("transitLeg") for leg in legs) else "walking"
    params = {
        "api": "1",
        "origin": f"{origin['lat']},{origin['lon']}",
        "destination": f"{dest['lat']},{dest['lon']}",
        "travelmode": travelmode,
    }
    return f"https://www.google.com/maps/dir/?{urlencode(params)}"


def _storage_url() -> str:
    base = (os.getenv("AGENTVERSE_URL") or "https://agentverse.ai").rstrip("/")
    return f"{base}/v1/storage"


@dataclass
class TripMap:
    """Everything the caller needs to present the trip's map, however much of
    it succeeded. ``resource`` is ``None`` if rendering/upload failed or is
    unconfigured - but ``legend``/``maps_url`` need no network call, so a
    tile-server or storage outage still leaves the user with a usable link."""

    resource: ResourceContent | None
    legend: str
    maps_url: str | None


async def build_trip_map_resource(ctx: Context, sender: str, itinerary: dict[str, Any]) -> TripMap:
    """Render, upload, and wrap the confirmed itinerary's map as chat-ready content.

    ``resource`` is ``None`` (never raises) if the agent has no Agentverse API
    key configured, or if rendering/upload fails for any reason - a missing
    map image is a silent degrade, never a broken trip confirmation, and the
    legend/maps-link still go out either way.
    """
    legend = build_map_legend(itinerary)
    maps_url = google_maps_url(itinerary)

    api_token = (os.getenv("AGENTVERSE_API_KEY") or "").strip()
    if not api_token:
        ctx.logger.debug("[map] AGENTVERSE_API_KEY not set - skipping trip map image")
        return TripMap(resource=None, legend=legend, maps_url=maps_url)

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
        return TripMap(resource=None, legend=legend, maps_url=maps_url)

    resource = ResourceContent(
        type="resource",
        resource_id=UUID(asset_id),
        resource=Resource(
            uri=f"agent-storage://{storage_url}/{asset_id}",
            metadata={"mime_type": "image/png", "role": "trip-map"},
        ),
    )
    return TripMap(resource=resource, legend=legend, maps_url=maps_url)
