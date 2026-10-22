import datetime
import decimal

from django.test import SimpleTestCase

from chatbot.services.exceptions import (
    QueryExecutionError,
    QueryTimeoutError,
    UnsafeSQLError,
    UpstreamServiceError,
)
from chatbot.services.executor import MySQLExecutor
from chatbot.services.formatting import format_result, json_safe
from chatbot.services.sql_validator import ValidatedSQL

from .fakes import FakeConnection, make_config, mysql_error

SQL = "SELECT id, name FROM customers LIMIT 20"


def validated(sql=SQL):
    return ValidatedSQL(sql=sql, tables=("customers",), limit=20)


class ExecutorTests(SimpleTestCase):
    def test_runs_in_read_only_transaction_and_rolls_back(self):
        conn = FakeConnection(SQL, ["id", "name"], [(1, "Asha"), (2, "Ravi")])
        result = MySQLExecutor(make_config(QUERY_TIMEOUT_SECONDS=7), connect=conn.connect).execute(validated())

        statements = [s for s, _ in conn.executed]
        self.assertEqual(statements[0], "SET SESSION TRANSACTION READ ONLY")
        self.assertIn(("SET SESSION MAX_EXECUTION_TIME = %s", (7000,)), conn.executed)
        self.assertLess(statements.index("START TRANSACTION READ ONLY"), statements.index(SQL))
        self.assertTrue(conn.rolled_back)
        self.assertTrue(conn.closed)
        self.assertEqual(result.columns, ["id", "name"])
        self.assertEqual(result.rows, [[1, "Asha"], [2, "Ravi"]])
        self.assertFalse(result.truncated)

    def test_connection_disables_multi_statements_and_local_infile(self):
        conn = FakeConnection(SQL, ["id"], [])
        MySQLExecutor(make_config(QUERY_TIMEOUT_SECONDS=5), connect=conn.connect).execute(validated())
        self.assertEqual(conn.connect_kwargs["client_flag"], 0)
        self.assertFalse(conn.connect_kwargs["local_infile"])
        self.assertEqual(conn.connect_kwargs["read_timeout"], 10)

    def test_fetch_is_bounded_and_truncation_detected(self):
        rows = [(i,) for i in range(100)]
        conn = FakeConnection(SQL, ["id"], rows)
        result = MySQLExecutor(make_config(MAX_ROWS=10), connect=conn.connect).execute(validated())
        self.assertEqual(conn.fetch_sizes, [11])
        self.assertEqual(len(result.rows), 10)
        self.assertTrue(result.truncated)

    def test_duplicate_column_names_are_disambiguated(self):
        conn = FakeConnection(SQL, ["name", "name"], [("a", "b")])
        result = MySQLExecutor(make_config(), connect=conn.connect).execute(validated())
        self.assertEqual(result.columns, ["name", "name_2"])

    def test_mariadb_timeout_fallback(self):
        conn = FakeConnection(
            SQL, ["id"], [], errors={"SET SESSION MAX_EXECUTION_TIME = %s": mysql_error(1193, "unknown var")}
        )
        MySQLExecutor(make_config(QUERY_TIMEOUT_SECONDS=3), connect=conn.connect).execute(validated())
        self.assertIn(("SET SESSION max_statement_time = %s", (3,)), conn.executed)

    def test_error_mapping(self):
        cases = [
            (3024, QueryTimeoutError),
            (2013, QueryTimeoutError),
            (1792, UnsafeSQLError),
            (1054, QueryExecutionError),
            (2006, UpstreamServiceError),
        ]
        for code, expected in cases:
            with self.subTest(code=code):
                conn = FakeConnection(SQL, ["id"], [], errors={SQL: mysql_error(code, "Unknown column 'secret'")})
                with self.assertRaises(expected) as ctx:
                    MySQLExecutor(make_config(), connect=conn.connect).execute(validated())
                # Driver error text stays out of the public message.
                self.assertNotIn("secret", ctx.exception.public_message)
                self.assertTrue(conn.closed)

    def test_connect_failure(self):
        def refuse(**kwargs):
            raise mysql_error(2003, "Can't connect to MySQL server on 'db-host'")

        with self.assertRaises(UpstreamServiceError) as ctx:
            MySQLExecutor(make_config(), connect=refuse).execute(validated())
        self.assertNotIn("db-host", ctx.exception.public_message)


class FormattingTests(SimpleTestCase):
    def test_json_safe_values(self):
        self.assertEqual(json_safe(decimal.Decimal("10.50")), "10.50")
        self.assertEqual(json_safe(datetime.date(2026, 1, 2)), "2026-01-02")
        self.assertEqual(json_safe(b"\x00\x01"), "<binary 2 bytes>")
        self.assertIsNone(json_safe(None))
        self.assertEqual(json_safe(5), 5)
        self.assertEqual(len(json_safe("x" * 5000)), 2000)

    def test_payload_shape(self):
        from chatbot.services.executor import QueryResult

        result = QueryResult(columns=["a"], rows=[[decimal.Decimal("1.5")]], sql="SELECT a", duration_ms=3.2)
        payload = format_result(result, ["t"], {"total_ms": 10.04}, max_rows=50, include_records=True)
        self.assertEqual(payload["rows"], [["1.5"]])
        self.assertEqual(payload["result"], [{"a": "1.5"}])
        self.assertEqual(payload["summary"], "Found 1 row.")
        self.assertEqual(payload["timings"], {"total_ms": 10.0})
