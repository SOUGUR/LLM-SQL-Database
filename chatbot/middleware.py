from django.http import JsonResponse

HEALTHCHECK_PATH = "/healthz/"


class HealthCheckMiddleware:
    """Answers liveness probes before host validation and SSL redirects.

    Container health checks hit ``localhost``, which usually isn't in
    ``ALLOWED_HOSTS`` in production, so this must run first.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.path == HEALTHCHECK_PATH and request.method in ("GET", "HEAD"):
            return JsonResponse({"status": "ok"})
        return self.get_response(request)


CONTENT_SECURITY_POLICY = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "connect-src 'self'; object-src 'none'; base-uri 'self'; form-action 'self'; frame-ancestors 'none'"
)


class ContentSecurityPolicyMiddleware:
    """Strict CSP for the app pages (no inline scripts/styles are used).

    The Django admin is excluded because it relies on inline styles.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        if not request.path.startswith("/admin/"):
            response.headers.setdefault("Content-Security-Policy", CONTENT_SECURITY_POLICY)
        return response
