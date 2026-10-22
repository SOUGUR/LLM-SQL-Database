from django.test import SimpleTestCase

from chatbot.services.catalog import allowed_tables
from chatbot.services.exceptions import UnsafeSQLError
from chatbot.services.sql_validator import SQLValidator

from .fakes import make_catalog, make_config


def make_validator(**config_overrides):
    config = make_config(**config_overrides)
    catalog = make_catalog()
    return SQLValidator(
        allowed_tables=lambda: allowed_tables(catalog, config),
        database_name="shop",
        max_rows=config.MAX_ROWS,
        default_limit=config.DEFAULT_LIMIT,
    )


class AllowedQueriesTests(SimpleTestCase):
    def setUp(self):
        self.validator = make_validator()

    def test_simple_select_gets_default_limit(self):
        result = self.validator.validate("SELECT name FROM customers")
        self.assertEqual(result.sql, "SELECT name FROM customers LIMIT 20")
        self.assertEqual(result.limit, 20)
        self.assertEqual(result.tables, ("customers",))

    def test_join_aggregate_and_subquery(self):
        sql = (
            "SELECT c.name, SUM(o.total) AS spent FROM orders o JOIN customers c ON c.id = o.customer_id "
            "WHERE o.total > (SELECT AVG(total) FROM orders) GROUP BY c.name ORDER BY spent DESC"
        )
        result = self.validator.validate(sql)
        self.assertEqual(result.tables, ("customers", "orders"))
        self.assertTrue(result.sql.endswith("LIMIT 20"))

    def test_cte_names_are_not_treated_as_tables(self):
        result = self.validator.validate("WITH big AS (SELECT * FROM orders WHERE total > 10) SELECT * FROM big")
        self.assertEqual(result.tables, ("orders",))

    def test_union_of_allowed_tables(self):
        result = self.validator.validate("SELECT name FROM customers UNION SELECT title FROM products")
        self.assertIn("UNION", result.sql)

    def test_small_limit_is_kept_and_large_limit_clamped(self):
        self.assertEqual(self.validator.validate("SELECT * FROM orders LIMIT 5").limit, 5)
        clamped = self.validator.validate("SELECT * FROM orders LIMIT 100000")
        self.assertEqual(clamped.limit, 50)
        self.assertTrue(clamped.sql.endswith("LIMIT 50"))

    def test_mysql_offset_limit_syntax_is_clamped(self):
        result = self.validator.validate("SELECT * FROM orders LIMIT 10, 99999")
        self.assertEqual(result.limit, 50)

    def test_trailing_semicolon_and_same_database_qualifier(self):
        result = self.validator.validate("SELECT * FROM shop.orders;")
        self.assertEqual(result.tables, ("orders",))

    def test_table_names_are_case_insensitive(self):
        self.assertEqual(self.validator.validate("SELECT * FROM ORDERS").tables, ("orders",))

    def test_semicolon_inside_string_literal_is_fine(self):
        self.validator.validate("SELECT * FROM customers WHERE name = 'a;b'")

    def test_comments_are_stripped_from_executed_sql(self):
        result = self.validator.validate("SELECT name /* hi */ FROM customers -- trailing")
        self.assertNotIn("/*", result.sql)
        self.assertNotIn("--", result.sql)

    def test_mysql_executable_comment_never_reaches_database(self):
        result = self.validator.validate("SELECT 1 /*!50000 , SLEEP(10) */ FROM customers")
        self.assertNotIn("SLEEP", result.sql.upper())


class BlockedQueriesTests(SimpleTestCase):
    def setUp(self):
        self.validator = make_validator()

    def assertBlocked(self, sql, message_fragment=""):
        with self.assertRaises(UnsafeSQLError) as ctx:
            self.validator.validate(sql)
        if message_fragment:
            self.assertIn(message_fragment, ctx.exception.public_message)
        return ctx.exception

    def test_dml_and_ddl(self):
        for sql in [
            "INSERT INTO orders (id) VALUES (1)",
            "UPDATE orders SET total = 0",
            "DELETE FROM orders",
            "DROP TABLE orders",
            "TRUNCATE TABLE orders",
            "ALTER TABLE orders ADD COLUMN x INT",
            "CREATE TABLE x (id INT)",
            "REPLACE INTO orders (id) VALUES (1)",
            "INSERT INTO orders SELECT * FROM orders",
        ]:
            with self.subTest(sql=sql):
                self.assertBlocked(sql)

    def test_stacked_queries(self):
        self.assertBlocked("SELECT * FROM orders; DROP TABLE orders", "Exactly one")
        self.assertBlocked("SELECT 1; SELECT 2", "Exactly one")

    def test_admin_and_session_statements(self):
        for sql in [
            "SHOW TABLES",
            "SET @a = 1",
            "CALL cleanup()",
            "GRANT ALL ON *.* TO 'x'",
            "LOCK TABLES orders READ",
            "USE mysql",
            "EXPLAIN SELECT * FROM orders",
            "DO SLEEP(1)",
            "HANDLER orders OPEN",
            "LOAD DATA INFILE '/etc/passwd' INTO TABLE orders",
        ]:
            with self.subTest(sql=sql):
                self.assertBlocked(sql)

    def test_select_into_outfile_and_locking_reads(self):
        self.assertBlocked("SELECT * FROM orders INTO OUTFILE '/tmp/x'")
        self.assertBlocked("SELECT * FROM orders FOR UPDATE")
        self.assertBlocked("SELECT * INTO @x FROM orders")

    def test_unauthorized_tables(self):
        self.assertBlocked("SELECT * FROM secrets", "secrets")
        self.assertBlocked("SELECT * FROM orders WHERE id IN (SELECT id FROM secrets)")
        self.assertBlocked("SELECT * FROM orders o JOIN payroll p ON p.id = o.id")

    def test_denied_prefix_tables(self):
        self.assertBlocked("SELECT password FROM auth_user", "auth_user")

    def test_cross_database_and_system_schemas(self):
        for sql in [
            "SELECT * FROM mysql.user",
            "SELECT * FROM information_schema.tables",
            "SELECT * FROM performance_schema.threads",
            "SELECT * FROM other_db.orders",
        ]:
            with self.subTest(sql=sql):
                self.assertBlocked(sql, "Cross-database")

    def test_cte_cannot_hide_unauthorized_table(self):
        self.assertBlocked("WITH x AS (SELECT * FROM auth_user) SELECT * FROM x")

    def test_unsafe_functions(self):
        for sql in [
            "SELECT SLEEP(5)",
            "SELECT * FROM orders WHERE SLEEP(5) = 0",
            "SELECT BENCHMARK(1000000, MD5('x'))",
            "SELECT LOAD_FILE('/etc/passwd')",
            "SELECT GET_LOCK('x', 10)",
            "SELECT USER()",
            "SELECT DATABASE()",
            "SELECT VERSION()",
            "SELECT EXTRACTVALUE(1, CONCAT(0x7e, (SELECT 1)))",
            "SELECT name FROM customers WHERE id = 1 AND UPDATEXML(1, CONCAT(0x7e, USER()), 1)",
        ]:
            with self.subTest(sql=sql):
                self.assertBlocked(sql, "forbidden")

    def test_variables(self):
        self.assertBlocked("SELECT @@version")
        self.assertBlocked("SELECT @a := 1 FROM orders")

    def test_garbage_and_empty(self):
        self.assertBlocked("")
        self.assertBlocked("   ")
        self.assertBlocked("this is not sql at all")
        self.assertBlocked("SELECT * FROM orders WHERE")

    def test_oversized_sql(self):
        self.assertBlocked("SELECT * FROM orders WHERE name = '" + "a" * 25000 + "'")

    def test_allowlist_restricts_further(self):
        validator = make_validator(ALLOWED_TABLES=("orders",))
        validator.validate("SELECT * FROM orders")
        with self.assertRaises(UnsafeSQLError):
            validator.validate("SELECT * FROM customers")

    def test_denylist(self):
        validator = make_validator(DENIED_TABLES=("products",))
        with self.assertRaises(UnsafeSQLError):
            validator.validate("SELECT * FROM products")

    def test_no_catalog_means_no_tables(self):
        validator = SQLValidator(allowed_tables=set(), database_name="shop")
        with self.assertRaises(UnsafeSQLError):
            validator.validate("SELECT * FROM orders")
