"""Confirmed-trip map image (diagnosis.md issue 7; improved per ux-diagnosis.md
issue D: a colour legend + a free "open in Google Maps" link, since neither the
tile-server render nor the Agentverse upload can be relied on, and no live/
interactive map surface exists on this platform for a third-party agent).

Doesn't hit the real OSM tile server or Agentverse storage - just the polyline
decode, the legend/URL builders, and the "never break the trip confirmation"
contract of ``build_trip_map_resource``.
"""

from __future__ import annotations

import pytest

import map_image

_ITINERARY = {
    "legs": [
        {
            "mode": "RAIL", "transitLeg": True, "routeShortName": "Red-S",
            "agencyName": "Bay Area Rapid Transit", "headsign": "Millbrae",
            "from": {"lat": 37.87, "lon": -122.27}, "to": {"lat": 37.75, "lon": -122.40},
            "legGeometry": {"points": "_p~iF~ps|U_ulLnnqC"},
        },
    ]
}


def test_decode_polyline_known_vector():
    # The canonical example from Google's encoded-polyline-algorithm docs.
    coords = map_image.decode_polyline("_p~iF~ps|U_ulLnnqC_mqNvxq`@")
    assert coords == [(38.5, -120.2), (40.7, -120.95), (43.252, -126.453)]


# ── Issue D: plain-language legend + keyless "open in your own maps app" ────
def test_build_map_legend_names_the_leg_plainly_not_by_route_code():
    legend = map_image.build_map_legend(_ITINERARY)
    assert "BART train toward Millbrae" in legend
    assert "Red-S" not in legend  # the line-color code is jargon here, not useful


def test_google_maps_url_is_keyless_and_transit_mode():
    url = map_image.google_maps_url(_ITINERARY)
    assert url is not None
    assert url.startswith("https://www.google.com/maps/dir/?")
    assert "travelmode=transit" in url
    assert "37.87" in url and "-122.4" in url
    assert "key=" not in url  # no API key parameter - confirmed unnecessary


def test_google_maps_url_uses_walking_mode_when_no_transit_leg():
    walk_only = {"legs": [{"mode": "WALK", "transitLeg": False,
                            "from": {"lat": 1.0, "lon": 2.0}, "to": {"lat": 1.1, "lon": 2.1}}]}
    url = map_image.google_maps_url(walk_only)
    assert url is not None and "travelmode=walking" in url


def test_google_maps_url_none_without_coordinates():
    assert map_image.google_maps_url({"legs": []}) is None


# ── Issue D: markers must distinguish start/transfer/end, not one identical dot ─
def test_boundary_markers_dedupe_points_within_150m():
    # A short walk leg's from/to (e.g. platform to a nearby stop) sit within
    # ~150m of each other - they must collapse to a single marker so it
    # doesn't render as two overlapping dots that look like one.
    assert map_image._close((-122.270, 37.870), (-122.2701, 37.8701))
    assert not map_image._close((-122.270, 37.870), (-122.40, 37.75))


@pytest.mark.asyncio
async def test_build_trip_map_resource_still_returns_legend_and_url_without_api_key(
    monkeypatch, ctx, sender
):
    """A missing Agentverse key skips the image, but the legend and Google
    Maps link need no network call, so they must still be there."""
    monkeypatch.delenv("AGENTVERSE_API_KEY", raising=False)
    result = await map_image.build_trip_map_resource(ctx, sender, _ITINERARY)
    assert result.resource is None
    assert "BART" in result.legend
    assert result.maps_url is not None


@pytest.mark.asyncio
async def test_build_trip_map_resource_never_raises_on_render_failure(monkeypatch, ctx, sender):
    """A tile-server hiccup or a malformed itinerary must degrade to 'no map
    image,' never bubble up and break the trip confirmation that already went
    out - but the legend/link (no network needed) still go out."""
    monkeypatch.setenv("AGENTVERSE_API_KEY", "fake-token-for-test")

    def boom(itinerary):
        raise RuntimeError("tile server unreachable")

    monkeypatch.setattr(map_image, "render_itinerary_map", boom)
    result = await map_image.build_trip_map_resource(ctx, sender, _ITINERARY)
    assert result.resource is None
    assert result.maps_url is not None


def test_render_itinerary_map_rejects_itinerary_with_no_geometry():
    with pytest.raises(ValueError):
        map_image.render_itinerary_map({"legs": [{"mode": "WALK", "transitLeg": False}]})
