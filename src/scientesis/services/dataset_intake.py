from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import re
import uuid
from pathlib import Path

MAX_UPLOAD_BYTES = 10 * 1024 * 1024
SUPPORTED_EXTENSIONS = {".csv", ".json", ".md", ".markdown"}


def parse_expected_ranges(raw: str) -> dict[str, dict[str, float]]:
    try:
        value = json.loads(raw or "{}")
    except json.JSONDecodeError as error:
        raise ValueError(f"Expected ranges must be valid JSON: {error.msg}.") from error
    if not isinstance(value, dict):
        raise ValueError("Expected ranges must be a JSON object keyed by column name.")
    ranges = {}
    for column, bounds in value.items():
        if not isinstance(column, str) or not isinstance(bounds, dict) or not bounds:
            raise ValueError("Each range must map a column name to min and/or max numbers.")
        if set(bounds) - {"min", "max"}:
            raise ValueError(f"Range for {column!r} may contain only 'min' and 'max'.")
        parsed = {}
        for key, bound in bounds.items():
            if isinstance(bound, bool) or not isinstance(bound, (int, float)) or not math.isfinite(bound):
                raise ValueError(f"Range {key!r} for {column!r} must be a finite number.")
            parsed[key] = float(bound)
        if "min" in parsed and "max" in parsed and parsed["min"] > parsed["max"]:
            raise ValueError(f"Range minimum exceeds maximum for {column!r}.")
        ranges[column] = parsed
    return ranges


def inspect_upload(
    filename: str,
    content: bytes,
    expected_ranges: dict[str, dict[str, float]] | None = None,
) -> dict:
    suffix = Path(filename).suffix.lower()
    if suffix not in SUPPORTED_EXTENSIONS:
        raise ValueError("Upload a CSV, JSON, or Markdown file.")
    if not content:
        raise ValueError("The uploaded file is empty.")
    if len(content) > MAX_UPLOAD_BYTES:
        raise ValueError(f"The upload exceeds the {MAX_UPLOAD_BYTES // (1024 * 1024)} MiB MVP limit.")

    report = {
        "original_filename": Path(filename).name,
        "format": suffix.lstrip("."),
        "size_bytes": len(content),
        "sha256": hashlib.sha256(content).hexdigest(),
        "row_count": 0,
        "column_count": 0,
        "columns": [],
        "duplicate_rows": 0,
        "duplicate_keys": [],
        "row_width_mismatches": 0,
        "warnings": [],
        "errors": [],
    }
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        report["errors"].append(f"File is not valid UTF-8 text: {error.reason}.")
        return report

    ranges = expected_ranges or {}
    if suffix == ".csv":
        _inspect_csv(text, ranges, report)
    elif suffix == ".json":
        _inspect_json(text, ranges, report)
    else:
        _inspect_markdown(text, report)
    if report["duplicate_rows"]:
        report["warnings"].append(f"Found {report['duplicate_rows']} duplicate row(s); original bytes will be kept unchanged.")
    for key in report["duplicate_keys"]:
        report["errors"].append(f"JSON contains repeated object key {key!r}; review and correct it before intake.")
    for column in report["columns"]:
        if column.get("missing_count", 0):
            report["warnings"].append(f"Column {column['name']!r} has {column['missing_count']} missing value(s).")
        if column.get("out_of_range_count", 0):
            report["warnings"].append(
                f"Column {column['name']!r} has {column['out_of_range_count']} value(s) outside the supplied range."
            )
    if report["row_width_mismatches"]:
        report["warnings"].append(f"Found {report['row_width_mismatches']} row(s) whose field count differs from the header.")
    return report


def _inspect_csv(text: str, ranges: dict, report: dict) -> None:
    reader = csv.reader(io.StringIO(text, newline=""))
    try:
        headers = next(reader)
    except StopIteration:
        report["errors"].append("CSV has no header row.")
        return
    headers = [header.strip() for header in headers]
    if not headers or any(not header for header in headers):
        report["errors"].append("CSV headers must be non-empty.")
        return
    repeated = sorted({header for header in headers if headers.count(header) > 1})
    if repeated:
        report["errors"].append("CSV contains duplicate column names: " + ", ".join(repeated) + ".")
        return

    rows = list(reader)
    report["row_count"] = len(rows)
    report["column_count"] = len(headers)
    report["row_width_mismatches"] = sum(len(row) != len(headers) for row in rows)
    normalized = [row[:len(headers)] + [""] * max(0, len(headers) - len(row)) for row in rows]
    report["duplicate_rows"] = _duplicate_count(normalized)
    report["columns"] = _profile_columns(headers, normalized, ranges)
    unknown_ranges = set(ranges) - set(headers)
    if unknown_ranges:
        report["warnings"].append("No matching column for supplied range(s): " + ", ".join(sorted(unknown_ranges)) + ".")


def _inspect_json(text: str, ranges: dict, report: dict) -> None:
    repeated_keys = []

    def object_hook(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                repeated_keys.append(key)
            result[key] = value
        return result

    try:
        payload = json.loads(text, object_pairs_hook=object_hook)
    except json.JSONDecodeError as error:
        report["errors"].append(f"Invalid JSON at line {error.lineno}, column {error.colno}: {error.msg}.")
        return
    report["duplicate_keys"] = sorted(set(repeated_keys))

    if isinstance(payload, list):
        if payload and not all(isinstance(row, dict) for row in payload):
            report["errors"].append("JSON arrays must contain only objects to receive a tabular schema check.")
            report["row_count"] = len(payload)
            return
        rows = payload
    elif isinstance(payload, dict) and payload and all(isinstance(value, list) for value in payload.values()):
        lengths = {len(value) for value in payload.values()}
        if len(lengths) != 1:
            report["errors"].append("JSON columns-as-arrays must all have the same number of items.")
            return
        headers = list(payload)
        rows = [dict(zip(headers, values)) for values in zip(*(payload[key] for key in headers))]
    else:
        report["warnings"].append("JSON is valid but not a table-shaped array/object; only document-level checks were possible.")
        report["row_count"] = 1
        report["column_count"] = len(payload) if isinstance(payload, dict) else 1
        return

    headers = list(dict.fromkeys(key for row in rows for key in row))
    key_sets = {tuple(sorted(row)) for row in rows}
    if len(key_sets) > 1:
        report["warnings"].append("JSON records have inconsistent keys; absent fields are counted as missing values.")
    normalized = [[row.get(header) for header in headers] for row in rows]
    report["row_count"] = len(rows)
    report["column_count"] = len(headers)
    report["duplicate_rows"] = _duplicate_count(normalized)
    report["columns"] = _profile_columns(headers, normalized, ranges)
    unknown_ranges = set(ranges) - set(headers)
    if unknown_ranges:
        report["warnings"].append("No matching column for supplied range(s): " + ", ".join(sorted(unknown_ranges)) + ".")


def _inspect_markdown(text: str, report: dict) -> None:
    lines = text.splitlines()
    report["row_count"] = sum(bool(line.strip()) for line in lines)
    report["headings"] = sum(bool(re.match(r"^\s{0,3}#{1,6}\s+", line)) for line in lines)
    if not text.strip():
        report["errors"].append("Markdown document contains no text.")
    else:
        report["warnings"].append("Markdown is retained as a document; tabular type, missing-value, and range checks do not apply.")


def _profile_columns(headers: list[str], rows: list[list], ranges: dict) -> list[dict]:
    profiles = []
    for index, header in enumerate(headers):
        values = [row[index] if index < len(row) else None for row in rows]
        present = [value for value in values if value is not None and value != ""]
        types = sorted({_value_type(value) for value in present})
        numeric = [number for value in present if (number := _number(value)) is not None]
        bounds = ranges.get(header, {})
        out_of_range = sum(
            1 for number in numeric
            if ("min" in bounds and number < bounds["min"]) or ("max" in bounds and number > bounds["max"])
        )
        profiles.append({
            "name": str(header),
            "inferred_type": types[0] if len(types) == 1 else "mixed" if types else "empty",
            "missing_count": len(values) - len(present),
            "observed_min": min(numeric) if numeric else None,
            "observed_max": max(numeric) if numeric else None,
            "expected_range": bounds or None,
            "out_of_range_count": out_of_range,
        })
    return profiles


def _value_type(value) -> str:
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    text = str(value).strip()
    if text.lower() in {"true", "false"}:
        return "boolean"
    try:
        int(text)
        return "integer"
    except ValueError:
        try:
            float(text)
            return "number"
        except ValueError:
            return "string"


def _number(value):
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _duplicate_count(rows: list[list]) -> int:
    seen = set()
    duplicates = 0
    for row in rows:
        key = json.dumps(row, sort_keys=True, ensure_ascii=False, default=str)
        if key in seen:
            duplicates += 1
        seen.add(key)
    return duplicates


def save_reviewed_upload(
    repository,
    project_id: str,
    filename: str,
    content: bytes,
    report: dict,
    provenance: dict,
    reviewed: bool,
    storage_root: str | Path | None = None,
    expected_ranges: dict[str, dict[str, float]] | None = None,
) -> str:
    if not reviewed:
        raise ValueError("Review the inspection report and confirm before saving this dataset.")
    if report.get("errors"):
        raise ValueError("Resolve the blocking inspection errors before saving this dataset.")
    collection_method = provenance.get("collection_method") if isinstance(provenance, dict) else None
    if not isinstance(collection_method, str) or not collection_method.strip():
        raise ValueError("Record the data collection method before saving this dataset.")
    fresh_report = inspect_upload(filename, content, expected_ranges=expected_ranges)
    if fresh_report != report:
        raise ValueError("The file or inspection settings changed after review. Inspect it again before saving.")

    project_root = Path(__file__).resolve().parents[3]
    base = Path(storage_root).resolve() if storage_root else project_root / "data" / "uploads"
    base.mkdir(parents=True, exist_ok=True)
    safe_name = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(filename).name).strip("._") or "dataset"
    dataset_id = f"DATA-{uuid.uuid4().hex[:10].upper()}"
    destination = base / f"{dataset_id}-{safe_name[:120]}"
    temporary = destination.with_name(destination.name + ".tmp")
    try:
        temporary.write_bytes(content)
        temporary.replace(destination)
        relative_uri = str(destination.relative_to(base.parent))
        record = {
            "id": dataset_id,
            "project_id": project_id,
            "name": Path(filename).name,
            "storage_uri": relative_uri,
            "source_type": "human_upload",
            "provenance": {**provenance, "original_filename": Path(filename).name},
            "validation_report": report,
            "sha256": report["sha256"],
            "uploaded_by": "scientist",
        }
        repository.add_dataset(record)
        return dataset_id
    except Exception:
        temporary.unlink(missing_ok=True)
        destination.unlink(missing_ok=True)
        raise
