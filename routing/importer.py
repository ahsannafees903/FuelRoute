"""Fuel station CSV data ingestion pipeline.

Authoritative source: OPIS truckstop price CSV data feed.
Coordinates: Offline fallback from us_cities.csv (city-centre centroids).
Duplicate policy: Deterministically keep the lowest observed price per OPIS ID
(reflecting the lowest cost-of-entry fuel tier at that truckstop), breaking ties
with the most descriptive station name and earliest file order. Never invent or average prices.
"""

from __future__ import annotations

import csv
import io
import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple, Union

from django.db import transaction

from .models import Station
from .services import DATA, STATES, gazetteer, norm

REQUIRED_COLUMNS = [
    "OPIS Truckstop ID",
    "Truckstop Name",
    "Address",
    "City",
    "State",
    "Rack ID",
    "Retail Price",
]

DEFAULT_CSV_PATH = DATA / "fuel-prices-for-be-assessment.csv"
FALLBACK_CSV_PATH = DATA / "fuel_prices.csv"

CANADIAN_PROVINCES = {
    "AB", "BC", "MB", "NB", "NL", "NS", "NT", "NU", "ON", "PE", "QC", "SK", "YT"
}
US_STATES = set(STATES.values())


class DataPipelineError(ValueError):
    """Base error for data pipeline issues."""
    pass


class HeaderValidationError(DataPipelineError):
    """Raised when CSV headers are missing required columns."""
    pass


@dataclass
class SkippedRow:
    row_number: int
    reason: str
    raw_id: str
    name: str
    city: str
    state: str
    raw_price: str
    details: str = ""


@dataclass
class ImportStats:
    total_rows_read: int = 0
    valid_us_rows: int = 0
    unique_us_stations_found: int = 0
    canadian_rows_skipped: int = 0
    unknown_state_rows_skipped: int = 0
    invalid_price_rows_skipped: int = 0
    invalid_id_rows_skipped: int = 0
    missing_fields_rows_skipped: int = 0
    unmatched_us_stations_skipped: int = 0
    loaded_stations_count: int = 0
    multi_record_stations_count: int = 0
    skipped_rows: List[SkippedRow] = field(default_factory=list)
    unmatched_stations: List[Dict[str, str]] = field(default_factory=list)

    @property
    def total_skipped_rows(self) -> int:
        return (
            self.canadian_rows_skipped
            + self.unknown_state_rows_skipped
            + self.invalid_price_rows_skipped
            + self.invalid_id_rows_skipped
            + self.missing_fields_rows_skipped
        )

    def summary(self) -> str:
        lines = [
            f"Total rows read: {self.total_rows_read}",
            f"Valid US price rows: {self.valid_us_rows}",
            f"Unique US truckstops: {self.unique_us_stations_found} (with multiple price observations: {self.multi_record_stations_count})",
            f"Successfully loaded: {self.loaded_stations_count}",
            f"Skipped Canadian rows: {self.canadian_rows_skipped}",
            f"Skipped unknown territories/states: {self.unknown_state_rows_skipped}",
            f"Skipped invalid prices: {self.invalid_price_rows_skipped}",
            f"Skipped invalid IDs: {self.invalid_id_rows_skipped}",
            f"Skipped missing fields: {self.missing_fields_rows_skipped}",
            f"Unmatched US cities (no coordinates in gazetteer): {self.unmatched_us_stations_skipped}",
        ]
        return "\n".join(lines)


def get_default_csv_path() -> Path:
    """Return the authoritative CSV path if present, otherwise fallback."""
    if DEFAULT_CSV_PATH.is_file():
        return DEFAULT_CSV_PATH
    if FALLBACK_CSV_PATH.is_file():
        return FALLBACK_CSV_PATH
    return DEFAULT_CSV_PATH


def validate_headers(fieldnames: Optional[List[str]]) -> None:
    """Ensure all required columns are present in the CSV header."""
    if not fieldnames:
        raise HeaderValidationError("CSV file is empty or missing header row.")
    normalized_fields = {f.strip() for f in fieldnames if f}
    missing = [col for col in REQUIRED_COLUMNS if col not in normalized_fields]
    if missing:
        raise HeaderValidationError(
            f"CSV is missing required columns: {', '.join(missing)}. "
            f"Expected: {', '.join(REQUIRED_COLUMNS)}"
        )


def parse_and_validate_row(
    row: Dict[str, str], row_num: int, stats: ImportStats
) -> Optional[Dict]:
    """Validate a single CSV row against business and data rules.

    Returns cleaned record dict if valid US candidate, or None if skipped.
    """
    raw_id = (row.get("OPIS Truckstop ID") or "").strip()
    raw_name = (row.get("Truckstop Name") or "").strip()
    raw_addr = (row.get("Address") or "").strip()
    raw_city = (row.get("City") or "").strip()
    raw_state = (row.get("State") or "").strip().upper()
    raw_rack = (row.get("Rack ID") or "").strip()
    raw_price = (row.get("Retail Price") or "").strip()

    # 1. Missing required string fields
    if not (raw_id and raw_name and raw_addr and raw_city and raw_state and raw_price):
        stats.missing_fields_rows_skipped += 1
        stats.skipped_rows.append(
            SkippedRow(
                row_number=row_num,
                reason="MISSING_REQUIRED_FIELDS",
                raw_id=raw_id,
                name=raw_name,
                city=raw_city,
                state=raw_state,
                raw_price=raw_price,
                details="One or more required fields were empty.",
            )
        )
        return None

    # 2. Parse and validate OPIS ID
    try:
        opis_id = int(raw_id)
        if opis_id <= 0:
            raise ValueError("ID must be positive")
    except ValueError:
        stats.invalid_id_rows_skipped += 1
        stats.skipped_rows.append(
            SkippedRow(
                row_number=row_num,
                reason="INVALID_OPIS_ID",
                raw_id=raw_id,
                name=raw_name,
                city=raw_city,
                state=raw_state,
                raw_price=raw_price,
                details=f"Invalid OPIS ID: '{raw_id}' (must be a positive integer).",
            )
        )
        return None

    # 3. Parse and validate price
    try:
        price = float(raw_price)
        if not math.isfinite(price) or price <= 0.0:
            raise ValueError("Price must be finite positive number")
    except ValueError:
        stats.invalid_price_rows_skipped += 1
        stats.skipped_rows.append(
            SkippedRow(
                row_number=row_num,
                reason="INVALID_PRICE",
                raw_id=raw_id,
                name=raw_name,
                city=raw_city,
                state=raw_state,
                raw_price=raw_price,
                details=f"Invalid price: '{raw_price}' (must be a positive decimal number).",
            )
        )
        return None

    # 4. Explicit Country / State Validation
    if raw_state in CANADIAN_PROVINCES:
        stats.canadian_rows_skipped += 1
        stats.skipped_rows.append(
            SkippedRow(
                row_number=row_num,
                reason="CANADIAN_LOCATION",
                raw_id=raw_id,
                name=raw_name,
                city=raw_city,
                state=raw_state,
                raw_price=raw_price,
                details=f"Canadian province '{raw_state}' excluded (USA route planner).",
            )
        )
        return None

    if raw_state not in US_STATES:
        stats.unknown_state_rows_skipped += 1
        stats.skipped_rows.append(
            SkippedRow(
                row_number=row_num,
                reason="UNKNOWN_TERRITORY",
                raw_id=raw_id,
                name=raw_name,
                city=raw_city,
                state=raw_state,
                raw_price=raw_price,
                details=f"State code '{raw_state}' is not a recognized US state or territory.",
            )
        )
        return None

    stats.valid_us_rows += 1
    return {
        "opis_id": opis_id,
        "name": raw_name,
        "address": raw_addr,
        "city": raw_city,
        "state": raw_state,
        "rack_id": raw_rack,
        "price": price,
        "row_num": row_num,
    }


def resolve_duplicates(valid_rows: List[Dict]) -> Tuple[List[Dict], int]:
    """Group valid US records by OPIS ID and apply deterministic duplicate policy.

    Policy:
    - Retain the exact lowest observed retail price (representing the entry-level fuel grade/tier).
    - If prices are tied, pick the candidate with the longest name (most complete description),
      and then earliest file order.
    - Never invent, round, or average supplied prices.
    - Track the count of raw price observations for that station.
    """
    groups: Dict[int, List[Dict]] = {}
    for r in valid_rows:
        groups.setdefault(r["opis_id"], []).append(r)

    multi_count = sum(1 for g in groups.values() if len(g) > 1)
    resolved: List[Dict] = []

    for opis_id, group in groups.items():
        # Deterministic sorting:
        # 1. Price ascending (lowest price first)
        # 2. Name length descending (longer, more descriptive name preferred on tie)
        # 3. Row number ascending (earliest occurrence preferred)
        best = min(
            group,
            key=lambda x: (x["price"], -len(x["name"]), x["row_num"]),
        )
        resolved.append({
            "opis_id": opis_id,
            "name": best["name"],
            "address": best["address"],
            "city": best["city"],
            "state": best["state"],
            "rack_id": best["rack_id"],
            "price": best["price"],
            "raw_record_count": len(group),
        })

    return resolved, multi_count


def import_stations_from_csv(
    csv_source: Union[str, Path, io.TextIOBase],
    gaz: Optional[Dict[Tuple[str, str], Tuple[float, float]]] = None,
    dry_run: bool = False,
    clear_existing: bool = True,
) -> ImportStats:
    """Safely and repeatably import stations from a CSV source.

    Wraps database operations in a transaction. If an error occurs, the database
    is rolled back and remains unmodified.
    """
    stats = ImportStats()
    if gaz is None:
        gaz = gazetteer()

    if isinstance(csv_source, (str, Path)):
        p = Path(csv_source)
        if not p.is_file():
            raise FileNotFoundError(f"Fuel price CSV not found at '{p}'.")
        f = open(p, newline="", encoding="utf-8-sig")
        should_close = True
    else:
        f = csv_source
        should_close = False

    valid_records: List[Dict] = []
    try:
        reader = csv.DictReader(f)
        validate_headers(reader.fieldnames)
        for row_num, row in enumerate(reader, start=2):
            stats.total_rows_read += 1
            cleaned = parse_and_validate_row(row, row_num, stats)
            if cleaned:
                valid_records.append(cleaned)
    finally:
        if should_close:
            f.close()

    resolved_stations, multi_count = resolve_duplicates(valid_records)
    stats.unique_us_stations_found = len(resolved_stations)
    stats.multi_record_stations_count = multi_count

    # Coordinate Matching using city-centre gazetteer fallback
    station_instances: List[Station] = []
    for s in resolved_stations:
        key = (norm(s["city"]), s["state"])
        pt = gaz.get(key)
        if not pt:
            stats.unmatched_us_stations_skipped += 1
            stats.unmatched_stations.append({
                "opis_id": s["opis_id"],
                "name": s["name"],
                "address": s["address"],
                "city": s["city"],
                "state": s["state"],
            })
            continue

        station_instances.append(
            Station(
                opis_id=s["opis_id"],
                name=s["name"],
                address=s["address"],
                city=s["city"],
                state=s["state"],
                price=s["price"],
                lat=pt[0],
                lon=pt[1],
                rack_id=s["rack_id"],
                raw_record_count=s["raw_record_count"],
                is_exact_coord=False,
                coord_source="city_centre_fallback",
            )
        )

    stats.loaded_stations_count = len(station_instances)

    # Atomic DB persistence
    if not dry_run:
        with transaction.atomic():
            if clear_existing:
                Station.objects.all().delete()
            Station.objects.bulk_create(station_instances, batch_size=1000)

    return stats

