import tempfile
from pathlib import Path

from django.core.cache.backends.locmem import LocMemCache
from django.test import SimpleTestCase

from chatbot.services.catalog import CatalogStore, SchemaCatalog, TableInfo, Column
from chatbot.services.exceptions import SchemaNotIndexedError, UnanswerableQuestionError
from chatbot.services.ingestion import (
    build_documents,
    format_sample_value,
    is_sensitive_column,
    quote_identifier,
    sync_vector_index,
)
from chatbot.services.retrieval import SchemaRetriever, lexical_rank
from chatbot.services.vector_store import table_of

from .fakes import FakeVectorIndex, StaticCatalogStore, make_catalog, make_config


class RetrievalTests(SimpleTestCase):
    def retriever(self, index, **config):
        return SchemaRetriever(index, StaticCatalogStore(make_catalog()), make_config(**config), cache=LocMemCache("t", {}))

    def test_vector_hits_are_deduplicated_and_fk_neighbors_added(self):
        result = self.retriever(FakeVectorIndex(["orders", "orders"])).retrieve("revenue per buyer")
        self.assertEqual(result.tables, ["orders", "customers"])
        self.assertIn("TABLE_NAME: orders", result.context)
        self.assertIn("orders.customer_id -> customers.id", result.context)

    def test_denied_tables_are_never_in_context(self):
        result = self.retriever(FakeVectorIndex(["auth_user", "products"])).retrieve("users")
        self.assertNotIn("auth_user", result.tables)
        self.assertNotIn("password", result.context)

    def test_lexical_fallback_when_vector_store_is_down(self):
        result = self.retriever(FakeVectorIndex(error=ConnectionError("redis down"))).retrieve("list all products")
        self.assertEqual(result.source, "lexical")
        self.assertEqual(result.tables[0], "products")

    def test_literal_table_mentions_are_boosted(self):
        result = self.retriever(FakeVectorIndex(["orders"])).retrieve("product prices")
        self.assertEqual(result.tables[0], "products")

    def test_context_budget_is_respected(self):
        result = self.retriever(FakeVectorIndex(["orders", "customers", "products"]), MAX_CONTEXT_CHARS=400).retrieve("x")
        self.assertLessEqual(len(result.context), 400)

    def test_max_tables(self):
        result = self.retriever(FakeVectorIndex(["products", "orders", "customers"]), MAX_CONTEXT_TABLES=1).retrieve("x")
        self.assertEqual(result.tables[0], "products")
        self.assertLessEqual(len(result.tables), 3)

    def test_results_are_cached(self):
        index = FakeVectorIndex(["orders"])
        retriever = self.retriever(index, RETRIEVAL_CACHE_SECONDS=60)
        first = retriever.retrieve("Total orders")
        second = retriever.retrieve("  total   ORDERS ")
        self.assertEqual(len(index.searches), 1)
        self.assertEqual(second.source, "cache")
        self.assertEqual(first.tables, second.tables)

    def test_no_match_is_unanswerable(self):
        with self.assertRaises(UnanswerableQuestionError):
            self.retriever(FakeVectorIndex([])).retrieve("weather tomorrow")

    def test_missing_catalog(self):
        retriever = SchemaRetriever(FakeVectorIndex(), StaticCatalogStore(None), make_config())
        with self.assertRaises(SchemaNotIndexedError):
            retriever.retrieve("orders")

    def chain_catalog(self):
        """invoice -> dispatch -> order -> party, plus auth_user audit FKs."""
        from chatbot.services.catalog import ForeignKey

        def table(name, fks=(), cols=("id", "name")):
            return TableInfo(name=name, columns=[Column(c, "int") for c in cols],
                             foreign_keys=[ForeignKey(c, t, "id") for c, t in fks])

        return SchemaCatalog(database="shop", generated_at="x", tables={
            "marketing_invoice": table("marketing_invoice", [("dispatch_id", "marketing_orderdispatches"), ("created_by_id", "auth_user")]),
            "marketing_orderdispatches": table("marketing_orderdispatches", [("order_id", "marketing_orders"), ("created_by_id", "auth_user")]),
            "marketing_orders": table("marketing_orders", [("party_id", "party_partymaster")]),
            "party_partymaster": table("party_partymaster", [("created_by_id", "auth_user")]),
            "auth_user": table("auth_user"),
        })

    def test_join_path_finds_multi_hop_bridges_and_skips_denied_tables(self):
        catalog = self.chain_catalog()
        allowed = {"marketing_invoice", "marketing_orderdispatches", "marketing_orders", "party_partymaster"}
        self.assertEqual(
            catalog.join_path("marketing_invoice", "party_partymaster", allowed),
            ["marketing_invoice", "marketing_orderdispatches", "marketing_orders", "party_partymaster"],
        )
        # The shorter route through auth_user audit columns must not be used.
        self.assertEqual(catalog.join_path("marketing_invoice", "party_partymaster", allowed | {"auth_user"})[1], "auth_user")
        self.assertEqual(catalog.join_path("marketing_invoice", "party_partymaster", allowed, max_hops=2), [])

    def test_bridge_tables_are_added_to_context(self):
        retriever = SchemaRetriever(
            FakeVectorIndex(["party_partymaster", "marketing_invoice"]),
            StaticCatalogStore(self.chain_catalog()), make_config(), cache=LocMemCache("t", {}),
        )
        result = retriever.retrieve("recent invoices with party names")
        for bridge in ("marketing_orderdispatches", "marketing_orders"):
            self.assertIn(bridge, result.tables)
            self.assertIn(f"TABLE_NAME: {bridge}", result.context)
        self.assertNotIn("auth_user", result.tables)

    def test_schemas_win_over_sample_rows_when_budget_is_tight(self):
        catalog = make_catalog()
        catalog.tables["orders"].sample_rows = [{"note": "x" * 300}] * 3
        retriever = SchemaRetriever(FakeVectorIndex(["orders", "customers"]), StaticCatalogStore(catalog),
                                    make_config(MAX_CONTEXT_CHARS=600), cache=LocMemCache("t", {}))
        result = retriever.retrieve("orders")
        self.assertIn("TABLE_NAME: customers", result.context)
        self.assertNotIn("x" * 300, result.context)

    def test_lexical_rank_respects_allowed(self):
        catalog = make_catalog()
        self.assertEqual(lexical_rank("auth user password", catalog, {"orders"}), [])


class IngestionTests(SimpleTestCase):
    def test_sensitive_columns_redacted(self):
        for name in ["password", "user_email", "api_key", "reset_token", "mobile"]:
            self.assertTrue(is_sensitive_column(name), name)
        for name in ["name", "pincode", "total", "shipping_address"]:
            self.assertFalse(is_sensitive_column(name), name)
        self.assertEqual(format_sample_value("password", "pbkdf2$..."), "<redacted>")
        self.assertEqual(format_sample_value("note", "x" * 200)[-3:], "...")

    def test_quote_identifier_escapes_backticks(self):
        self.assertEqual(quote_identifier("we`ird"), "`we``ird`")

    def test_small_table_is_one_document(self):
        docs = build_documents(make_catalog().tables["orders"], 2000)
        self.assertEqual(len(docs), 1)
        self.assertEqual(docs[0].metadata, {"table": "orders", "module": "orders"})

    def test_large_table_chunks_all_repeat_the_header(self):
        table = TableInfo(
            name="marketing_invoice",
            description="Invoices.",
            columns=[Column(f"column_number_{i}", "varchar(255)") for i in range(200)],
        )
        docs = build_documents(table, 800)
        self.assertGreater(len(docs), 1)
        for doc in docs:
            self.assertTrue(doc.page_content.startswith("TABLE_NAME: marketing_invoice"))
            self.assertEqual(table_of(doc), "marketing_invoice")

    def test_sync_reuses_embeddings_for_unchanged_tables(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = make_config(SCHEMA_CATALOG_PATH=str(Path(tmp) / "catalog.json"))
            catalog = make_catalog()
            index = FakeVectorIndex()

            first = sync_vector_index(catalog, index, config, log=lambda m: None)
            self.assertEqual(first["embedded_tables"], 4)
            self.assertEqual(index.resets, 1)

            index.added.clear()
            second = sync_vector_index(catalog, index, config, log=lambda m: None)
            self.assertEqual(second["embedded_tables"], 0)
            self.assertEqual(second["unchanged"], 4)
            self.assertEqual(index.added, [])

            catalog.tables["orders"].description = "Changed description"
            del catalog.tables["products"]
            third = sync_vector_index(catalog, index, config, log=lambda m: None)
            self.assertEqual(third["embedded_tables"], 1)
            self.assertEqual(third["removed"], 1)
            self.assertIn("doc:products:0", index.deleted)
            self.assertIn("doc:orders:0", index.deleted)

    def test_sync_rebuilds_when_embedding_model_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "catalog.json")
            index = FakeVectorIndex()
            sync_vector_index(make_catalog(), index, make_config(SCHEMA_CATALOG_PATH=path), log=lambda m: None)
            sync_vector_index(
                make_catalog(), index, make_config(SCHEMA_CATALOG_PATH=path, EMBEDDING_MODEL="other"), log=lambda m: None
            )
            self.assertEqual(index.resets, 2)

    def test_sync_respects_rate_limit_delay(self):
        with tempfile.TemporaryDirectory() as tmp:
            sleeps = []
            config = make_config(
                SCHEMA_CATALOG_PATH=str(Path(tmp) / "c.json"), EMBEDDING_BATCH_SIZE=1, EMBEDDING_BATCH_DELAY_SECONDS=2
            )
            sync_vector_index(make_catalog(), FakeVectorIndex(), config, log=lambda m: None, sleep=sleeps.append)
            self.assertEqual(sleeps, [2, 2, 2])

    def test_catalog_round_trip_and_store_reload(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "catalog.json"
            make_catalog().save(path)
            store = CatalogStore(path)
            loaded = store.get()
            self.assertIsInstance(loaded, SchemaCatalog)
            self.assertEqual(loaded.tables["orders"].foreign_keys[0].ref_table, "customers")
            self.assertEqual(loaded.neighbors("customers"), ["orders"])
            self.assertIsNone(CatalogStore(Path(tmp) / "missing.json").get())
