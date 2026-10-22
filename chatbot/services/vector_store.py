"""Embedding model and vector store adapters.

Both previously used stores are supported behind one small interface:
``VECTOR_STORE=redis`` (Redis Stack, default) or ``VECTOR_STORE=chroma``.
Clients are created lazily so Django can boot (and tests can run) without
network access or API keys.
"""

import logging
import re
from typing import Protocol

from langchain_core.documents import Document

from .config import TextToSQLConfig

logger = logging.getLogger(__name__)

# Metadata fields stored with every chunk; declared explicitly so Redis returns
# them from similarity searches.
REDIS_INDEX_SCHEMA = {"text": [{"name": "table"}, {"name": "module"}]}

_TABLE_LINE = re.compile(r"^\s*TABLE_NAME:\s*(\S+)", re.MULTILINE)


def table_of(doc: Document) -> str | None:
    table = (doc.metadata or {}).get("table")
    if table:
        return str(table)
    match = _TABLE_LINE.search(doc.page_content or "")
    return match.group(1) if match else None


def build_embeddings(config: TextToSQLConfig):
    provider = config.EMBEDDING_PROVIDER.lower()
    if provider == "google":
        from langchain_google_genai import GoogleGenerativeAIEmbeddings

        return GoogleGenerativeAIEmbeddings(
            model=config.EMBEDDING_MODEL,
            google_api_key=config.GOOGLE_API_KEY or None,
        )
    if provider == "huggingface":
        from langchain_huggingface import HuggingFaceEmbeddings  # optional dependency

        return HuggingFaceEmbeddings(model_name=config.EMBEDDING_MODEL)
    raise ValueError(f"Unsupported EMBEDDING_PROVIDER: {config.EMBEDDING_PROVIDER}")


class SchemaVectorIndex(Protocol):
    def search(self, query: str, k: int) -> list[Document]: ...
    def add(self, documents: list[Document], ids: list[str]) -> list[str]: ...
    def delete(self, ids: list[str]) -> None: ...
    def reset(self) -> None: ...


class RedisSchemaIndex:
    def __init__(self, config: TextToSQLConfig, embeddings=None):
        self.config = config
        self._embeddings = embeddings
        self._store = None

    @property
    def store(self):
        if self._store is None:
            from langchain_community.vectorstores import Redis

            self._store = Redis(
                redis_url=self.config.REDIS_URL,
                index_name=self.config.VECTOR_INDEX_NAME,
                embedding=self._embeddings or build_embeddings(self.config),
                index_schema=REDIS_INDEX_SCHEMA,
            )
        return self._store

    def search(self, query: str, k: int) -> list[Document]:
        return self.store.similarity_search(query, k=k)

    def add(self, documents: list[Document], ids: list[str]) -> list[str]:
        return self.store.add_texts(
            [d.page_content for d in documents],
            metadatas=[d.metadata for d in documents],
            keys=ids,
        )

    def delete(self, ids: list[str]) -> None:
        if ids:
            self.store.delete(ids=ids)

    def reset(self) -> None:
        from langchain_community.vectorstores import Redis

        try:
            Redis.drop_index(
                self.config.VECTOR_INDEX_NAME, delete_documents=True, redis_url=self.config.REDIS_URL
            )
        except Exception as e:  # index may not exist yet
            logger.info("Redis index not dropped (%s)", e)
        self._store = None


class ChromaSchemaIndex:
    def __init__(self, config: TextToSQLConfig, embeddings=None):
        self.config = config
        self._embeddings = embeddings
        self._store = None

    @property
    def store(self):
        if self._store is None:
            from langchain_chroma import Chroma  # optional dependency

            self._store = Chroma(
                collection_name=self.config.VECTOR_INDEX_NAME,
                embedding_function=self._embeddings or build_embeddings(self.config),
                persist_directory=self.config.CHROMA_DIR,
            )
        return self._store

    def search(self, query: str, k: int) -> list[Document]:
        return self.store.similarity_search(query, k=k)

    def add(self, documents: list[Document], ids: list[str]) -> list[str]:
        return self.store.add_documents(documents, ids=ids)

    def delete(self, ids: list[str]) -> None:
        if ids:
            self.store.delete(ids=ids)

    def reset(self) -> None:
        try:
            self.store.delete_collection()
        except Exception as e:
            logger.info("Chroma collection not deleted (%s)", e)
        self._store = None


def build_vector_index(config: TextToSQLConfig, embeddings=None) -> SchemaVectorIndex:
    store = config.VECTOR_STORE.lower()
    if store == "redis":
        return RedisSchemaIndex(config, embeddings)
    if store == "chroma":
        return ChromaSchemaIndex(config, embeddings)
    raise ValueError(f"Unsupported VECTOR_STORE: {config.VECTOR_STORE}")
