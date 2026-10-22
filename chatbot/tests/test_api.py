import json
from unittest import mock

from django.contrib.auth.models import Permission, User
from django.core.cache import cache
from django.test import Client, TestCase, override_settings

from chatbot.models import QueryLog
from chatbot.services.catalog import allowed_tables
from chatbot.services.container import set_text_to_sql_service
from chatbot.services.exceptions import QueryExecutionError
from chatbot.services.executor import QueryResult
from chatbot.services.pipeline import TextToSQLService
from chatbot.services.retrieval import SchemaRetriever
from chatbot.services.sql_generation import SQLGenerator
from chatbot.services.sql_validator import SQLValidator
from django.conf import settings

from .fakes import FakeLLM, FakeVectorIndex, StaticCatalogStore, make_catalog, make_config

QUERY_URL = "/chatbot/api/query/"
STREAM_URL = "/chatbot/api/query/stream/"
RESET_URL = "/chatbot/api/conversation/reset/"


class FakeExecutor:
    def __init__(self, error=None):
        self.error = error
        self.executed = []

    def execute(self, validated):
        self.executed.append(validated.sql)
        if self.error:
            raise self.error
        return QueryResult(
            columns=["name", "total"], rows=[["Asha", 10]], sql=validated.sql, tables=validated.tables, duration_ms=1.5
        )


def text_to_sql(**overrides):
    return {**settings.TEXT_TO_SQL, **overrides}


class APITestBase(TestCase):
    llm_output = "SELECT c.name, o.total FROM orders o JOIN customers c ON c.id = o.customer_id"

    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user("analyst", password="pw-12345-long")
        self.user.user_permissions.add(Permission.objects.get(codename="query_database"))
        self.llm = FakeLLM(self.llm_output)
        self.executor = FakeExecutor()
        self.install_service()
        self.client = Client()
        self.client.force_login(self.user)

    def install_service(self):
        config = make_config()
        catalog = make_catalog()
        self.service = TextToSQLService(
            retriever=SchemaRetriever(FakeVectorIndex(["orders"]), StaticCatalogStore(catalog), config),
            generator=SQLGenerator(lambda: self.llm, config),
            validator=SQLValidator(
                allowed_tables=lambda: allowed_tables(catalog, config),
                database_name="shop",
                max_rows=config.MAX_ROWS,
                default_limit=config.DEFAULT_LIMIT,
            ),
            executor=self.executor,
            config=config,
        )
        set_text_to_sql_service(self.service)
        self.addCleanup(set_text_to_sql_service, None)

    def post(self, url, body, client=None, **extra):
        data = body if isinstance(body, (str, bytes)) else json.dumps(body)
        return (client or self.client).post(url, data=data, content_type="application/json", **extra)


class AuthorizationTests(APITestBase):
    def test_anonymous_requests_are_rejected(self):
        response = self.post(QUERY_URL, {"question": "orders"}, client=Client())
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["code"], "not_authenticated")
        self.assertEqual(self.executor.executed, [])

    def test_anonymous_ui_redirects_to_login(self):
        response = Client().get("/")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/accounts/login/", response["Location"])

    def test_user_without_permission_is_forbidden(self):
        User.objects.create_user("viewer", password="pw-12345-long")
        client = Client()
        client.force_login(User.objects.get(username="viewer"))
        response = self.post(QUERY_URL, {"question": "orders"}, client=client)
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.executor.executed, [])

    @override_settings(TEXT_TO_SQL=text_to_sql(REQUIRE_PERMISSION=False))
    def test_permission_check_can_be_disabled(self):
        User.objects.create_user("viewer", password="pw-12345-long")
        client = Client()
        client.force_login(User.objects.get(username="viewer"))
        self.assertEqual(self.post(QUERY_URL, {"question": "orders"}, client=client).status_code, 200)

    def test_get_is_not_allowed(self):
        self.assertEqual(self.client.get(QUERY_URL).status_code, 405)

    def test_csrf_is_enforced(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.user)
        response = self.post(QUERY_URL, {"question": "orders"}, client=client)
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["code"], "csrf_failed")
        self.assertEqual(self.executor.executed, [])

    def test_csrf_token_from_page_is_accepted(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.user)
        page = client.get("/")
        self.assertEqual(page.status_code, 200)
        token = client.cookies["csrftoken"].value
        response = self.post(QUERY_URL, {"question": "orders"}, client=client, HTTP_X_CSRFTOKEN=token)
        self.assertEqual(response.status_code, 200)

    def test_security_headers(self):
        response = self.client.get("/")
        self.assertIn("script-src 'self'", response["Content-Security-Policy"])
        self.assertEqual(response["X-Frame-Options"], "DENY")
        self.assertEqual(response["X-Content-Type-Options"], "nosniff")


class QueryAPITests(APITestBase):
    def test_success_payload_and_backward_compatible_result(self):
        response = self.post(QUERY_URL, {"question": "order totals by customer"})
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["sql"].endswith("LIMIT 20"))
        self.assertEqual(body["columns"], ["name", "total"])
        self.assertEqual(body["rows"], [["Asha", 10]])
        self.assertEqual(body["result"], [{"name": "Asha", "total": 10}])
        self.assertEqual(body["tables"], ["customers", "orders"])
        self.assertIn("total_ms", body["timings"])

    def test_bad_requests(self):
        cases = [
            ("not json", 400, "invalid_json"),
            ("[1, 2]", 400, "invalid_json"),
            ({"question": ""}, 400, "invalid_question"),
            ({"question": 42}, 400, "invalid_question"),
            ({}, 400, "invalid_question"),
            ({"question": "x" * 20000}, 413, "payload_too_large"),
        ]
        for body, status, code in cases:
            with self.subTest(body=str(body)[:20]):
                response = self.post(QUERY_URL, body)
                self.assertEqual(response.status_code, status)
                self.assertEqual(response.json()["code"], code)

    def test_too_long_question(self):
        response = self.post(QUERY_URL, {"question": "x" * 1500})
        self.assertEqual(response.status_code, 400)
        self.assertIn("limited to", response.json()["error"])

    def test_unsafe_sql_is_rejected_audited_and_not_executed(self):
        self.llm.responses = ["DELETE FROM orders"]
        response = self.post(QUERY_URL, {"question": "delete everything"})
        self.assertEqual(response.status_code, 422)
        body = response.json()
        self.assertEqual(body["code"], "unsafe_sql")
        self.assertEqual(body["sql"], "DELETE FROM orders")
        self.assertEqual(self.executor.executed, [])
        log = QueryLog.objects.get()
        self.assertEqual(log.status, QueryLog.Status.REJECTED)
        self.assertEqual(log.generated_sql, "DELETE FROM orders")
        self.assertEqual(log.user, self.user)

    def test_execution_errors_do_not_leak_details(self):
        self.executor.error = QueryExecutionError("(1054, \"Unknown column 'salary' in 'field list'\")")
        response = self.post(QUERY_URL, {"question": "orders"})
        self.assertEqual(response.status_code, 502)
        self.assertNotIn("salary", response.content.decode())
        self.assertNotIn("detail", response.json())

    def test_unexpected_errors_are_generic(self):
        self.executor.error = RuntimeError("password=hunter2 at /srv/app/secret.py")
        response = self.post(QUERY_URL, {"question": "orders"})
        self.assertEqual(response.status_code, 500)
        self.assertNotIn("hunter2", response.content.decode())
        self.assertEqual(response.json()["code"], "internal_error")

    def test_llm_outage_is_503(self):
        self.llm.error = TimeoutError("deadline exceeded")
        response = self.post(QUERY_URL, {"question": "orders"})
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["code"], "upstream_unavailable")

    def test_success_is_audited(self):
        self.post(QUERY_URL, {"question": "orders"})
        log = QueryLog.objects.get()
        self.assertEqual(log.status, QueryLog.Status.SUCCESS)
        self.assertEqual(log.row_count, 1)

    def test_history_is_per_session_and_resettable(self):
        self.post(QUERY_URL, {"question": "first question"})
        self.post(QUERY_URL, {"question": "follow up"})
        self.assertIn("Q: first question", self.llm.prompts[-1])

        other = Client()
        other.force_login(self.user)
        self.post(QUERY_URL, {"question": "fresh start"}, client=other)
        self.assertNotIn("first question", self.llm.prompts[-1])

        self.assertEqual(self.post(RESET_URL, {}).status_code, 200)
        self.post(QUERY_URL, {"question": "after reset"})
        self.assertNotIn("first question", self.llm.prompts[-1])

    # Freeze the clock so the test can't straddle a fixed-window boundary.
    @mock.patch("chatbot.ratelimit.time.time", return_value=1_800_000_010.0)
    @override_settings(TEXT_TO_SQL=text_to_sql(RATE_LIMIT_PER_MINUTE=2))
    def test_rate_limit(self, _time):
        self.assertEqual(self.post(QUERY_URL, {"question": "a"}).status_code, 200)
        self.assertEqual(self.post(QUERY_URL, {"question": "b"}).status_code, 200)
        response = self.post(QUERY_URL, {"question": "c"})
        self.assertEqual(response.status_code, 429)
        self.assertIn("Retry-After", response)
        self.assertEqual(len(self.executor.executed), 2)


class StreamAPITests(APITestBase):
    def read_events(self, response):
        return [json.loads(line) for line in b"".join(response.streaming_content).decode().splitlines() if line]

    def test_stream_emits_stages_then_result(self):
        response = self.post(STREAM_URL, {"question": "orders"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/x-ndjson")
        events = self.read_events(response)
        stages = [e["stage"] for e in events if e["event"] == "stage"]
        self.assertEqual(stages, ["retrieving", "generating", "validating", "executing"])
        self.assertEqual(events[-1]["event"], "result")
        self.assertNotIn("result", events[-1]["data"])  # compact payload, rows only

    def test_stream_errors_are_events(self):
        self.llm.responses = ["SELECT * FROM auth_user"]
        events = self.read_events(self.post(STREAM_URL, {"question": "users"}))
        self.assertEqual(events[-1]["event"], "error")
        self.assertEqual(events[-1]["code"], "unsafe_sql")
        self.assertEqual(events[-1]["status"], 422)

    def test_stream_saves_history(self):
        self.read_events(self.post(STREAM_URL, {"question": "streamed question"}))
        self.read_events(self.post(STREAM_URL, {"question": "next"}))
        self.assertIn("Q: streamed question", self.llm.prompts[-1])

    def test_stream_requires_auth(self):
        self.assertEqual(self.post(STREAM_URL, {"question": "x"}, client=Client()).status_code, 401)


class MiscViewTests(TestCase):
    def test_healthz_is_public(self):
        response = Client().get("/healthz/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})

    @override_settings(ALLOWED_HOSTS=["app.example.com"], SECURE_SSL_REDIRECT=True)
    def test_healthz_bypasses_host_check_and_ssl_redirect(self):
        self.assertEqual(Client().get("/healthz/", HTTP_HOST="localhost:8000").status_code, 200)
        self.assertEqual(Client().get("/", HTTP_HOST="localhost:8000").status_code, 400)

    @mock.patch("chatbot.ratelimit.time.time", return_value=1_800_000_010.0)
    def test_login_page_and_rate_limit(self, _time):
        cache.clear()
        client = Client()
        self.assertEqual(client.get("/accounts/login/").status_code, 200)
        statuses = [
            client.post("/accounts/login/", {"username": "x", "password": "wrong"}).status_code for _ in range(11)
        ]
        self.assertEqual(statuses[:10], [200] * 10)
        self.assertEqual(statuses[10], 429)
