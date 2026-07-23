"""Confirmed-trip map image (diagnosis.md issue 7).

Doesn't hit the real OSM tile server or Agentverse storage - just the polyline
decode and the "never break the trip confirmation" contract of
``build_trip_map_resource``.
"""

from __future__ import annotations

import pytest

import map_image


def test_decode_polyline_known_vector():
    # The canonical example from Google's encoded-polyline-algorithm docs.
    coords = map_image.decode_polyline("_p~iF~ps|U_ulLnnqC_mqNvxq`@")
    assert coords == [(38.5, -120.2), (40.7, -120.95), (43.252, -126.453)]


@pytest.mark.asyncio
async def test_build_trip_map_resource_returns_none_without_api_key(monkeypatch, ctx, sender):
    monkeypatch.delenv("AGENTVERSE_API_KEY", raising=False)
    result = await map_image.build_trip_map_resource(ctx, sender, {"legs": []})
    assert result is None


@pytest.mark.asyncio
async def test_build_trip_map_resource_never_raises_on_render_failure(monkeypatch, ctx, sender):
    """A tile-server hiccup or a malformed itinerary must degrade to 'no map,'
    never bubble up and break the trip confirmation that already went out."""
    monkeypatch.setenv("AGENTVERSE_API_KEY", "fake-token-for-test")

    def boom(itinerary):
        raise RuntimeError("tile server unreachable")

    monkeypatch.setattr(map_image, "render_itinerary_map", boom)
    result = await map_image.build_trip_map_resource(ctx, sender, {"legs": []})
    assert result is None


def test_render_itinerary_map_rejects_itinerary_with_no_geometry():
    with pytest.raises(ValueError):
        map_image.render_itinerary_map({"legs": [{"mode": "WALK", "transitLeg": False}]})
