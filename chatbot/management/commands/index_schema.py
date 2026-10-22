from django.core.management.base import BaseCommand, CommandError

from chatbot.services.config import get_config
from chatbot.services.executor import MySQLExecutor
from chatbot.services.ingestion import (
    build_documents,
    extract_catalog,
    load_table_descriptions,
    sync_vector_index,
)
from chatbot.services.vector_store import build_vector_index


class Command(BaseCommand):
    help = (
        "Extract the target database schema into the schema catalog and embed it "
        "into the configured vector store. Unchanged tables reuse existing embeddings."
    )

    def add_arguments(self, parser):
        parser.add_argument("--force", action="store_true", help="Drop the index and re-embed every table.")
        parser.add_argument(
            "--dry-run", action="store_true", help="Build the catalog and chunks without embedding anything."
        )
        parser.add_argument("--show-chunks", action="store_true", help="Print every chunk that would be embedded.")

    def handle(self, *args, **options):
        config = get_config()
        if not config.DB_NAME:
            raise CommandError("DB_NAME is not configured.")

        descriptions = load_table_descriptions(config.TABLE_DESCRIPTIONS_PATH)
        self.stdout.write(f"Loaded {len(descriptions)} table descriptions.")

        try:
            catalog = extract_catalog(MySQLExecutor(config), config, descriptions)
        except Exception as e:
            raise CommandError(f"Schema extraction failed: {e}") from e
        self.stdout.write(f"Extracted {len(catalog.tables)} tables from '{catalog.database}'.")

        if options["show_chunks"] or options["dry_run"]:
            total = 0
            for table in catalog.tables.values():
                docs = build_documents(table, config.CHUNK_SIZE)
                total += len(docs)
                if options["show_chunks"]:
                    for i, doc in enumerate(docs):
                        self.stdout.write(f"\n===== {table.name} chunk {i} =====\n{doc.page_content}")
            self.stdout.write(f"{total} chunks.")

        if options["dry_run"]:
            self.stdout.write(self.style.WARNING("Dry run: catalog and vector index not written."))
            return

        catalog.save(config.SCHEMA_CATALOG_PATH)
        self.stdout.write(f"Schema catalog written to {config.SCHEMA_CATALOG_PATH}")

        try:
            stats = sync_vector_index(
                catalog, build_vector_index(config), config, force=options["force"], log=self.stdout.write
            )
        except Exception as e:
            raise CommandError(
                f"Vector indexing failed: {e}. Re-run the command to resume; finished tables are kept."
            ) from e
        self.stdout.write(
            self.style.SUCCESS(
                f"Indexed into {config.VECTOR_STORE} '{config.VECTOR_INDEX_NAME}': "
                f"{stats['embedded_tables']} tables embedded ({stats['embedded_chunks']} chunks), "
                f"{stats['unchanged']} unchanged, {stats['removed']} removed."
            )
        )
