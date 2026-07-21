"""511 SF Bay Regional client - GTFS-Fares v2 data + GTFS-RT service alerts.

The regional GTFS zip (``operator_id=RG``) bundles the Fares v2 files for all ~30+
Bay Area operators. It's large (~65 MB) so it's downloaded once to ``.cache/`` and
parsed into an in-process :class:`FareData` snapshot; schedules/fares change slowly,
so a long cache TTL is fine and keeps us well under the 60 req/hour token limit.

Live disruptions come from the GTFS-RT **Service Alerts** feed in JSON form
(``format=json``), filtered to the routes in the chosen itinerary. (Trip Updates,
the finer-grained delay feed, are protobuf-only; alerts in JSON are a lighter,
dependency-free overlay that satisfies "surface any active delay or alert" without
pulling a protobuf decoder into the request path.)
"""

from __future__ import annotations

import asyncio
import csv
import io
import os
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

BASE = "http://api.511.org/transit"
CACHE_DIR = Path(os.getenv("FIVE11_CACHE_DIR", ".cache"))
_ZIP_PATH = CACHE_DIR / "gtfs_regional.zip"
_ZIP_TTL_S = float(os.getenv("FIVE11_GTFS_TTL_S", str(24 * 3600)))

CLIPPER_MEDIA = ("clipper", "contactless", "munimobile", "ezfare", "tokentransit")
CASH_MEDIA = ("cash", "ticket")

_PASS_MARKERS = ("day", "month", "week", "passport", "-day", "1-month", "31-day")


@dataclass
class Product:
    amount: float
    currency: str
    rider: str
    media: str
    is_pass: bool
    name: str


@dataclass
class LegRule:
    from_area: str
    to_area: str
    leg_group_id: str
    fare_product_id: str
    transfer_only: bool


@dataclass
class TransferRule:
    from_leg_group: str
    to_leg_group: str
    fare_product_id: str


@dataclass
class FareData:
    route_network: dict[str, str] = field(default_factory=dict)
    leg_rules: dict[str, list[LegRule]] = field(default_factory=dict)  # network_id -> rules
    products: dict[str, list[Product]] = field(default_factory=dict)  # product_id -> rows
    transfer_rules: list[TransferRule] = field(default_factory=list)
    media_names: dict[str, str] = field(default_factory=dict)

    def net_leg_group(self, network_id: str) -> str:
        rules = self.leg_rules.get(network_id) or []
        return rules[0].leg_group_id if rules else network_id


_fare_data: FareData | None = None
_load_lock = asyncio.Lock()


def _is_pass(product_id: str) -> bool:
    pid = product_id.lower()
    return any(m in pid for m in _PASS_MARKERS)


def _api_key() -> str:
    key = (os.getenv("sf_bay_511_api") or "").strip()
    if not key:
        raise RuntimeError("sf_bay_511_api is not set.")
    return key


def _download_regional_zip() -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    fresh = _ZIP_PATH.exists() and (time.time() - _ZIP_PATH.stat().st_mtime) < _ZIP_TTL_S
    if fresh and _ZIP_PATH.stat().st_size > 1000:
        return
    with httpx.Client(timeout=180.0) as client:
        resp = client.get(BASE + "/datafeeds", params={"api_key": _api_key(), "operator_id": "RG"})
        resp.raise_for_status()
        _ZIP_PATH.write_bytes(resp.content)


def _read_csv(z: zipfile.ZipFile, name: str) -> tuple[list[str], list[list[str]]]:
    with z.open(name) as f:
        rows = list(csv.reader(io.TextIOWrapper(f, encoding="utf-8-sig")))
    return (rows[0], rows[1:]) if rows else ([], [])


def _parse_fare_data() -> FareData:
    data = FareData()
    with zipfile.ZipFile(_ZIP_PATH) as z:
        names = set(z.namelist())

        header, rows = _read_csv(z, "routes.txt")
        ci = {c: i for i, c in enumerate(header)}
        if "network_id" in ci:
            for r in rows:
                net = r[ci["network_id"]].strip()
                if net:
                    data.route_network[r[ci["route_id"]]] = net

        header, rows = _read_csv(z, "fare_media.txt")
        ci = {c: i for i, c in enumerate(header)}
        for r in rows:
            data.media_names[r[ci["fare_media_id"]]] = r[ci["fare_media_name"]]

        header, rows = _read_csv(z, "fare_products.txt")
        ci = {c: i for i, c in enumerate(header)}
        for r in rows:
            pid = r[ci["fare_product_id"]]
            try:
                amount = float(r[ci["amount"]] or 0)
            except ValueError:
                amount = 0.0
            data.products.setdefault(pid, []).append(
                Product(
                    amount=amount,
                    currency=r[ci["currency"]] or "USD",
                    rider=r[ci["rider_category_id"]] or "adult",
                    media=r[ci["fare_media_id"]] or "",
                    is_pass=_is_pass(pid),
                    name=r[ci["fare_product_name"]],
                )
            )

        header, rows = _read_csv(z, "fare_leg_rules.txt")
        ci = {c: i for i, c in enumerate(header)}
        for r in rows:
            net = r[ci["network_id"]].strip()
            data.leg_rules.setdefault(net, []).append(
                LegRule(
                    from_area=r[ci["from_area_id"]].strip(),
                    to_area=r[ci["to_area_id"]].strip(),
                    leg_group_id=r[ci["leg_group_id"]].strip(),
                    fare_product_id=r[ci["fare_product_id"]].strip(),
                    transfer_only=(r[ci["transfer_only"]].strip() == "1"),
                )
            )

        if "fare_transfer_rules.txt" in names:
            header, rows = _read_csv(z, "fare_transfer_rules.txt")
            ci = {c: i for i, c in enumerate(header)}
            for r in rows:
                data.transfer_rules.append(
                    TransferRule(
                        from_leg_group=r[ci["from_leg_group_id"]].strip(),
                        to_leg_group=r[ci["to_leg_group_id"]].strip(),
                        fare_product_id=r[ci["fare_product_id"]].strip(),
                    )
                )
    return data


async def load_fare_data() -> FareData:
    """Return the parsed regional fare data (download + parse once, then cached)."""
    global _fare_data
    if _fare_data is not None:
        return _fare_data
    async with _load_lock:
        if _fare_data is None:
            await asyncio.to_thread(_download_regional_zip)
            _fare_data = await asyncio.to_thread(_parse_fare_data)
    return _fare_data


# ── GTFS-RT Service Alerts (JSON) ─────────────────────────────────────────────
_alerts_cache: tuple[float, list[dict[str, Any]]] | None = None
_ALERTS_TTL_S = 60.0


async def _fetch_regional_alerts() -> list[dict[str, Any]]:
    global _alerts_cache
    if _alerts_cache and (time.monotonic() - _alerts_cache[0]) < _ALERTS_TTL_S:
        return _alerts_cache[1]
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(
            BASE + "/servicealerts", params={"api_key": _api_key(), "agency": "RG", "format": "json"}
        )
        resp.raise_for_status()
        entities = resp.json().get("entity", []) or []
    _alerts_cache = (time.monotonic(), entities)
    return entities


def _alert_text(entity: dict[str, Any]) -> str:
    alert = entity.get("alert", {}) or {}
    for field_name in ("header_text", "description_text"):
        block = alert.get(field_name) or {}
        for tr in block.get("translation", []) or []:
            if tr.get("text"):
                return str(tr["text"]).strip()
    return ""


async def alerts_for_routes(route_ids: set[str], agency_ids: set[str]) -> list[str]:
    """Return short alert strings touching any of the given routes/agencies.

    Best-effort: any error (network, rate limit, shape change) yields an empty list
    so the fare detail card still renders.
    """
    try:
        entities = await _fetch_regional_alerts()
    except Exception:
        return []
    out: list[str] = []
    for ent in entities:
        informed = (ent.get("alert", {}) or {}).get("informed_entity", []) or []
        hit = any(
            (ie.get("route_id") in route_ids)
            or (ie.get("agency_id") in agency_ids)
            or (str(ie.get("route_id", "")).split(":")[0] in agency_ids)
            for ie in informed
        )
        if hit:
            text = _alert_text(ent)
            if text and text not in out:
                out.append(text)
    return out[:3]
