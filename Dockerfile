# syntax=docker/dockerfile:1
# Production image: multi-stage build, non-root user, Gunicorn, static files
# served by WhiteNoise. Configuration comes from environment variables only.

ARG PYTHON_VERSION=3.12

FROM python:${PYTHON_VERSION}-slim AS builder
ARG INSTALL_OPTIONAL=false
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
COPY requirements.txt requirements-optional.txt ./
RUN pip install -r requirements.txt \
 && if [ "$INSTALL_OPTIONAL" = "true" ]; then pip install -r requirements-optional.txt; fi

FROM python:${PYTHON_VERSION}-slim AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH" \
    APP_VAR_DIR=/app/var \
    DJANGO_SQLITE_PATH=/app/var/db.sqlite3 \
    SCHEMA_CATALOG_PATH=/app/var/schema_catalog.json \
    CHROMA_DIR=/app/var/chroma

RUN groupadd --system app && useradd --system --gid app --home-dir /app --shell /usr/sbin/nologin app
WORKDIR /app

COPY --from=builder /opt/venv /opt/venv
COPY --chown=app:app . .

# collectstatic needs settings to import; these values exist only for this step.
RUN DJANGO_SECRET_KEY=collectstatic-only DJANGO_DEBUG=false python manage.py collectstatic --noinput \
 && mkdir -p /app/var && chown -R app:app /app/var /app/staticfiles \
 && chmod +x docker/entrypoint.sh

USER app
VOLUME ["/app/var"]
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz/', timeout=4).status == 200 else 1)"

ENTRYPOINT ["docker/entrypoint.sh"]
CMD ["gunicorn", "SQL_AI.wsgi:application", "--config", "docker/gunicorn.conf.py"]
