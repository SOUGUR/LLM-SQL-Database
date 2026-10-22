"""Gunicorn settings, overridable through environment variables."""

import os

bind = os.getenv("GUNICORN_BIND", "0.0.0.0:8000")
# Threaded workers: requests spend most of their time waiting on the LLM and
# the database, and the streaming endpoint holds a connection open.
worker_class = "gthread"
workers = int(os.getenv("GUNICORN_WORKERS", "2"))
threads = int(os.getenv("GUNICORN_THREADS", "4"))
timeout = int(os.getenv("GUNICORN_TIMEOUT", "120"))
graceful_timeout = 30
keepalive = 5
max_requests = 1000
max_requests_jitter = 100
accesslog = "-"
errorlog = "-"
loglevel = os.getenv("LOG_LEVEL", "info").lower()
forwarded_allow_ips = os.getenv("GUNICORN_FORWARDED_ALLOW_IPS", "127.0.0.1")
