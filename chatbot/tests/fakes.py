"""Test doubles for the external services (LLM, vector store, MySQL driver)."""

from types import SimpleNamespace

import pymysql
from langchain_core.documents import Document

from chatbot.services.catalog import Column, ForeignKey, SchemaCatalog, TableInfo
from chatbot.services.config import TextToSQLConfig


def make_catalog() -> SchemaCatalog:
    return SchemaCatalog(
        database="shop",
        generated_at="2026-01-01T00:00:00",
        tables={
            "customers": TableInfo(
                name="customers",
                description="Customers who place orders.",
                columns=[Column("id", "int", False, "PRI"), Column("name", "varchar(100)"), Column("city", "varchar(50)")],
                sample_rows=[{"id": "1", "name": "Asha", "city": "Pune"}],
            ),
            "orders": TableInfo(
                name="orders",
                description="Customer orders with totals.",
                columns=[
                    Column("id", "int", False, "PRI"),
                    Column("customer_id", "int"),
                    Column("total", "decimal(10,2)"),
                    Column("created_at", "datetime"),
                ],
                foreign_keys=[ForeignKey("customer_id", "customers", "id")],
                sample_rows=[{"id": "10", "customer_id": "1", "total": "99.50", "created_at": "2026-01-02"}],
            ),
            "products": TableInfo(
                name="products",
                description="Products in the catalogue.",
                columns=[Column("id", "int", False, "PRI"), Column("title", "varchar(200)"), Column("price", "decimal(10,2)")],
            ),
            "auth_user": TableInfo(
                name="auth_user",
                description="Application users and password hashes.",
                columns=[Column("id", "int", False, "PRI"), Column("password", "varchar(128)")],
            ),
        },
    )


def make_config(**overrides) -> TextToSQLConfig:
    values = dict(
        DB_NAME="shop",
        MAX_ROWS=50,
        DEFAULT_LIMIT=20,
        EMBEDDING_BATCH_DELAY_SECONDS=0,
        RETRIEVAL_CACHE_SECONDS=0,
    )
    values.update(overrides)
    return TextToSQLConfig(**values)


class StaticCatalogStore:
    def __init__(self, catalog):
        self.catalog = catalog

    def get(self):
        return self.catalog


class FakeLLM:
    def __init__(self, *responses, error: Exception | None = None):
        self.responses = list(responses)
        self.error = error
        self.prompts: list[str] = []

    def invoke(self, prompt):
        self.prompts.append(prompt)
        if self.error:
            raise self.error
        return SimpleNamespace(content=self.responses.pop(0) if len(self.responses) > 1 else self.responses[0])


class FakeVectorIndex:
    def __init__(self, tables=(), error: Exception | None = None):
        self.tables = list(tables)
        self.error = error
        self.searches: list[str] = []
        self.added: list[tuple[list[Document], list[str]]] = []
        self.deleted: list[str] = []
        self.resets = 0

    def search(self, query, k):
        self.searches.append(query)
        if self.error:
            raise self.error
        return [Document(page_content=f"TABLE_NAME: {t}", metadata={"table": t}) for t in self.tables][:k]

    def add(self, documents, ids):
        self.added.append((documents, ids))
        return [f"doc:{i}" for i in ids]

    def delete(self, ids):
        self.deleted.extend(ids)

    def reset(self):
        self.resets += 1


class FakeCursor:
    def __init__(self, conn):
        self.conn = conn
        self.description = None
        self._rows = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self.conn.executed.append((sql, params))
        error = self.conn.errors.get(sql)
        if error:
            raise error
        if sql == self.conn.query_sql:
            self.description = [(c,) for c in self.conn.columns]
            self._rows = list(self.conn.rows)

    def fetchmany(self, size):
        self.conn.fetch_sizes.append(size)
        return self._rows[:size]

    def fetchall(self):
        return self._rows


class FakeConnection:
    def __init__(self, query_sql="", columns=(), rows=(), errors=None):
        self.query_sql = query_sql
        self.columns = list(columns)
        self.rows = list(rows)
        self.errors = errors or {}
        self.executed = []
        self.fetch_sizes = []
        self.rolled_back = False
        self.closed = False
        self.connect_kwargs = {}

    def cursor(self):
        return FakeCursor(self)

    def rollback(self):
        self.rolled_back = True

    def close(self):
        self.closed = True

    def connect(self, **kwargs):
        self.connect_kwargs = kwargs
        return self


def mysql_error(code, message="error"):
    return pymysql.err.OperationalError(code, message)
