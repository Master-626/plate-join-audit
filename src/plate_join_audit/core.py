"""The audit never joins tables or changes scientific measurements."""

from __future__ import annotations

import csv
import hashlib
import io
import os
import re
import stat
from collections import Counter, defaultdict
from pathlib import Path

GEOMETRIES = {6: (2, 3), 12: (3, 4), 24: (4, 6), 48: (6, 8), 96: (8, 12), 384: (16, 24)}
MAX_BYTES = 16 * 1024 * 1024


class AuditError(ValueError):
    """Invalid file/schema/options; no audit verdict is available."""


def normalize_well(value: str, size: int = 96) -> str:
    """Canonicalize ASCII A01/a1 to A1; reject out-of-geometry coordinates."""
    if type(size) is not int or size not in GEOMETRIES:
        raise AuditError("plate_size must be one of 6, 12, 24, 48, 96, 384")
    if not isinstance(value, str):
        raise AuditError("well must be text")
    match = re.fullmatch(r"([A-Za-z])0*([1-9][0-9]*)", value.strip())
    if not match:
        raise AuditError("well must be a row letter followed by a positive column number")
    row = match[1].upper()
    # Avoid converting unbounded digit sequences, even in hostile input.
    if len(match[2]) > 2:
        raise AuditError("well is outside the selected plate geometry")
    col = int(match[2])
    rows, cols = GEOMETRIES[size]
    if ord(row) - ord("A") >= rows or col > cols:
        raise AuditError("well is outside the selected plate geometry")
    return f"{row}{col}"


def _load(path: str | Path, delimiter: str, required: list[str]) -> tuple[list, dict]:
    if delimiter not in (",", "\t"):
        raise AuditError("delimiter must be comma or tab")
    if any(not isinstance(x, str) or not x for x in required):
        raise AuditError("mapped column names must be nonempty text")
    if len(set(required)) != len(required):
        raise AuditError("mapped column names must be nonempty and distinct")
    p = Path(path)
    try:
        flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0)
        fd = os.open(p, flags)
        with os.fdopen(fd, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise AuditError(f"{p.name}: only regular files are supported")
            # O_NONBLOCK avoids hanging on a FIFO at open. On POSIX, clear it
            # for regular files. read() may still return before the full size.
            if os.name == "posix" and hasattr(os, "set_blocking"):
                os.set_blocking(stream.fileno(), True)
            data = _read_bounded(stream, MAX_BYTES + 1)
    except OSError as exc:
        raise AuditError(f"cannot read {p.name}: {exc.strerror}") from exc
    if len(data) > MAX_BYTES:
        raise AuditError(f"{p.name}: file exceeds 16 MiB limit")
    try:
        text = data.decode("utf-8-sig")
    except UnicodeError as exc:
        raise AuditError(f"{p.name}: expected UTF-8 text") from exc
    if "\x00" in text:
        raise AuditError(f"{p.name}: NUL characters are not supported")
    reader = csv.reader(io.StringIO(text, newline=""), delimiter=delimiter, strict=True)
    records = []
    try:
        header = next(reader, None)
        if not header or any(not x.strip() for x in header) or len(set(header)) != len(header):
            raise AuditError(f"{p.name}: missing, empty or duplicate header")
        absent = set(required) - set(header)
        if absent:
            raise AuditError(f"{p.name}: missing required columns {sorted(absent)}")
        for index, values in enumerate(reader, start=1):
            if not any(cell.strip() for cell in values):
                raise AuditError(f"{p.name}: record {index} is a blank row")
            if len(values) != len(header):
                raise AuditError(f"{p.name}: record {index} has wrong number of columns")
            records.append((index, dict(zip(header, values))))
    except csv.Error as exc:
        raise AuditError(
            f"{p.name}: invalid CSV near physical line {reader.line_num}: {exc}"
        ) from exc
    return records, {
        "filename": p.name,
        "sha256": hashlib.sha256(data).hexdigest(),
        "bytes": len(data),
        "records": len(records),
    }


def _read_bounded(stream, limit):
    """Read until EOF or limit. A single read() may return early."""
    chunks = []
    remaining = limit
    while remaining > 0:
        block = stream.read(remaining)
        if not block:
            break
        chunks.append(block)
        remaining -= len(block)
    return b"".join(chunks)


def _well_sort_key(plate, well):
    return (plate, well[0], int(well[1:]))


def _ordered(keys):
    return sorted(keys, key=lambda key: _well_sort_key(key[0], key[1]))


def audit(
    layout: str | Path,
    readings: str | Path,
    *,
    plate_size: int = 96,
    layout_plate: str = "plate",
    layout_well: str = "well",
    readings_plate: str = "plate",
    readings_well: str = "well",
    observation_columns: tuple[str, ...] = (),
    layout_delimiter: str = ",",
    readings_delimiter: str = ",",
) -> dict:
    """Check keys without joining. Repeated wells need explicit observation columns.

    A pass covers key integrity and planned-well presence, not scientific validity,
    numeric measurements, or completeness of time/channel combinations.
    noncanonical_wells lists raw tokens that are not already canonical; those rows
    still count as matches.
    """
    normalize_well("A1", plate_size)
    if isinstance(observation_columns, str):
        raise AuditError("observation_columns must be a sequence of column names, not text")
    try:
        obs = list(observation_columns)
    except TypeError as exc:
        raise AuditError("observation_columns must be a sequence of column names") from exc
    if any(not isinstance(x, str) for x in obs):
        raise AuditError("observation column names must be text")
    left, left_meta = _load(layout, layout_delimiter, [layout_plate, layout_well])
    right, right_meta = _load(readings, readings_delimiter, [readings_plate, readings_well, *obs])
    issues = []

    def issue(code, table, records, **details):
        issues.append({"code": code, "table": table, "records": records, **details})

    def keys(rows, table, plate_col, well_col):
        found = []
        aliases = defaultdict(list)
        if not rows:
            issue("empty_table", table, [])
        for index, row in rows:
            plate = row[plate_col]
            # Never silently coalesce plate identifiers.
            if not plate or plate != plate.strip():
                issue("invalid_plate", table, [index], raw_plate=plate)
                continue
            raw_well = row[well_col]
            try:
                well = normalize_well(raw_well, plate_size)
            except AuditError as exc:
                issue("invalid_well", table, [index], raw_well=raw_well, reason=str(exc))
                continue
            if raw_well != well:
                aliases[(plate, well, raw_well)].append(index)
            found.append((index, (plate, well), row))
        return found, aliases

    def spelling_entries(table, aliases):
        return [
            {
                "table": table,
                "plate": plate,
                "well": well,
                "raw_well": raw_well,
                "records": list(records),
            }
            for (plate, well, raw_well), records in aliases.items()
        ]

    lkeys, layout_aliases = keys(left, "layout", layout_plate, layout_well)
    rkeys, reading_aliases = keys(right, "readings", readings_plate, readings_well)
    noncanonical = sorted(
        [
            *spelling_entries("layout", layout_aliases),
            *spelling_entries("readings", reading_aliases),
        ],
        key=lambda entry: (
            0 if entry["table"] == "layout" else 1,
            *_well_sort_key(entry["plate"], entry["well"]),
            entry["raw_well"],
        ),
    )
    layouts = defaultdict(list)
    measurements = defaultdict(list)
    for index, key, row in lkeys:
        layouts[key].append(index)
    for key in _ordered(layouts):
        if len(layouts[key]) > 1:
            issue("duplicate_layout_well", "layout", layouts[key], plate=key[0], well=key[1])
    for index, key, row in rkeys:
        values = tuple(row[c] for c in obs)
        if any(not v or v != v.strip() for v in values):
            issue("invalid_observation", "readings", [index], plate=key[0], well=key[1])
            continue
        measurements[(*key, *values)].append(index)
    for key, indices in sorted(
        measurements.items(),
        key=lambda item: (*_well_sort_key(item[0][0], item[0][1]), item[0][2:]),
    ):
        if len(indices) > 1:
            issue(
                "duplicate_observation",
                "readings",
                indices,
                plate=key[0],
                well=key[1],
                observation=dict(zip(obs, key[2:])),
            )
    # Coverage includes only rows with valid coordinate AND observation keys.
    by_well = defaultdict(list)
    for key, indices in measurements.items():
        by_well[key[:2]].extend(indices)
    seen = set(by_well)
    for key in _ordered(set(layouts) - seen):
        issue("missing_well", "readings", [], plate=key[0], well=key[1])
    for key in _ordered(seen - set(layouts)):
        indices = sorted(by_well[key])
        issue("unexpected_well", "readings", indices, plate=key[0], well=key[1])
    counts = Counter(i["code"] for i in issues)
    return {
        "schema_version": 1,
        "ok": not issues,
        "scope": "join-key integrity and planned-well presence only",
        "options": {
            "plate_size": plate_size,
            "layout_plate": layout_plate,
            "layout_well": layout_well,
            "readings_plate": readings_plate,
            "readings_well": readings_well,
            "observation_columns": obs,
            "layout_delimiter": layout_delimiter,
            "readings_delimiter": readings_delimiter,
        },
        "sources": {"layout": left_meta, "readings": right_meta},
        "summary": {
            "planned_unique_wells": len(layouts),
            "observed_unique_wells": len(seen),
            "matched_unique_wells": len(set(layouts) & seen),
            "noncanonical_rows": sum(len(entry["records"]) for entry in noncanonical),
            "issues_by_code": dict(sorted(counts.items())),
        },
        "noncanonical_wells": noncanonical,
        "issues": issues,
    }
