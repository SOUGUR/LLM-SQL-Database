# LLM-SQL-Database

Chat with a complex MySQL database in plain English. A Django app retrieves the
relevant part of your schema (RAG over a vector store), asks an LLM to write a
query, **validates it with a real SQL parser**, runs it read-only with a timeout
and row cap, and shows the results in a chat UI.

![Query result with joined tables, timings and export actions](docs/images/04-result-table.jpg)

## Features

- **Natural language → SQL** with Google Gemini (`gemini-2.5-flash` by default).
- **Schema-aware RAG**: per-table documents in Redis Stack (default) or Chroma,
  hybrid vector + keyword ranking, foreign-key expansion for joins, a context
  budget, and cached retrieval.
- **Defence in depth**: parser-based validation, read-only DB user, read-only
  transactions, statement timeouts, row limits, table allowlists.
- **Chat UI**: streamed progress, sortable result tables, SQL preview, timings,
  copy / CSV / JSON export, light/dark themes, keyboard and screen-reader friendly.
- **Accounts and audit**: login required, per-user permission, rate limiting,
  every question and generated query logged in the Django admin.

## Architecture

```text
Browser (chat UI, static JS/CSS)
   │  POST /chatbot/api/query/stream/   (session auth + CSRF, NDJSON progress events)
   ▼
chatbot/views.py ── auth · permission · rate limit · body limits · audit log · session history
   │
   ▼
services/pipeline.py  TextToSQLService  (components injected via services/container.py)
   │
   ├─ retrieval.py      SchemaRetriever   vector search + keyword boost + FK neighbours,
   │                                      context rebuilt from the schema catalog, cached
   ├─ sql_generation.py SQLGenerator      prompt with untrusted input fenced, CANNOT_ANSWER path
   ├─ sql_validator.py  SQLValidator      sqlglot AST checks → canonical SQL + enforced LIMIT
   ├─ executor.py       MySQLExecutor     read-only txn, timeouts, bounded fetch, safe errors
   └─ formatting.py                       JSON-safe columns/rows, summary, timings

Offline: manage.py index_schema
   ingestion.py  information_schema → catalog.json (var/) → chunks → vector_store.py (Redis | Chroma)
```

| Module | Responsibility |
| --- | --- |
| `services/config.py` | Typed view of `settings.TEXT_TO_SQL` (all from env vars) |
| `services/catalog.py` | Schema catalog: tables, columns, FKs, samples; allow/deny rules |
| `services/ingestion.py` | Schema extraction, redaction, chunking, incremental embedding |
| `services/vector_store.py` | Embedding + vector store adapters (lazy clients) |
| `services/retrieval.py` | Relevant-table selection and prompt context |
| `services/sql_generation.py` | Prompt construction, LLM call, output cleanup |
| `services/sql_validator.py` | Security gate for generated SQL |
| `services/executor.py` | Safe execution against MySQL |
| `services/pipeline.py` | Orchestration and progress events |
| `services/exceptions.py` | Typed errors with user-safe messages |
| `services/rag_query.py`, `db_utils.py` | Backwards-compatible wrappers |

**Why a schema catalog?** Vector search only decides *which tables* are relevant.
The prompt is then built from the catalog's compact per-table text, so the LLM
never sees half a table, never sees the whole schema, and the same catalog
is the source of truth for which tables the validator allows.

## Security model

LLM output is treated as untrusted input. A query runs only if **all** of these hold:

1. **Parsed by sqlglot** into exactly one statement (stacked queries rejected).
2. **Read-only root**: `SELECT`, `UNION`/`INTERSECT`/`EXCEPT`, or a CTE over them.
3. **No forbidden nodes anywhere in the tree**: DML/DDL, `SET`, `SHOW`, `CALL`,
   `GRANT`, `LOAD DATA`, `SELECT … INTO`, `FOR UPDATE`, `@var`, `@@sysvar`, `:=`.
4. **No unsafe functions**: `SLEEP`, `BENCHMARK`, `LOAD_FILE`, lock functions,
   `EXTRACTVALUE`/`UPDATEXML`, `USER()`/`DATABASE()`/`VERSION()`, and others.
5. **Only allowed tables** from the catalog (minus `SQL_DENIED_TABLES` and the
   `auth_`, `django_`, `authtoken_` prefixes by default; optional
   `SQL_ALLOWED_TABLES`). No other schemas (`mysql`, `information_schema`, …).
6. **LIMIT enforced**: default `SQL_DEFAULT_LIMIT`, clamped to `SQL_MAX_ROWS`.
7. The SQL that runs is **regenerated from the AST**, so comments (including
   MySQL `/*! … */` executable comments) never reach the server.

At execution time: `SET SESSION TRANSACTION READ ONLY`, `START TRANSACTION READ ONLY`,
`MAX_EXECUTION_TIME` (or MariaDB `max_statement_time`), socket timeouts,
multi-statements and `LOCAL INFILE` disabled, at most `SQL_MAX_ROWS + 1` rows
fetched, and the transaction is always rolled back.

Around it: login + `chatbot.query_database` permission, CSRF on every POST,
per-user rate limit (`TEXT_TO_SQL_RATE_LIMIT_PER_MINUTE`), login throttling,
request size and question length limits, a strict Content-Security-Policy,
per-session (not global) chat history, error messages that never include driver
or stack details, secrets only from the environment, and an audit log.
Denied tables are never indexed, and sample values from sensitive-looking columns
(`password`, `email`, `phone`, `token`, …) are redacted before anything is sent
to the embedding provider or the LLM.

### Least-privilege database user

Point the app at an account that can only read the target schema:

```sql
CREATE USER 'sqlai_reader'@'%' IDENTIFIED BY '<strong password>';
GRANT SELECT, SHOW VIEW ON your_db.* TO 'sqlai_reader'@'%';
-- no INSERT/UPDATE/DELETE, FILE, PROCESS, SUPER or EXECUTE
```

For tighter control, grant `SELECT` on specific tables or views instead of `your_db.*`.
The bundled Docker MySQL creates such a user automatically
(`docker/mysql/init/01-readonly-user.sh`).

## Local setup (without Docker)

Requirements: Python 3.12, a MySQL 8 database, Redis Stack (for `VECTOR_STORE=redis`),
and a Google AI API key.

```bash
python -m venv venv
source venv/bin/activate            # Windows: venv\Scripts\activate
pip install -r requirements.txt
# optional: Chroma / HuggingFace backends
pip install -r requirements-optional.txt

cp .env.example .env                # then fill in DB_*, GOOGLE_API_KEY, DJANGO_SECRET_KEY
python manage.py migrate
python manage.py createsuperuser    # superusers have the query permission
python manage.py index_schema       # build the catalog and vector index
python manage.py runserver
```

Open http://localhost:8000/ and sign in. To let other users query, grant them
the **“Can query the database in natural language”** permission in `/admin/`
(or set `TEXT_TO_SQL_REQUIRE_PERMISSION=false`).

### Indexing the schema

```bash
python manage.py index_schema              # incremental: only changed tables are re-embedded
python manage.py index_schema --dry-run --show-chunks   # inspect chunks, embed nothing
python manage.py index_schema --force      # drop and rebuild the index
python create_mysql_schema_index.py        # old entry point, same command
```

Table descriptions come from `data/table_descriptions.json` (falling back to
MySQL table comments). Re-run the command after schema changes. Indexing is
checkpointed per table, so an interrupted run (e.g. an API rate limit) resumes
where it stopped. The first run with this version rebuilds any index created by
the old script.

## Docker

### Local development (`docker-compose.yml`)

```bash
cp .env.example .env    # fill in values
docker compose up --build
docker compose exec web python manage.py createsuperuser
docker compose exec web python manage.py index_schema
```

- `web`: Django dev server with auto-reload (`Dockerfile.local`, source bind-mounted).
- `redis`: Redis Stack with AOF persistence (`redis_data` volume) for vectors, cache and rate limits.
- `app_var` volume: Django SQLite DB, schema catalog, Chroma files.
- By default the app connects to MySQL **on your host** (`host.docker.internal:3306`).

To run a bundled MySQL instead (data in the `mysql_data` volume; a read-only
user is created from `DB_USER`/`DB_PASSWORD`):

```bash
# in .env: COMPOSE_DB_HOST=mysql, MYSQL_ROOT_PASSWORD=..., DB_NAME/DB_USER/DB_PASSWORD
docker compose --profile local-db up --build
```

Host ports default to 8000 (web), 6380 (Redis) and 3307 (MySQL) to avoid
clashing with locally installed services; override with `WEB_HOST_PORT`,
`REDIS_HOST_PORT` and `MYSQL_HOST_PORT`.

### Production image (`Dockerfile`)

Multi-stage build, non-root user, Gunicorn (`gthread`), WhiteNoise static
files, health check on `/healthz/`, state in the `/app/var` volume.

```bash
docker build -t sql-ai .
# optional backends: docker build --build-arg INSTALL_OPTIONAL=true -t sql-ai .

docker run -d --name sql-ai -p 8000:8000 \
  --env-file .env.production \
  -e DJANGO_DEBUG=false \
  -v sql_ai_var:/app/var \
  sql-ai
docker exec -it sql-ai python manage.py createsuperuser
docker exec -it sql-ai python manage.py index_schema
```

Production checklist: set `DJANGO_SECRET_KEY`, `DJANGO_ALLOWED_HOSTS`,
`DJANGO_CSRF_TRUSTED_ORIGINS`, `DJANGO_CACHE_URL` (shared Redis so rate limits
work across workers), terminate TLS at a proxy and set `DJANGO_BEHIND_PROXY=true`,
`DJANGO_SECURE_SSL_REDIRECT=true`, `DJANGO_SECURE_HSTS_SECONDS`. Use a secret
manager or orchestrator secrets to inject environment variables. Gunicorn is tuned
via `GUNICORN_WORKERS`, `GUNICORN_THREADS` and `GUNICORN_TIMEOUT`.

## API

All endpoints require an authenticated session and a CSRF token.

| Method & path | Body | Response |
| --- | --- | --- |
| `POST /chatbot/api/query/` | `{"question": "..."}` | JSON: `sql`, `columns`, `rows`, `result` (row objects, original format), `row_count`, `truncated`, `tables`, `execution_ms`, `timings`, `summary` |
| `POST /chatbot/api/query/stream/` | `{"question": "..."}` | NDJSON: `{"event":"stage",...}` lines, then `{"event":"result","data":{...}}` or `{"event":"error",...}` |
| `POST /chatbot/api/conversation/reset/` | – | Clears the follow-up question history |
| `GET /healthz/` | – | `{"status":"ok"}` (no auth) |

Errors return `{"error": "<safe message>", "code": "<stable code>"}` with
status 400/401/403/413/422/429/502/503/504; blocked queries also include the
rejected `sql`.

## Tests

```bash
python manage.py test chatbot
```

Unit tests cover SQL validation (DML/DDL, stacked queries, unsafe functions,
variables, cross-schema access, CTE tricks, limits), prompt injection (a
compromised model emitting malicious SQL, delimiter escapes), authorization,
CSRF, rate limits, API error handling, streaming, the executor's safety
settings and error mapping, retrieval and incremental indexing. External
services are replaced by fakes, so no keys or network are needed.

MySQL integration tests (real read-only user, write rejection, timeout, schema
extraction, end-to-end pipeline) run when `INTEGRATION_MYSQL_*` variables are
set; see `chatbot/tests/test_integration_mysql.py` for a one-line Docker setup.

## Screenshots and verification

Captured from a live run: real Gemini LLM and embeddings, Redis Stack, and a
MySQL 8.4 demo database with sample dairy data, queried through the read-only user.

| | |
| --- | --- |
| ![Sign in](docs/images/01-login.jpg) Sign-in is required | ![Empty state](docs/images/02-empty-state.jpg) Empty state with example questions |
| ![Progress](docs/images/03-streaming-progress.jpg) Streamed progress stages | ![Result](docs/images/04-result-table.jpg) Result: joins found via FK expansion |
| ![SQL preview](docs/images/05-sql-preview.jpg) Validated SQL preview | ![Second query](docs/images/06-second-query.jpg) Warm query (~3 s total) |
| ![Injection blocked](docs/images/07-injection-blocked.jpg) XSS shown as text; injection and `auth_user` requests refused | ![Audit log](docs/images/08-audit-log.jpg) Every question is audited |

Test suite (95 tests, including the 6 MySQL integration tests) and the
production image smoke test:

![Test results](docs/images/09-test-results.jpg)

![Docker smoke test](docs/images/10-docker-smoke-test.jpg)

## Configuration reference

See [`.env.example`](.env.example) for every setting with defaults.
