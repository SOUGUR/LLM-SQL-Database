"""Composition root: wires pipeline components from settings.

Components are built once per process and create network clients lazily.
Tests replace the service with ``set_text_to_sql_service``.
"""

import threading

from .catalog import CatalogStore, allowed_tables
from .config import TextToSQLConfig, get_config
from .executor import MySQLExecutor
from .pipeline import TextToSQLService
from .retrieval import SchemaRetriever
from .sql_generation import SQLGenerator, build_llm
from .sql_validator import SQLValidator
from .vector_store import build_vector_index

_lock = threading.Lock()
_service: TextToSQLService | None = None


def build_text_to_sql_service(config: TextToSQLConfig | None = None) -> TextToSQLService:
    config = config or get_config()
    catalog_store = CatalogStore(config.SCHEMA_CATALOG_PATH)
    return TextToSQLService(
        retriever=SchemaRetriever(build_vector_index(config), catalog_store, config),
        generator=SQLGenerator(lambda: build_llm(config), config),
        validator=SQLValidator(
            allowed_tables=lambda: allowed_tables(catalog_store.get(), config),
            database_name=config.DB_NAME,
            dialect=config.SQL_DIALECT,
            max_rows=config.MAX_ROWS,
            default_limit=config.DEFAULT_LIMIT,
        ),
        executor=MySQLExecutor(config),
        config=config,
    )


def get_text_to_sql_service() -> TextToSQLService:
    global _service
    if _service is None:
        with _lock:
            if _service is None:
                _service = build_text_to_sql_service()
    return _service


def set_text_to_sql_service(service: TextToSQLService | None) -> None:
    global _service
    _service = service
