"""Backwards-compatible helpers; prefer ``SQLValidator`` and ``MySQLExecutor``."""

from .catalog import CatalogStore, allowed_tables
from .config import get_config
from .exceptions import UnsafeSQLError
from .executor import MySQLExecutor
from .sql_validator import SQLValidator, ValidatedSQL


def _validator() -> SQLValidator:
    config = get_config()
    store = CatalogStore(config.SCHEMA_CATALOG_PATH)
    return SQLValidator(
        allowed_tables=lambda: allowed_tables(store.get(), config),
        database_name=config.DB_NAME,
        dialect=config.SQL_DIALECT,
        max_rows=config.MAX_ROWS,
        default_limit=config.DEFAULT_LIMIT,
    )


def validate_sql(sql: str):
    """Return ``(is_safe, reason)`` like the original helper."""
    try:
        _validator().validate(sql)
        return True, ""
    except UnsafeSQLError as e:
        return False, e.public_message


def execute_mysql_query(sql: str):
    """Validate, then execute ``sql``; returns a list of row dicts."""
    validated: ValidatedSQL = _validator().validate(sql)
    result = MySQLExecutor(get_config()).execute(validated)
    return [dict(zip(result.columns, row)) for row in result.rows]
