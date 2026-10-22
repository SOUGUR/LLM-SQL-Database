"""Execution of validated SQL against the target MySQL database.

Safety measures applied here (on top of SQL validation):
* the session and transaction are READ ONLY and always rolled back,
* a server-side statement timeout plus client socket timeouts,
* multi-statement and LOCAL INFILE support are disabled on the connection,
* at most ``MAX_ROWS + 1`` rows are fetched (the extra row detects truncation).
"""

import logging
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Callable

import pymysql

from .config import TextToSQLConfig
from .exceptions import QueryExecutionError, QueryTimeoutError, UnsafeSQLError, UpstreamServiceError
from .sql_validator import ValidatedSQL

logger = logging.getLogger(__name__)

# MySQL / MariaDB error codes
ER_QUERY_TIMEOUT = 3024          # MAX_EXECUTION_TIME exceeded (MySQL)
ER_STATEMENT_TIMEOUT = 1969      # max_statement_time exceeded (MariaDB)
ER_QUERY_INTERRUPTED = 1317
CR_SERVER_LOST = 2013            # lost connection during query (socket read timeout)
ER_READ_ONLY_TRANSACTION = 1792
CONNECTION_ERRORS = {1045, 1049, 2002, 2003, 2005, 2006}


@dataclass
class QueryResult:
    columns: list[str]
    rows: list[list[Any]]
    truncated: bool = False
    duration_ms: float = 0.0
    sql: str = ""
    tables: tuple[str, ...] = field(default_factory=tuple)


def _dedupe(names: list[str]) -> list[str]:
    seen: dict[str, int] = {}
    result = []
    for name in names:
        if name in seen:
            seen[name] += 1
            result.append(f"{name}_{seen[name]}")
        else:
            seen[name] = 1
            result.append(name)
    return result


class MySQLExecutor:
    def __init__(self, config: TextToSQLConfig, connect: Callable[..., Any] = pymysql.connect):
        self.config = config
        self._connect = connect

    @contextmanager
    def connection(self):
        timeout = self.config.QUERY_TIMEOUT_SECONDS
        try:
            conn = self._connect(
                host=self.config.DB_HOST,
                port=int(self.config.DB_PORT),
                user=self.config.DB_USER,
                password=self.config.DB_PASSWORD,
                database=self.config.DB_NAME,
                charset="utf8mb4",
                connect_timeout=5,
                read_timeout=timeout + 5,
                write_timeout=timeout,
                autocommit=False,
                client_flag=0,
                local_infile=False,
            )
        except pymysql.err.MySQLError as e:
            logger.error("Database connection failed: %s", e)
            raise UpstreamServiceError(f"connect failed: {e}") from e
        try:
            yield conn
        finally:
            try:
                conn.close()
            except Exception:  # pragma: no cover - best effort
                pass

    def apply_session_guards(self, cursor) -> None:
        timeout = self.config.QUERY_TIMEOUT_SECONDS
        cursor.execute("SET SESSION TRANSACTION READ ONLY")
        try:
            cursor.execute("SET SESSION MAX_EXECUTION_TIME = %s", (timeout * 1000,))
        except pymysql.err.MySQLError:
            # MariaDB uses a different variable (in seconds).
            try:
                cursor.execute("SET SESSION max_statement_time = %s", (timeout,))
            except pymysql.err.MySQLError:
                logger.warning("Server-side statement timeout unsupported; relying on socket timeout.")

    def execute(self, validated: ValidatedSQL) -> QueryResult:
        max_rows = self.config.MAX_ROWS
        with self.connection() as conn:
            try:
                with conn.cursor() as cur:
                    self.apply_session_guards(cur)
                    cur.execute("START TRANSACTION READ ONLY")
                    started = time.perf_counter()
                    cur.execute(validated.sql)
                    fetched = list(cur.fetchmany(max_rows + 1))
                    duration_ms = (time.perf_counter() - started) * 1000
                    columns = _dedupe([d[0] for d in (cur.description or [])])
                conn.rollback()
            except pymysql.err.MySQLError as e:
                raise self._translate_error(e) from e

        truncated = len(fetched) > max_rows
        return QueryResult(
            columns=columns,
            rows=[list(r) for r in fetched[:max_rows]],
            truncated=truncated,
            duration_ms=round(duration_ms, 2),
            sql=validated.sql,
            tables=validated.tables,
        )

    @staticmethod
    def _translate_error(error: Exception) -> Exception:
        code = error.args[0] if error.args and isinstance(error.args[0], int) else None
        logger.warning("Query failed (code=%s): %s", code, error)
        if code in (ER_QUERY_TIMEOUT, ER_STATEMENT_TIMEOUT, ER_QUERY_INTERRUPTED, CR_SERVER_LOST):
            return QueryTimeoutError(str(error))
        if code == ER_READ_ONLY_TRANSACTION:
            return UnsafeSQLError(str(error), "The database rejected the query because it is read-only.")
        if code in CONNECTION_ERRORS:
            return UpstreamServiceError(str(error))
        return QueryExecutionError(str(error))
