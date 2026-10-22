"""
Django settings for SQL_AI project.

All secrets and deployment-specific values come from environment variables
(optionally loaded from a local ``.env`` file). See ``.env.example`` for the
full list.
"""

import os
import sys
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent

load_dotenv(BASE_DIR / ".env")


def env(name, default=None):
    value = os.getenv(name)
    return default if value is None or value == "" else value


def env_bool(name, default=False):
    value = os.getenv(name)
    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def env_int(name, default):
    value = os.getenv(name)
    return int(value) if value not in (None, "") else default


def env_list(name, default=""):
    return [item.strip() for item in env(name, default).split(",") if item.strip()]


# --------------------------------------------------------------------------
# Core
# --------------------------------------------------------------------------

DEBUG = env_bool("DJANGO_DEBUG", False)

SECRET_KEY = env("DJANGO_SECRET_KEY")
if not SECRET_KEY:
    if not DEBUG:
        raise ImproperlyConfigured("DJANGO_SECRET_KEY must be set when DJANGO_DEBUG is false.")
    # Development only: a throwaway key so the project boots without config.
    SECRET_KEY = "django-insecure-dev-only-key-do-not-use-in-production"

ALLOWED_HOSTS = env_list("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1" if DEBUG else "")
CSRF_TRUSTED_ORIGINS = env_list("DJANGO_CSRF_TRUSTED_ORIGINS")

INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'chatbot',
]

MIDDLEWARE = [
    'chatbot.middleware.HealthCheckMiddleware',
    'django.middleware.security.SecurityMiddleware',
    'whitenoise.middleware.WhiteNoiseMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
    'chatbot.middleware.ContentSecurityPolicyMiddleware',
]

ROOT_URLCONF = 'SQL_AI.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [BASE_DIR / 'templates'],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

WSGI_APPLICATION = 'SQL_AI.wsgi.application'

# Runtime state (Django DB, schema catalog, Chroma files) lives here so Docker
# can mount it as a single persistent volume.
VAR_DIR = Path(env("APP_VAR_DIR", str(BASE_DIR / "var")))

# Django's own metadata database (users, sessions, audit log). This is NOT the
# database users query; that one is configured under TEXT_TO_SQL below.
DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.sqlite3',
        'NAME': env("DJANGO_SQLITE_PATH", str(BASE_DIR / 'db.sqlite3')),
    }
}

AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator'},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},
]

LOGIN_URL = 'login'
LOGIN_REDIRECT_URL = 'home'
LOGOUT_REDIRECT_URL = 'login'

LANGUAGE_CODE = 'en-us'
TIME_ZONE = 'UTC'
USE_I18N = True
USE_TZ = True

STATIC_URL = 'static/'
STATIC_ROOT = BASE_DIR / 'staticfiles'
STATICFILES_DIRS = [BASE_DIR / 'static']
# The manifest storage needs `collectstatic`, so it is skipped in debug and tests.
TESTING = len(sys.argv) > 1 and sys.argv[1] == "test"
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {
        "BACKEND": (
            "django.contrib.staticfiles.storage.StaticFilesStorage"
            if DEBUG or TESTING
            else "whitenoise.storage.CompressedManifestStaticFilesStorage"
        )
    },
}

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

# --------------------------------------------------------------------------
# Cache (retrieval cache + rate limiting). Uses Redis when configured so limits
# are shared between worker processes.
# --------------------------------------------------------------------------

CACHE_URL = env("DJANGO_CACHE_URL")
if CACHE_URL:
    CACHES = {
        "default": {
            "BACKEND": "django.core.cache.backends.redis.RedisCache",
            "LOCATION": CACHE_URL,
            "KEY_PREFIX": "sqlai",
        }
    }
else:
    CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}

# --------------------------------------------------------------------------
# Security hardening
# --------------------------------------------------------------------------

CSRF_FAILURE_VIEW = "chatbot.views.csrf_failure"
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
CSRF_COOKIE_SAMESITE = "Lax"
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "same-origin"
X_FRAME_OPTIONS = "DENY"

SESSION_COOKIE_SECURE = env_bool("DJANGO_SECURE_COOKIES", not DEBUG)
CSRF_COOKIE_SECURE = SESSION_COOKIE_SECURE
SECURE_SSL_REDIRECT = env_bool("DJANGO_SECURE_SSL_REDIRECT", False)
SECURE_HSTS_SECONDS = env_int("DJANGO_SECURE_HSTS_SECONDS", 0)
SECURE_HSTS_INCLUDE_SUBDOMAINS = SECURE_HSTS_SECONDS > 0
if env_bool("DJANGO_BEHIND_PROXY", False):
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

# --------------------------------------------------------------------------
# Text-to-SQL pipeline
# --------------------------------------------------------------------------

TEXT_TO_SQL = {
    # Target database that users query. Use a least-privilege, SELECT-only user.
    "DB_HOST": env("DB_HOST", "localhost"),
    "DB_PORT": env_int("DB_PORT", 3306),
    "DB_USER": env("DB_USER", ""),
    "DB_PASSWORD": env("DB_PASSWORD", ""),
    "DB_NAME": env("DB_NAME", ""),
    "SQL_DIALECT": "mysql",
    "QUERY_TIMEOUT_SECONDS": env_int("SQL_QUERY_TIMEOUT_SECONDS", 15),
    "MAX_ROWS": env_int("SQL_MAX_ROWS", 500),
    "DEFAULT_LIMIT": env_int("SQL_DEFAULT_LIMIT", 100),
    # Optional table allow/deny lists (comma separated). When the allowlist is
    # empty every table in the schema catalog is allowed, minus the denylist.
    "ALLOWED_TABLES": env_list("SQL_ALLOWED_TABLES"),
    "DENIED_TABLES": env_list("SQL_DENIED_TABLES"),
    "DENIED_TABLE_PREFIXES": env_list("SQL_DENIED_TABLE_PREFIXES", "auth_,django_,authtoken_"),

    # LLM + embeddings
    "GOOGLE_API_KEY": env("GOOGLE_API_KEY", ""),
    "LLM_MODEL": env("LLM_MODEL", "gemini-2.5-flash"),
    "LLM_TIMEOUT_SECONDS": env_int("LLM_TIMEOUT_SECONDS", 60),
    "EMBEDDING_PROVIDER": env("EMBEDDING_PROVIDER", "google"),  # google | huggingface
    "EMBEDDING_MODEL": env("EMBEDDING_MODEL", "gemini-embedding-2-preview"),

    # Vector store
    "VECTOR_STORE": env("VECTOR_STORE", "redis"),  # redis | chroma
    "REDIS_URL": env("REDIS_URL", "redis://localhost:6379"),
    "VECTOR_INDEX_NAME": env("VECTOR_INDEX_NAME", "mysql_schema"),
    "CHROMA_DIR": env("CHROMA_DIR", str(BASE_DIR / "chroma_mysql")),

    # Retrieval
    "RETRIEVAL_K": env_int("RETRIEVAL_K", 10),
    "MAX_CONTEXT_TABLES": env_int("MAX_CONTEXT_TABLES", 8),
    "MAX_CONTEXT_CHARS": env_int("MAX_CONTEXT_CHARS", 16000),
    "RETRIEVAL_CACHE_SECONDS": env_int("RETRIEVAL_CACHE_SECONDS", 3600),
    "HISTORY_TURNS": env_int("CHAT_HISTORY_TURNS", 3),
    "MAX_QUESTION_CHARS": env_int("MAX_QUESTION_CHARS", 1000),

    # Schema indexing
    "SCHEMA_CATALOG_PATH": env("SCHEMA_CATALOG_PATH", str(VAR_DIR / "schema_catalog.json")),
    "TABLE_DESCRIPTIONS_PATH": env(
        "TABLE_DESCRIPTIONS_PATH", str(BASE_DIR / "data" / "table_descriptions.json")
    ),
    "SCHEMA_SAMPLE_ROWS": env_int("SCHEMA_SAMPLE_ROWS", 3),
    "CHUNK_SIZE": env_int("SCHEMA_CHUNK_SIZE", 1800),
    "EMBEDDING_BATCH_SIZE": env_int("EMBEDDING_BATCH_SIZE", 5),
    "EMBEDDING_BATCH_DELAY_SECONDS": float(env("EMBEDDING_BATCH_DELAY_SECONDS", "5")),

    # Access control
    "REQUIRE_PERMISSION": env_bool("TEXT_TO_SQL_REQUIRE_PERMISSION", True),
    "RATE_LIMIT_PER_MINUTE": env_int("TEXT_TO_SQL_RATE_LIMIT_PER_MINUTE", 20),
}

# --------------------------------------------------------------------------
# Logging
# --------------------------------------------------------------------------

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "standard": {"format": "%(asctime)s %(levelname)s %(name)s: %(message)s"},
    },
    "handlers": {
        "console": {"class": "logging.StreamHandler", "formatter": "standard", "level": env("LOG_LEVEL", "INFO")},
    },
    "root": {"handlers": ["console"], "level": env("LOG_LEVEL", "INFO")},
    "loggers": {
        # Keep third-party HTTP chatter down unless explicitly debugging.
        "httpx": {"level": "WARNING"},
        "urllib3": {"level": "WARNING"},
        # sqlglot warns when it falls back to an opaque Command node; the
        # validator rejects those anyway.
        "sqlglot": {"level": "ERROR"},
    },
}
