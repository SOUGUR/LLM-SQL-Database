"""Schema ingestion: database -> schema catalog -> vector index.

* Schema metadata is read from ``information_schema`` with parameterised
  queries over the same read-only connection used for user queries.
* Denied tables (e.g. ``auth_*``) are never indexed, so their schema and data
  are never sent to the embedding provider or the LLM.
* Sample values from sensitive-looking columns are redacted.
* Each table becomes one document; large tables are split into chunks that all
  repeat the table header, so every chunk maps back to its table.
* Embeddings are reused: only tables whose content hash changed are
  re-embedded, and progress is checkpointed after every table.
"""

import datetime
import json
import logging
import os
import time
from pathlib import Path
from typing import Callable

from langchain_core.documents import Document
from langchain_text_splitters import RecursiveCharacterTextSplitter

from .catalog import Column, ForeignKey, SchemaCatalog, TableInfo, is_table_denied
from .config import TextToSQLConfig
from .executor import MySQLExecutor
from .vector_store import SchemaVectorIndex

logger = logging.getLogger(__name__)

SAMPLE_VALUE_CHARS = 100
SENSITIVE_PARTS = frozenset(
    "password passwd pwd secret token salt hash otp pin ssn cvv iban email phone "
    "mobile aadhaar aadhar pan dob".split()
)
SENSITIVE_SUBSTRINGS = ("password", "secret", "token", "apikey", "api_key")


def is_sensitive_column(name: str) -> bool:
    lowered = name.lower()
    return any(s in lowered for s in SENSITIVE_SUBSTRINGS) or bool(
        set(lowered.split("_")) & SENSITIVE_PARTS
    )


def format_sample_value(column: str, value) -> str:
    if value is None:
        return "NULL"
    if is_sensitive_column(column):
        return "<redacted>"
    if isinstance(value, (bytes, bytearray)):
        return "<binary>"
    text = str(value).replace("\n", " ")
    if len(text) > SAMPLE_VALUE_CHARS:
        text = text[: SAMPLE_VALUE_CHARS - 3] + "..."
    return text


def quote_identifier(name: str) -> str:
    return "`" + name.replace("`", "``") + "`"


def load_table_descriptions(path: str | os.PathLike) -> dict[str, str]:
    path = Path(path)
    if not path.exists():
        logger.warning("Table descriptions file not found: %s", path)
        return {}
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def extract_catalog(
    executor: MySQLExecutor, config: TextToSQLConfig, descriptions: dict[str, str]
) -> SchemaCatalog:
    schema = config.DB_NAME
    allow = {t.lower() for t in config.ALLOWED_TABLES}

    with executor.connection() as conn, conn.cursor() as cur:
        executor.apply_session_guards(cur)
        cur.execute(
            "SELECT TABLE_NAME, TABLE_COMMENT FROM information_schema.TABLES "
            "WHERE TABLE_SCHEMA = %s AND TABLE_TYPE IN ('BASE TABLE', 'VIEW') ORDER BY TABLE_NAME",
            (schema,),
        )
        tables: dict[str, TableInfo] = {}
        for name, comment in cur.fetchall():
            if is_table_denied(name, config) or (allow and name.lower() not in allow):
                continue
            tables[name] = TableInfo(name=name, description=descriptions.get(name) or comment or "")

        cur.execute(
            "SELECT TABLE_NAME, COLUMN_NAME, COLUMN_TYPE, IS_NULLABLE, COLUMN_KEY "
            "FROM information_schema.COLUMNS WHERE TABLE_SCHEMA = %s "
            "ORDER BY TABLE_NAME, ORDINAL_POSITION",
            (schema,),
        )
        for table, column, col_type, nullable, key in cur.fetchall():
            if table in tables:
                tables[table].columns.append(
                    Column(name=column, type=str(col_type), nullable=nullable == "YES", key=key or "")
                )

        cur.execute(
            "SELECT TABLE_NAME, COLUMN_NAME, REFERENCED_TABLE_NAME, REFERENCED_COLUMN_NAME "
            "FROM information_schema.KEY_COLUMN_USAGE "
            "WHERE TABLE_SCHEMA = %s AND REFERENCED_TABLE_NAME IS NOT NULL "
            "ORDER BY TABLE_NAME, COLUMN_NAME",
            (schema,),
        )
        for table, column, ref_table, ref_column in cur.fetchall():
            if table in tables:
                tables[table].foreign_keys.append(ForeignKey(column, ref_table, ref_column))

        if config.SCHEMA_SAMPLE_ROWS > 0:
            for table in tables.values():
                if not table.columns:
                    continue
                columns = [c.name for c in table.columns]
                cols_sql = ", ".join(quote_identifier(c) for c in columns)
                try:
                    cur.execute(
                        f"SELECT {cols_sql} FROM {quote_identifier(table.name)} LIMIT %s",
                        (config.SCHEMA_SAMPLE_ROWS,),
                    )
                    table.sample_rows = [
                        {c: format_sample_value(c, v) for c, v in zip(columns, row)}
                        for row in cur.fetchall()
                    ]
                except Exception as e:
                    logger.warning("Could not sample %s: %s", table.name, e)
        conn.rollback()

    return SchemaCatalog(
        database=schema,
        tables=tables,
        generated_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
    )


def build_documents(table: TableInfo, chunk_size: int) -> list[Document]:
    metadata = {"table": table.name, "module": table.name.split("_")[0]}
    full = table.render()
    if len(full) <= chunk_size:
        return [Document(page_content=full, metadata=dict(metadata))]

    header = table.header()
    body = full[len(header.rstrip("\n")):].lstrip("\n")
    splitter = RecursiveCharacterTextSplitter(
        chunk_size=max(200, chunk_size - len(header) - 20),
        chunk_overlap=100,
        separators=["\n", ", ", " ", ""],
    )
    parts = splitter.split_text(body)
    return [
        Document(
            page_content=f"{header}(part {i}/{len(parts)})\n{part}",
            metadata=dict(metadata),
        )
        for i, part in enumerate(parts, start=1)
    ]


class IndexState:
    """Tracks which table versions are embedded, to reuse embeddings."""

    def __init__(self, path: str | os.PathLike, signature: dict):
        self.path = Path(path)
        self.signature = signature
        self.tables: dict[str, dict] = {}
        self.valid = False
        if self.path.exists():
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if data.get("signature") == signature:
                self.tables = data.get("tables", {})
                self.valid = True

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"signature": self.signature, "tables": self.tables}, f, indent=2)
        os.replace(tmp, self.path)


def index_state_path(config: TextToSQLConfig) -> Path:
    return Path(config.SCHEMA_CATALOG_PATH).with_name("index_state.json")


def sync_vector_index(
    catalog: SchemaCatalog,
    vector_index: SchemaVectorIndex,
    config: TextToSQLConfig,
    force: bool = False,
    log: Callable[[str], None] = logger.info,
    sleep: Callable[[float], None] = time.sleep,
) -> dict:
    signature = {
        "vector_store": config.VECTOR_STORE,
        "index_name": config.VECTOR_INDEX_NAME,
        "embedding_provider": config.EMBEDDING_PROVIDER,
        "embedding_model": config.EMBEDDING_MODEL,
        "chunk_size": config.CHUNK_SIZE,
    }
    state = IndexState(index_state_path(config), signature)
    if force or not state.valid:
        log("Rebuilding vector index from scratch.")
        vector_index.reset()
        state.tables = {}
        state.save()

    stats = {"embedded_tables": 0, "embedded_chunks": 0, "unchanged": 0, "removed": 0}

    for name in [t for t in state.tables if t not in catalog.tables]:
        vector_index.delete(state.tables[name].get("ids", []))
        del state.tables[name]
        stats["removed"] += 1
    state.save()

    batch_size = max(1, config.EMBEDDING_BATCH_SIZE)
    first_batch = True
    for name, table in sorted(catalog.tables.items()):
        digest = table.content_hash()
        if state.tables.get(name, {}).get("hash") == digest:
            stats["unchanged"] += 1
            continue

        old_ids = state.tables.get(name, {}).get("ids", [])
        if old_ids:
            vector_index.delete(old_ids)

        docs = build_documents(table, config.CHUNK_SIZE)
        ids = [f"{name}:{i}" for i in range(len(docs))]
        stored: list[str] = []
        for start in range(0, len(docs), batch_size):
            if not first_batch and config.EMBEDDING_BATCH_DELAY_SECONDS > 0:
                # Stay under embedding API rate limits.
                sleep(config.EMBEDDING_BATCH_DELAY_SECONDS)
            first_batch = False
            stored += vector_index.add(docs[start:start + batch_size], ids[start:start + batch_size])

        state.tables[name] = {"hash": digest, "ids": stored or ids}
        state.save()
        stats["embedded_tables"] += 1
        stats["embedded_chunks"] += len(docs)
        log(f"Embedded {name} ({len(docs)} chunk{'s' if len(docs) != 1 else ''})")

    return stats
