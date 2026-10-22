import json
import logging

from django.conf import settings
from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib.auth.views import LoginView
from django.http import HttpResponse, JsonResponse, StreamingHttpResponse
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.generic import TemplateView

from .models import QueryLog
from .ratelimit import check_rate_limit
from .services.config import get_config
from .services.container import get_text_to_sql_service
from .services.exceptions import TextToSQLError, UnsafeSQLError

logger = logging.getLogger(__name__)

HISTORY_SESSION_KEY = "t2s_history"
MAX_BODY_BYTES = 16 * 1024
QUERY_PERMISSION = "chatbot.query_database"


def client_ip(request) -> str:
    return request.META.get("REMOTE_ADDR", "unknown")


def error_response(code: str, message: str, status: int, **extra) -> JsonResponse:
    return JsonResponse({"error": message, "code": code, **extra}, status=status)


def error_payload(error: Exception) -> tuple[dict, int]:
    if isinstance(error, TextToSQLError):
        payload = {"error": error.public_message, "code": error.code}
        if error.sql:
            payload["sql"] = error.sql
        if settings.DEBUG and error.detail:
            payload["detail"] = error.detail
        return payload, error.status
    return {"error": TextToSQLError.public_message, "code": TextToSQLError.code}, 500


def audit(request, question: str, *, payload: dict | None = None, error: Exception | None = None):
    try:
        if error is None:
            QueryLog.objects.create(
                user=request.user if request.user.is_authenticated else None,
                question=question[:4000],
                generated_sql=payload.get("sql", ""),
                status=QueryLog.Status.SUCCESS,
                row_count=payload.get("row_count"),
                duration_ms=payload.get("timings", {}).get("total_ms"),
            )
        else:
            QueryLog.objects.create(
                user=request.user if request.user.is_authenticated else None,
                question=str(question)[:4000],
                generated_sql=getattr(error, "sql", "") or "",
                status=QueryLog.Status.REJECTED if isinstance(error, UnsafeSQLError) else QueryLog.Status.ERROR,
                error_code=getattr(error, "code", "internal_error"),
            )
    except Exception:  # auditing must never break a request
        logger.exception("Failed to write query audit log")


def remember(request, payload: dict) -> None:
    turns = get_config().HISTORY_TURNS
    history = request.session.get(HISTORY_SESSION_KEY, [])
    history.append({"question": payload["question"], "sql": payload["sql"]})
    request.session[HISTORY_SESSION_KEY] = history[-turns:] if turns else []


class APIView(View):
    """JSON API base: auth, permission, rate limit and body parsing."""

    rate_limited = True
    http_method_names = ["post"]

    def dispatch(self, request, *args, **kwargs):
        if not request.user.is_authenticated:
            return error_response("not_authenticated", "Please sign in to continue.", 401)
        if get_config().REQUIRE_PERMISSION and not request.user.has_perm(QUERY_PERMISSION):
            return error_response("forbidden", "You don't have permission to query the database.", 403)
        if self.rate_limited:
            allowed, retry_after = check_rate_limit(
                f"user:{request.user.pk}", get_config().RATE_LIMIT_PER_MINUTE
            )
            if not allowed:
                response = error_response(
                    "rate_limited", f"Too many requests. Try again in {retry_after} seconds.", 429
                )
                response["Retry-After"] = str(retry_after)
                return response
        return super().dispatch(request, *args, **kwargs)

    def parse_question(self, request):
        if len(request.body) > MAX_BODY_BYTES:
            return None, error_response("payload_too_large", "Request is too large.", 413)
        try:
            body = json.loads(request.body or b"{}")
        except (ValueError, UnicodeDecodeError):
            return None, error_response("invalid_json", "Request body must be valid JSON.", 400)
        if not isinstance(body, dict):
            return None, error_response("invalid_json", "Request body must be a JSON object.", 400)
        question = body.get("question", "")
        if not isinstance(question, str) or not question.strip():
            return None, error_response("invalid_question", "Question is required.", 400)
        return question, None


class TextToSQLView(APIView):
    """POST {"question": "..."} -> JSON result (original API, extended)."""

    def post(self, request):
        question, error = self.parse_question(request)
        if error:
            return error
        history = request.session.get(HISTORY_SESSION_KEY, [])
        try:
            payload = get_text_to_sql_service().run(question, history, include_records=True)
        except Exception as e:
            if not isinstance(e, TextToSQLError):
                logger.exception("Unexpected Text-to-SQL failure")
            audit(request, question, error=e)
            body, status = error_payload(e)
            return JsonResponse(body, status=status)
        remember(request, payload)
        audit(request, question, payload=payload)
        return JsonResponse(payload)


class TextToSQLStreamView(APIView):
    """POST {"question": "..."} -> NDJSON stream of progress events, then the result."""

    def post(self, request):
        question, error = self.parse_question(request)
        if error:
            return error
        history = request.session.get(HISTORY_SESSION_KEY, [])
        service = get_text_to_sql_service()

        def events():
            try:
                for event in service.stream(question, history):
                    if event["event"] == "result":
                        remember(request, event["data"])
                        # The session middleware has already run for streaming
                        # responses, so persist the history explicitly.
                        request.session.save()
                        audit(request, question, payload=event["data"])
                    yield json.dumps(event) + "\n"
            except Exception as e:
                if not isinstance(e, TextToSQLError):
                    logger.exception("Unexpected Text-to-SQL failure")
                audit(request, question, error=e)
                body, status = error_payload(e)
                yield json.dumps({"event": "error", "status": status, **body}) + "\n"

        response = StreamingHttpResponse(events(), content_type="application/x-ndjson")
        response["Cache-Control"] = "no-cache"
        response["X-Accel-Buffering"] = "no"  # disable proxy buffering (nginx)
        return response


class ResetConversationView(APIView):
    rate_limited = False

    def post(self, request):
        request.session.pop(HISTORY_SESSION_KEY, None)
        return JsonResponse({"ok": True})


@method_decorator(ensure_csrf_cookie, name="dispatch")
class ChatView(LoginRequiredMixin, TemplateView):
    template_name = "index.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        config = get_config()
        context["can_query"] = (not config.REQUIRE_PERMISSION) or self.request.user.has_perm(QUERY_PERMISSION)
        context["max_question_chars"] = config.MAX_QUESTION_CHARS
        context["max_rows"] = config.MAX_ROWS
        return context


def csrf_failure(request, reason=""):
    """JSON CSRF errors for the API; Django's default page elsewhere."""
    if request.path.startswith("/chatbot/api/"):
        return error_response("csrf_failed", "Your session token is missing or expired. Reload the page and try again.", 403)
    from django.views.csrf import csrf_failure as default_csrf_failure

    return default_csrf_failure(request, reason=reason)


class RateLimitedLoginView(LoginView):
    template_name = "registration/login.html"
    redirect_authenticated_user = True

    def post(self, request, *args, **kwargs):
        allowed, retry_after = check_rate_limit(f"login:{client_ip(request)}", 10)
        if not allowed:
            response = HttpResponse(
                f"Too many login attempts. Try again in {retry_after} seconds.", status=429
            )
            response["Retry-After"] = str(retry_after)
            return response
        return super().post(request, *args, **kwargs)
