"""Typed data shapes shared across the agent.

Kept deliberately small - a couple of dataclasses plus the helpers that convert
the two intake paths (form + free text) into one normalized ``trip`` dict stored
in ``ctx.storage``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

BAY_AREA_TZ = ZoneInfo("America/Los_Angeles")

PRIORITIES = ("fastest", "fewest_transfers", "cheapest")


@dataclass
class GeocodeCandidate:
    """One geocoder hit for a place string."""

    label: str
    lat: float
    lon: float
    kind: str = ""  # osm 'type'/'class', informational only

    def to_selection(self, field: str) -> dict[str, Any]:
        return {
            "action": "pick_place",
            "field": field,
            "lat": self.lat,
            "lon": self.lon,
            "label": self.label,
        }


def now_local() -> datetime:
    return datetime.now(BAY_AREA_TZ)


def depart_option_to_iso(option: str | None, *, now: datetime | None = None) -> str | None:
    """Map a form ``depart_option`` to an absolute ISO local timestamp.

    Returns ``None`` for the "pick a time" option (the caller must then ask the
    user to type a time) and for an unrecognized value.
    """
    now = now or now_local()
    if option in (None, "", "now"):
        return now.isoformat()
    if option == "15":
        return (now + timedelta(minutes=15)).isoformat()
    if option == "30":
        return (now + timedelta(minutes=30)).isoformat()
    return None  # "custom" / unknown -> caller prompts for a time


def new_trip(
    *, origin_text: str, destination_text: str, depart_time: str | None, priority: str
) -> dict[str, Any]:
    """Build the normalized ``trip`` dict (coords filled in during geocoding)."""
    return {
        "origin_text": origin_text.strip(),
        "origin_coords": None,
        "destination_text": destination_text.strip(),
        "destination_coords": None,
        "depart_time": depart_time,
        "priority": priority if priority in PRIORITIES else "fastest",
    }


def validate_trip_texts(origin: str, destination: str) -> str | None:
    """Return an error message if the origin/destination pair is invalid, else None."""
    o, d = origin.strip(), destination.strip()
    if not o and not d:
        return "I need both a starting point and a destination."
    if not o:
        return "I didn't catch where you're starting from."
    if not d:
        return "I didn't catch where you're heading to."
    if o.lower() == d.lower():
        return "Your origin and destination look identical - where would you like to go?"
    return None
