"""Text-to-SQL orchestration: retrieve -> generate -> validate -> execute -> format.

Each component is injected, so tests (and alternative backends) can swap any
stage. ``stream`` yields progress events for the UI; ``run`` returns only the
final payload.
"""

import logging
import time
import unicodedata
from typing import Iterator

from .config import TextToSQLConfig
from .exceptions import InvalidQuestionError, TextToSQLError
from .executor import MySQLExecutor
from .formatting import format_result
from .retrieval import SchemaRetriever
from .sql_generation import SQLGenerator
from .sql_validator import SQLValidator

logger = logging.getLogger(__name__)

STAGES = {
    "retrieving": "Finding relevant tables",
    "generating": "Writing SQL",
    "validating": "Checking the query is safe",
    "executing": "Running the query",
}


def clean_question(question, max_chars: int) -> str:
    if not isinstance(question, str):
        raise InvalidQuestionError("question not a string")
    # Drop control characters (keep newlines/tabs) and normalise whitespace.
    question = "".join(
        ch for ch in question if ch in "\n\t" or unicodedata.category(ch)[0] != "C"
    ).strip()
    if not question:
        raise InvalidQuestionError("empty question", "Please enter a question.")
    if len(question) > max_chars:
        raise InvalidQuestionError(
            "question too long", f"Questions are limited to {max_chars} characters."
        )
    return question


class TextToSQLService:
    def __init__(
        self,
        retriever: SchemaRetriever,
        generator: SQLGenerator,
        validator: SQLValidator,
        executor: MySQLExecutor,
        config: TextToSQLConfig,
    ):
        self.retriever = retriever
        self.generator = generator
        self.validator = validator
        self.executor = executor
        self.config = config

    def stream(
        self, question: str, history: list[dict] | None = None, include_records: bool = False
    ) -> Iterator[dict]:
        question = clean_question(question, self.config.MAX_QUESTION_CHARS)
        history = (history or [])[-self.config.HISTORY_TURNS:] if self.config.HISTORY_TURNS else []
        timings: dict[str, float] = {}
        started = time.perf_counter()

        def stage(name):
            return {"event": "stage", "stage": name, "label": STAGES[name]}

        yield stage("retrieving")
        t = time.perf_counter()
        retrieval = self.retriever.retrieve(question)
        timings["retrieval_ms"] = (time.perf_counter() - t) * 1000
        logger.info("Retrieved tables (%s): %s", retrieval.source, ", ".join(retrieval.tables))

        yield stage("generating")
        t = time.perf_counter()
        candidate = self.generator.generate(question, retrieval.context, history)
        timings["generation_ms"] = (time.perf_counter() - t) * 1000

        yield stage("validating")
        try:
            validated = self.validator.validate(candidate)
        except TextToSQLError as e:
            e.sql = candidate
            logger.warning("Rejected generated SQL (%s): %s", e.detail, candidate)
            raise

        yield stage("executing")
        try:
            result = self.executor.execute(validated)
        except TextToSQLError as e:
            e.sql = validated.sql
            raise
        timings["execution_ms"] = result.duration_ms
        timings["total_ms"] = (time.perf_counter() - started) * 1000

        payload = format_result(
            result,
            tables=list(validated.tables),
            timings=timings,
            max_rows=self.config.MAX_ROWS,
            include_records=include_records,
        )
        payload["question"] = question
        yield {"event": "result", "data": payload}

    def run(self, question: str, history: list[dict] | None = None, include_records: bool = False) -> dict:
        payload = None
        for event in self.stream(question, history, include_records=include_records):
            if event["event"] == "result":
                payload = event["data"]
        return payload
