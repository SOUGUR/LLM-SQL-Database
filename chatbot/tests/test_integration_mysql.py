"""Integration tests against a real MySQL server.

Skipped unless ``INTEGRATION_MYSQL_HOST`` is set. Needs an admin account (to
create fixture tables) and the read-only account the app uses, e.g. the
bundled compose MySQL or:

    docker run -d --name sqlai-it -p 3317:3306 -e MYSQL_ROOT_PASSWORD=rootpw \
      -e MYSQL_DATABASE=it_shop -e MYSQL_READONLY_USER=reader -e MYSQL_READONLY_PASSWORD=readerpw \
      -v "$PWD/docker/mysql/init:/docker-entrypoint-initdb.d:ro" mysql:8.4

    INTEGRATION_MYSQL_HOST=127.0.0.1 INTEGRATION_MYSQL_PORT=3317 INTEGRATION_MYSQL_DB=it_shop \
    INTEGRATION_MYSQL_ADMIN_USER=root INTEGRATION_MYSQL_ADMIN_PASSWORD=rootpw \
    INTEGRATION_MYSQL_USER=reader INTEGRATION_MYSQL_PASSWORD=readerpw \
    python manage.py test chatbot.tests.test_integration_mysql
"""

import os
import unittest

import pymysql
from django.test import SimpleTestCase

from chatbot.services.catalog import allowed_tables
from chatbot.services.exceptions import QueryExecutionError, QueryTimeoutError, UnsafeSQLError
from chatbot.services.executor import MySQLExecutor
from chatbot.services.ingestion import extract_catalog
from chatbot.services.pipeline import TextToSQLService
from chatbot.services.retrieval import SchemaRetriever
from chatbot.services.sql_generation import SQLGenerator
from chatbot.services.sql_validator import SQLValidator, ValidatedSQL

from .fakes import FakeLLM, FakeVectorIndex, StaticCatalogStore, make_config

HOST = os.getenv("INTEGRATION_MYSQL_HOST")
PORT = int(os.getenv("INTEGRATION_MYSQL_PORT", "3306"))
DB = os.getenv("INTEGRATION_MYSQL_DB", "it_shop")


def admin_connect():
    return pymysql.connect(
        host=HOST,
        port=PORT,
        user=os.environ["INTEGRATION_MYSQL_ADMIN_USER"],
        password=os.environ["INTEGRATION_MYSQL_ADMIN_PASSWORD"],
        database=DB,
        autocommit=True,
    )


@unittest.skipUnless(HOST, "INTEGRATION_MYSQL_HOST not set")
class MySQLIntegrationTests(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        with admin_connect() as conn, conn.cursor() as cur:
            cur.execute("DROP TABLE IF EXISTS it_orders")
            cur.execute("DROP TABLE IF EXISTS it_customers")
            cur.execute("DROP TABLE IF EXISTS auth_user")
            cur.execute("CREATE TABLE it_customers (id INT PRIMARY KEY, name VARCHAR(50), email VARCHAR(100))")
            cur.execute(
                "CREATE TABLE it_orders (id INT PRIMARY KEY, customer_id INT, total DECIMAL(10,2), "
                "FOREIGN KEY (customer_id) REFERENCES it_customers(id))"
            )
            cur.execute("CREATE TABLE auth_user (id INT PRIMARY KEY, password VARCHAR(128))")
            cur.execute("INSERT INTO it_customers VALUES (1, 'Asha', 'asha@example.com'), (2, 'Ravi', 'ravi@example.com')")
            cur.executemany(
                "INSERT INTO it_orders VALUES (%s, %s, %s)", [(i, 1 + i % 2, 10 + i) for i in range(1, 31)]
            )
            cur.execute("INSERT INTO auth_user VALUES (1, 'pbkdf2_sha256$secret')")

    def config(self, **overrides):
        return make_config(
            DB_HOST=HOST,
            DB_PORT=PORT,
            DB_NAME=DB,
            DB_USER=os.environ["INTEGRATION_MYSQL_USER"],
            DB_PASSWORD=os.environ["INTEGRATION_MYSQL_PASSWORD"],
            SCHEMA_SAMPLE_ROWS=2,
            **overrides,
        )

    def test_select_executes_and_rows_are_capped(self):
        executor = MySQLExecutor(self.config(MAX_ROWS=10))
        result = executor.execute(ValidatedSQL("SELECT id, total FROM it_orders ORDER BY id", ("it_orders",), 10))
        self.assertEqual(result.columns, ["id", "total"])
        self.assertEqual(len(result.rows), 10)
        self.assertTrue(result.truncated)

    def test_database_rejects_writes_even_if_validation_were_bypassed(self):
        executor = MySQLExecutor(self.config())
        for sql in ["UPDATE it_orders SET total = 0", "DELETE FROM it_orders", "DROP TABLE it_orders"]:
            with self.subTest(sql=sql):
                with self.assertRaises((UnsafeSQLError, QueryExecutionError)):
                    executor.execute(ValidatedSQL(sql, ("it_orders",), 1))
        with admin_connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT COUNT(*), SUM(total) FROM it_orders")
            count, total = cur.fetchone()
        self.assertEqual(count, 30)
        self.assertGreater(total, 0)

    def test_server_side_timeout(self):
        executor = MySQLExecutor(self.config(QUERY_TIMEOUT_SECONDS=1))
        heavy = (
            "SELECT COUNT(*) FROM information_schema.COLUMNS a, information_schema.COLUMNS b, "
            "information_schema.COLUMNS c"
        )
        with self.assertRaises(QueryTimeoutError):
            executor.execute(ValidatedSQL(heavy, (), 1))

    def test_schema_extraction_skips_denied_tables_and_redacts(self):
        catalog = extract_catalog(MySQLExecutor(self.config()), self.config(), {"it_orders": "Orders."})
        self.assertIn("it_orders", catalog.tables)
        self.assertNotIn("auth_user", catalog.tables)
        orders = catalog.tables["it_orders"]
        self.assertEqual(orders.description, "Orders.")
        self.assertEqual(orders.foreign_keys[0].ref_table, "it_customers")
        self.assertEqual(len(orders.sample_rows), 2)
        emails = {row["email"] for row in catalog.tables["it_customers"].sample_rows}
        self.assertEqual(emails, {"<redacted>"})

    def test_end_to_end_pipeline(self):
        config = self.config()
        catalog = extract_catalog(MySQLExecutor(config), config, {})
        llm = FakeLLM(
            "SELECT c.name, SUM(o.total) AS spent FROM it_orders o "
            "JOIN it_customers c ON c.id = o.customer_id GROUP BY c.name ORDER BY c.name"
        )
        service = TextToSQLService(
            retriever=SchemaRetriever(FakeVectorIndex(["it_orders"]), StaticCatalogStore(catalog), config),
            generator=SQLGenerator(lambda: llm, config),
            validator=SQLValidator(
                allowed_tables=lambda: allowed_tables(catalog, config), database_name=DB,
                max_rows=config.MAX_ROWS, default_limit=config.DEFAULT_LIMIT,
            ),
            executor=MySQLExecutor(config),
            config=config,
        )
        payload = service.run("total spent per customer")
        self.assertEqual(payload["columns"], ["name", "spent"])
        self.assertEqual([row[0] for row in payload["rows"]], ["Asha", "Ravi"])
        self.assertIn("it_customers", llm.prompts[0])  # FK neighbour pulled into context

        llm.responses = ["SELECT password FROM auth_user"]
        with self.assertRaises(UnsafeSQLError):
            service.run("show passwords")

    def test_read_only_account_cannot_read_other_schemas(self):
        executor = MySQLExecutor(self.config())
        with self.assertRaises(QueryExecutionError):
            executor.execute(ValidatedSQL("SELECT user FROM mysql.user", (), 1))
