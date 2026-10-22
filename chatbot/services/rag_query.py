"""Backwards-compatible entry point for the Text-to-SQL pipeline.

The implementation now lives in dedicated modules (retrieval, sql_generation,
sql_validator, executor, pipeline); see ``container.py`` for the wiring.
"""

from .container import get_text_to_sql_service


def process_text_to_sql(question: str, history: list[dict] | None = None) -> dict:
    return get_text_to_sql_service().run(question, history, include_records=True)
