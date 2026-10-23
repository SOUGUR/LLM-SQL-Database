"""Schema-aware retrieval of the tables relevant to a question.

1. Vector search over table chunks ranks candidate tables (deduplicated).
2. A lexical matcher over the catalog boosts tables whose name or columns are
   mentioned literally, and is the fallback if the vector store is down.
3. Foreign-key neighbours of the top tables are added so the LLM can join to
   readable columns (names, codes) instead of returning bare ids.
4. Context is rebuilt from the catalog under a character budget, so the prompt
   never contains the whole schema.

The ranked table list is cached per (question, catalog version), which skips
the embedding call for repeated questions.
"""

import hashlib
import logging
import re
from dataclasses import dataclass

from django.core.cache import cache as default_cache

from .catalog import CatalogStore, SchemaCatalog, allowed_tables
from .config import TextToSQLConfig
from .exceptions import SchemaNotIndexedError, UnanswerableQuestionError
from .vector_store import SchemaVectorIndex, table_of

logger = logging.getLogger(__name__)

_WORD = re.compile(r"[a-z0-9]+")
_STOPWORDS = frozenset(
    "a an and are as at by count for from get give how in is list me many much "
    "of on or show the their there to total what which who with all each per".split()
)


@dataclass
class RetrievalResult:
    tables: list[str]
    context: str
    source: str  # "vector", "lexical", or "cache"


def _tokens(text: str) -> set[str]:
    words = {w for w in _WORD.findall(text.lower()) if w not in _STOPWORDS and len(w) > 1}
    # Cheap singularisation so "orders" matches "order".
    return words | {w[:-1] for w in words if w.endswith("s") and len(w) > 3}


def lexical_scores(question: str, catalog: SchemaCatalog, allowed: set[str]) -> list[tuple[int, int, str]]:
    """``(score, table_name_hits, table)`` sorted best first."""
    q = _tokens(question)
    if not q:
        return []
    scores = []
    for table in catalog.tables.values():
        if table.name.lower() not in allowed:
            continue
        name_hits = len(q & _tokens(table.name.replace("_", " ")))
        column_parts = set()
        for col in table.columns:
            column_parts |= _tokens(col.name.replace("_", " "))
        score = 3 * name_hits + len(q & column_parts) + len(q & _tokens(table.description))
        if score:
            scores.append((score, name_hits, table.name))
    scores.sort(key=lambda s: (-s[0], -s[1], s[2]))
    return scores


def lexical_rank(question: str, catalog: SchemaCatalog, allowed: set[str]) -> list[str]:
    return [name for _, _, name in lexical_scores(question, catalog, allowed)]


class SchemaRetriever:
    def __init__(
        self,
        vector_index: SchemaVectorIndex,
        catalog_store: CatalogStore,
        config: TextToSQLConfig,
        cache=default_cache,
    ):
        self.vector_index = vector_index
        self.catalog_store = catalog_store
        self.config = config
        self.cache = cache

    def retrieve(self, question: str) -> RetrievalResult:
        catalog = self.catalog_store.get()
        if catalog is None or not catalog.tables:
            raise SchemaNotIndexedError("schema catalog missing")
        allowed = allowed_tables(catalog, self.config)

        key = self._cache_key(question, catalog)
        ranked = self._cache_get(key)
        source = "cache"
        if ranked is None:
            ranked, source = self._rank(question, catalog, allowed)
            # Don't cache degraded results produced during a vector store outage.
            if ranked and source == "vector":
                self._cache_set(key, ranked)

        tables, primary_count = self._select(ranked, catalog, allowed)
        if not tables:
            raise UnanswerableQuestionError("no relevant tables")
        return RetrievalResult(
            tables=tables, context=self._render(tables, catalog, primary_count), source=source
        )

    # -- ranking -------------------------------------------------------------

    def _rank(self, question: str, catalog: SchemaCatalog, allowed: set[str]) -> tuple[list[str], str]:
        vector_tables: list[str] = []
        source = "vector"
        try:
            for doc in self.vector_index.search(question, k=self.config.RETRIEVAL_K):
                name = table_of(doc)
                table = catalog.get(name) if name else None
                if table and table.name not in vector_tables:
                    vector_tables.append(table.name)
        except Exception as e:
            logger.warning("Vector search failed, using lexical retrieval only: %s", e)
            source = "lexical"

        scores = lexical_scores(question, catalog, allowed)
        # Tables literally named in the question first, then semantic results,
        # then remaining keyword matches (column/description hits are noisier).
        strong = [name for _, name_hits, name in scores if name_hits][:2]
        ranked = []
        for name in strong + vector_tables + [name for _, _, name in scores]:
            if name not in ranked:
                ranked.append(name)
        return ranked, source

    def _select(self, ranked: list[str], catalog: SchemaCatalog, allowed: set[str]) -> tuple[list[str], int]:
        limit = self.config.MAX_CONTEXT_TABLES
        primary = [t for t in ranked if t.lower() in allowed][:limit]
        selected = list(primary)
        extra_budget = max(2, limit // 2)

        # Bridge tables: intermediate hops that join the top tables together
        # (e.g. invoice -> dispatch -> order -> party). Without them the LLM
        # sees both ends of a join but has no way to connect them.
        top = primary[:5]
        for i, start in enumerate(top):
            for end in top[i + 1:]:
                for bridge in catalog.join_path(start, end, allowed)[1:-1]:
                    if extra_budget > 0 and bridge not in selected:
                        selected.append(bridge)
                        extra_budget -= 1

        for table in primary[:3]:
            for neighbor in catalog.neighbors(table):
                if extra_budget <= 0:
                    break
                if neighbor not in selected and neighbor.lower() in allowed:
                    selected.append(neighbor)
                    extra_budget -= 1
        return selected, len(primary)

    def _render(self, tables: list[str], catalog: SchemaCatalog, primary_count: int) -> str:
        """Two passes: every table's schema first, then sample rows for the
        primary tables while budget remains. Join structure matters more than
        example values, so bridge tables are never crowded out by samples."""
        budget = self.config.MAX_CONTEXT_CHARS
        texts: dict[str, str] = {}
        used = 0
        for name in tables:
            text = catalog.get(name).render(sample_rows=0)
            if used + len(text) + 2 <= budget:
                texts[name] = text
                used += len(text) + 2
        for name in tables[:primary_count]:
            if name not in texts:
                continue
            full = catalog.get(name).render()
            extra = len(full) - len(texts[name])
            if extra > 0 and used + extra <= budget:
                texts[name] = full
                used += extra
        return "\n\n".join(texts[name] for name in tables if name in texts)

    # -- caching -------------------------------------------------------------

    def _cache_key(self, question: str, catalog: SchemaCatalog) -> str:
        normalized = " ".join(question.lower().split())
        digest = hashlib.sha256(f"{catalog.generated_at}|{normalized}".encode()).hexdigest()
        return f"t2s:retrieval:{digest}"

    def _cache_get(self, key: str):
        if self.config.RETRIEVAL_CACHE_SECONDS <= 0:
            return None
        try:
            return self.cache.get(key)
        except Exception as e:  # cache outages must not break queries
            logger.warning("Retrieval cache read failed: %s", e)
            return None

    def _cache_set(self, key: str, value: list[str]) -> None:
        if self.config.RETRIEVAL_CACHE_SECONDS <= 0:
            return
        try:
            self.cache.set(key, value, self.config.RETRIEVAL_CACHE_SECONDS)
        except Exception as e:
            logger.warning("Retrieval cache write failed: %s", e)
