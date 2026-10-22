"""Conversion of query results into JSON-safe API payloads."""

import datetime
import decimal
import uuid
from typing import Any

from .executor import QueryResult

MAX_CELL_CHARS = 2000


def json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, decimal.Decimal):
        # Keep exact decimal text (e.g. money) instead of lossy floats.
        return str(value)
    if isinstance(value, (datetime.datetime, datetime.date, datetime.time)):
        return value.isoformat()
    if isinstance(value, datetime.timedelta):
        return str(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        return f"<binary {len(bytes(value))} bytes>"
    if isinstance(value, uuid.UUID):
        return str(value)
    text = str(value)
    return text if len(text) <= MAX_CELL_CHARS else text[: MAX_CELL_CHARS - 1] + "…"


def summarize(row_count: int, truncated: bool) -> str:
    if row_count == 0:
        return "No rows matched your question."
    noun = "row" if row_count == 1 else "rows"
    if truncated:
        return f"Showing the first {row_count} {noun} (results were truncated)."
    return f"Found {row_count} {noun}."


def format_result(
    result: QueryResult,
    tables: list[str],
    timings: dict[str, float],
    max_rows: int,
    include_records: bool = False,
) -> dict:
    rows = [[json_safe(v) for v in row] for row in result.rows]
    payload = {
        "sql": result.sql,
        "columns": result.columns,
        "rows": rows,
        "row_count": len(rows),
        "truncated": result.truncated,
        "max_rows": max_rows,
        "tables": tables,
        "execution_ms": result.duration_ms,
        "timings": {k: round(v, 1) for k, v in timings.items()},
        "summary": summarize(len(rows), result.truncated),
    }
    if include_records:
        # Backwards compatible shape of the original API: list of row objects.
        payload["result"] = [dict(zip(result.columns, row)) for row in rows]
    return payload
