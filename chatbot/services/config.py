from dataclasses import dataclass, field, fields

from django.conf import settings


@dataclass(frozen=True)
class TextToSQLConfig:
    DB_HOST: str = "localhost"
    DB_PORT: int = 3306
    DB_USER: str = ""
    DB_PASSWORD: str = field(default="", repr=False)
    DB_NAME: str = ""
    SQL_DIALECT: str = "mysql"
    QUERY_TIMEOUT_SECONDS: int = 15
    MAX_ROWS: int = 500
    DEFAULT_LIMIT: int = 100
    ALLOWED_TABLES: tuple = ()
    DENIED_TABLES: tuple = ()
    DENIED_TABLE_PREFIXES: tuple = ("auth_", "django_", "authtoken_")

    GOOGLE_API_KEY: str = field(default="", repr=False)
    LLM_MODEL: str = "gemini-2.5-flash"
    LLM_TIMEOUT_SECONDS: int = 60
    EMBEDDING_PROVIDER: str = "google"
    EMBEDDING_MODEL: str = "gemini-embedding-2-preview"

    VECTOR_STORE: str = "redis"
    REDIS_URL: str = "redis://localhost:6379"
    VECTOR_INDEX_NAME: str = "mysql_schema"
    CHROMA_DIR: str = "./chroma_mysql"

    RETRIEVAL_K: int = 10
    MAX_CONTEXT_TABLES: int = 8
    MAX_CONTEXT_CHARS: int = 16000
    RETRIEVAL_CACHE_SECONDS: int = 3600
    HISTORY_TURNS: int = 3
    MAX_QUESTION_CHARS: int = 1000

    SCHEMA_CATALOG_PATH: str = "var/schema_catalog.json"
    TABLE_DESCRIPTIONS_PATH: str = "data/table_descriptions.json"
    SCHEMA_SAMPLE_ROWS: int = 3
    CHUNK_SIZE: int = 1800
    EMBEDDING_BATCH_SIZE: int = 5
    EMBEDDING_BATCH_DELAY_SECONDS: float = 5.0

    REQUIRE_PERMISSION: bool = True
    RATE_LIMIT_PER_MINUTE: int = 20

    @classmethod
    def from_settings(cls, overrides: dict | None = None) -> "TextToSQLConfig":
        values = dict(getattr(settings, "TEXT_TO_SQL", {}))
        values.update(overrides or {})
        known = {f.name for f in fields(cls)}
        clean = {}
        for key, value in values.items():
            if key not in known:
                continue
            clean[key] = tuple(value) if isinstance(value, list) else value
        return cls(**clean)


def get_config() -> TextToSQLConfig:
    return TextToSQLConfig.from_settings()
