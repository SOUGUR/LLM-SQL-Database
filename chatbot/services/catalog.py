"""Schema catalog: a JSON snapshot of the target database schema.

The catalog is produced by ``manage.py index_schema`` and is the source of
truth for (a) which tables may be queried and (b) the compact per-table schema
text given to the LLM. Vector search only decides *which* tables are relevant;
the prompt context is always rebuilt from the catalog, so a table is never
represented by a half chunk.
"""

import hashlib
import json
import os
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .config import TextToSQLConfig


@dataclass
class Column:
    name: str
    type: str
    nullable: bool = True
    key: str = ""


@dataclass
class ForeignKey:
    column: str
    ref_table: str
    ref_column: str


@dataclass
class TableInfo:
    name: str
    description: str = ""
    columns: list[Column] = field(default_factory=list)
    foreign_keys: list[ForeignKey] = field(default_factory=list)
    sample_rows: list[dict] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict) -> "TableInfo":
        return cls(
            name=data["name"],
            description=data.get("description", ""),
            columns=[Column(**c) for c in data.get("columns", [])],
            foreign_keys=[ForeignKey(**fk) for fk in data.get("foreign_keys", [])],
            sample_rows=data.get("sample_rows", []),
        )

    def header(self) -> str:
        return (
            f"TABLE_NAME: {self.name}\n"
            f"TABLE_DESCRIPTION: {self.description or 'No description available.'}\n"
        )

    def render(self, sample_rows: int | None = None) -> str:
        """Render the table as compact text for embeddings or LLM context."""
        lines = [self.header().rstrip("\n"), "COLUMNS:"]
        for col in self.columns:
            flags = []
            if col.key == "PRI":
                flags.append("primary key")
            if not col.nullable:
                flags.append("not null")
            suffix = f" [{', '.join(flags)}]" if flags else ""
            lines.append(f"- {col.name} ({col.type}){suffix}")

        if self.foreign_keys:
            lines.append("RELATIONSHIPS:")
            for fk in self.foreign_keys:
                lines.append(f"- {self.name}.{fk.column} -> {fk.ref_table}.{fk.ref_column}")

        rows = self.sample_rows if sample_rows is None else self.sample_rows[:sample_rows]
        if rows:
            lines.append(f"SAMPLE_ROWS ({len(rows)}):")
            for row in rows:
                lines.append("- " + ", ".join(f"{k}={v}" for k, v in row.items()))
        return "\n".join(lines)

    def content_hash(self) -> str:
        payload = json.dumps(asdict(self), sort_keys=True, default=str)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass
class SchemaCatalog:
    database: str
    tables: dict[str, TableInfo]
    generated_at: str = ""

    @classmethod
    def from_dict(cls, data: dict) -> "SchemaCatalog":
        return cls(
            database=data.get("database", ""),
            generated_at=data.get("generated_at", ""),
            tables={name: TableInfo.from_dict(t) for name, t in data.get("tables", {}).items()},
        )

    def to_dict(self) -> dict:
        return {
            "database": self.database,
            "generated_at": self.generated_at,
            "tables": {name: asdict(t) for name, t in sorted(self.tables.items())},
        }

    @classmethod
    def load(cls, path: str | os.PathLike) -> "SchemaCatalog":
        with open(path, "r", encoding="utf-8") as f:
            return cls.from_dict(json.load(f))

    def save(self, path: str | os.PathLike) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, indent=2, default=str)
        os.replace(tmp, path)

    def get(self, name: str) -> TableInfo | None:
        return self.tables.get(name) or self._lower_index().get(name.lower())

    def _lower_index(self) -> dict[str, TableInfo]:
        return {name.lower(): t for name, t in self.tables.items()}

    def neighbors(self, name: str) -> list[str]:
        """Tables linked to ``name`` by a foreign key in either direction."""
        table = self.get(name)
        if table is None:
            return []
        linked = [fk.ref_table for fk in table.foreign_keys]
        linked += [
            other.name
            for other in self.tables.values()
            if any(fk.ref_table == table.name for fk in other.foreign_keys)
        ]
        seen, result = {table.name}, []
        for item in linked:
            if item not in seen and item in self.tables:
                seen.add(item)
                result.append(item)
        return result

    def join_path(self, start: str, end: str, allowed: set[str], max_hops: int = 4) -> list[str]:
        """Shortest foreign-key path between two tables (inclusive), or ``[]``.

        Only passes through ``allowed`` (lower-cased) tables, so e.g. ``auth_user``
        audit columns never become a bridge.
        """
        a, b = self.get(start), self.get(end)
        if a is None or b is None:
            return []
        if a.name == b.name:
            return [a.name]
        previous = {a.name: None}
        frontier = [a.name]
        for _ in range(max_hops):
            next_frontier = []
            for name in frontier:
                for neighbor in self.neighbors(name):
                    if neighbor in previous or neighbor.lower() not in allowed:
                        continue
                    previous[neighbor] = name
                    if neighbor == b.name:
                        path = [neighbor]
                        while previous[path[-1]] is not None:
                            path.append(previous[path[-1]])
                        return path[::-1]
                    next_frontier.append(neighbor)
            frontier = next_frontier
        return []


def is_table_denied(name: str, config: TextToSQLConfig) -> bool:
    lowered = name.lower()
    if lowered in {t.lower() for t in config.DENIED_TABLES}:
        return True
    return any(lowered.startswith(p.lower()) for p in config.DENIED_TABLE_PREFIXES)


def allowed_tables(catalog: SchemaCatalog | None, config: TextToSQLConfig) -> set[str]:
    """Lower-cased names of tables users may query."""
    if config.ALLOWED_TABLES:
        names = {t.lower() for t in config.ALLOWED_TABLES}
        if catalog is not None:
            names &= {t.lower() for t in catalog.tables}
    elif catalog is not None:
        names = {t.lower() for t in catalog.tables}
    else:
        names = set()
    return {n for n in names if not is_table_denied(n, config)}


class CatalogStore:
    """Loads the catalog from disk, reloading when the file changes."""

    def __init__(self, path: str | os.PathLike):
        self.path = Path(path)
        self._lock = threading.Lock()
        self._mtime: float | None = None
        self._catalog: SchemaCatalog | None = None

    def get(self) -> SchemaCatalog | None:
        try:
            mtime = self.path.stat().st_mtime
        except FileNotFoundError:
            return None
        with self._lock:
            if self._catalog is None or mtime != self._mtime:
                self._catalog = SchemaCatalog.load(self.path)
                self._mtime = mtime
            return self._catalog
