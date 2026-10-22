"""Validation of LLM-generated SQL.

LLM output is untrusted input. A query is only executed if it parses with a
real SQL parser into exactly one read-only query that touches allowed tables
and no dangerous functions. The SQL that is executed is *regenerated from the
parsed AST*, so comments (including MySQL ``/*! ... */`` executable comments)
and anything the parser did not understand never reach the database.

This is defence in depth: the database user must also be read-only (see
README, "Least-privilege database user").
"""

import logging
from dataclasses import dataclass
from typing import Callable, Iterable

import sqlglot
from sqlglot import exp
from sqlglot.errors import SqlglotError

from .exceptions import UnsafeSQLError

logger = logging.getLogger(__name__)


def _expression_types(*names: str) -> tuple[type, ...]:
    # Resolve by name so the module works across sqlglot versions.
    return tuple(t for t in (getattr(exp, n, None) for n in names) if isinstance(t, type))


ALLOWED_ROOT_TYPES = _expression_types("Select", "SetOperation", "Union", "Intersect", "Except", "Subquery")

FORBIDDEN_NODE_TYPES = _expression_types(
    # Writes / DDL / admin
    "Insert", "Update", "Delete", "Merge", "Create", "Drop", "Alter", "AlterTable",
    "TruncateTable", "Grant", "Revoke", "Command", "LoadData", "Copy", "Kill",
    "Analyze", "Cache", "Uncache", "Refresh", "Pragma", "Use", "Describe", "Show",
    # Session / transaction control
    "Set", "SetItem", "Transaction", "Commit", "Rollback",
    # SELECT ... INTO, row locks
    "Into", "Lock",
    # Variables: @var, @@system_var, @var := value, bind placeholders
    "Parameter", "SessionParameter", "PropertyEQ", "Placeholder",
    # sqlglot-known functions that leak server/session information
    "CurrentUser",
)

# Functions that can sleep, lock, read files, leak server information or be
# used for error-based exfiltration. Names are matched case-insensitively.
FORBIDDEN_FUNCTIONS = frozenset(
    name.upper()
    for name in """
    sleep benchmark load_file get_lock release_lock release_all_locks is_free_lock
    is_used_lock master_pos_wait source_pos_wait wait_for_executed_gtid_set
    wait_until_sql_thread_after_gtids sys_exec sys_eval extractvalue updatexml
    name_const user current_user session_user system_user current_role database
    schema current_schema version connection_id last_insert_id row_count
    found_rows statement_digest statement_digest_text validate_password_strength
    roles_graphml ps_current_thread_id ps_thread_id gtid_subset gtid_subtract
    """.split()
)

VARIABLE_NODE_NAMES = frozenset({"Parameter", "SessionParameter", "PropertyEQ", "Placeholder"})
VARIABLE_NODE_LABEL = "variables or placeholders"

MAX_SQL_CHARS = 20000


@dataclass(frozen=True)
class ValidatedSQL:
    sql: str
    tables: tuple[str, ...]
    limit: int


def _function_name(node: exp.Func) -> str:
    if isinstance(node, exp.Anonymous):
        return str(node.name or "").upper()
    try:
        return node.sql_name().upper()
    except Exception:  # pragma: no cover - defensive
        return type(node).__name__.upper()


class SQLValidator:
    def __init__(
        self,
        allowed_tables: Iterable[str] | Callable[[], Iterable[str]],
        database_name: str = "",
        dialect: str = "mysql",
        max_rows: int = 500,
        default_limit: int = 100,
    ):
        self._allowed_tables = allowed_tables
        self.database_name = (database_name or "").lower()
        self.dialect = dialect
        self.max_rows = max_rows
        self.default_limit = min(default_limit, max_rows)

    def allowed(self) -> set[str]:
        source = self._allowed_tables() if callable(self._allowed_tables) else self._allowed_tables
        return {t.lower() for t in source}

    def validate(self, sql: str) -> ValidatedSQL:
        sql = (sql or "").strip()
        if not sql:
            raise UnsafeSQLError("empty SQL", "No SQL query was generated.")
        if len(sql) > MAX_SQL_CHARS:
            raise UnsafeSQLError("SQL too long")

        root = self._parse_single(sql)
        if not isinstance(root, ALLOWED_ROOT_TYPES):
            raise UnsafeSQLError(
                f"root type {type(root).__name__}", "Only read-only SELECT queries are allowed."
            )

        self._check_nodes(root)
        tables = self._check_tables(root)
        root, limit = self._enforce_limit(root)

        safe_sql = root.sql(dialect=self.dialect, comments=False)
        # Re-parse the regenerated SQL as a final consistency check.
        self._parse_single(safe_sql)
        return ValidatedSQL(sql=safe_sql, tables=tuple(sorted(tables)), limit=limit)

    # -- steps ---------------------------------------------------------------

    def _parse_single(self, sql: str) -> exp.Expression:
        try:
            statements = [s for s in sqlglot.parse(sql, read=self.dialect) if s is not None]
        except SqlglotError as e:
            raise UnsafeSQLError(f"parse error: {e}", "The generated SQL could not be parsed safely.") from e
        if len(statements) != 1:
            raise UnsafeSQLError(
                f"{len(statements)} statements", "Exactly one SQL statement is allowed."
            )
        return statements[0]

    def _check_nodes(self, root: exp.Expression) -> None:
        for node in root.walk():
            if isinstance(node, FORBIDDEN_NODE_TYPES):
                kind = type(node).__name__
                label = VARIABLE_NODE_LABEL if kind in VARIABLE_NODE_NAMES else kind.upper()
                raise UnsafeSQLError(
                    f"forbidden node {kind}", f"The query uses a forbidden operation ({label})."
                )
            if isinstance(node, exp.Func):
                name = _function_name(node)
                if name in FORBIDDEN_FUNCTIONS:
                    raise UnsafeSQLError(
                        f"forbidden function {name}", f"The query uses a forbidden function ({name})."
                    )

    def _check_tables(self, root: exp.Expression) -> set[str]:
        allowed = self.allowed()
        cte_names = {cte.alias_or_name.lower() for cte in root.find_all(exp.CTE)}
        used = set()
        for table in root.find_all(exp.Table):
            name = (table.name or "").lower()
            if not name:
                raise UnsafeSQLError("table expression without a name", "Unsupported table expression.")
            if table.args.get("catalog") is not None:
                raise UnsafeSQLError(f"catalog-qualified table {table.sql()}", "Cross-database queries are not allowed.")
            db = (table.db or "").lower()
            if db and db != self.database_name:
                raise UnsafeSQLError(f"cross-database table {db}.{name}", "Cross-database queries are not allowed.")
            if not db and name in cte_names:
                continue
            if name not in allowed:
                raise UnsafeSQLError(
                    f"table not allowed: {name}", f"Access to table '{table.name}' is not allowed."
                )
            used.add(name)
        return used

    def _enforce_limit(self, root: exp.Expression) -> tuple[exp.Expression, int]:
        if isinstance(root, exp.Subquery):
            root = exp.select("*").from_(root.as_("_q") if not root.alias else root)

        limit_node = root.args.get("limit")
        if limit_node is None:
            return root.limit(self.default_limit), self.default_limit

        value = limit_node.args.get("expression")
        requested = None
        if isinstance(value, exp.Literal) and not value.is_string:
            try:
                requested = int(value.this)
            except ValueError:
                requested = None
        if requested is None or requested > self.max_rows:
            # Non-literal or oversized limits are clamped to the hard cap.
            limit_node.set("expression", exp.Literal.number(self.max_rows))
            return root, self.max_rows
        return root, requested
