"""Typed pipeline errors.

Each error carries a ``public_message`` that is safe to show to end users and
a stable ``code`` for the API. Internal details (stack traces, driver error
text, raw LLM output) are logged, never returned.
"""


class TextToSQLError(Exception):
    code = "internal_error"
    status = 500
    public_message = "Something went wrong while answering your question."

    def __init__(self, detail: str = "", public_message: str | None = None):
        super().__init__(detail or self.public_message)
        self.detail = detail
        # SQL involved in the failure (LLM candidate or validated SQL), for audit/UI.
        self.sql = ""
        if public_message:
            self.public_message = public_message


class InvalidQuestionError(TextToSQLError):
    code = "invalid_question"
    status = 400
    public_message = "Please enter a valid question."


class SQLGenerationError(TextToSQLError):
    code = "generation_failed"
    status = 422
    public_message = "I couldn't turn that question into a query. Try rephrasing it."


class UnanswerableQuestionError(TextToSQLError):
    code = "unanswerable"
    status = 422
    public_message = (
        "I couldn't find tables that answer that question. "
        "Try mentioning the data you're interested in more specifically."
    )


class UnsafeSQLError(TextToSQLError):
    code = "unsafe_sql"
    status = 422
    public_message = "The generated query was blocked by the safety checks."


class QueryTimeoutError(TextToSQLError):
    code = "query_timeout"
    status = 504
    public_message = "The query took too long and was stopped. Try narrowing your question."


class QueryExecutionError(TextToSQLError):
    code = "execution_failed"
    status = 502
    public_message = "The database could not run the generated query. Try rephrasing your question."


class UpstreamServiceError(TextToSQLError):
    code = "upstream_unavailable"
    status = 503
    public_message = "A required service (LLM, vector store or database) is unavailable. Please try again shortly."


class SchemaNotIndexedError(TextToSQLError):
    code = "schema_not_indexed"
    status = 503
    public_message = "The database schema has not been indexed yet. Run `python manage.py index_schema`."
