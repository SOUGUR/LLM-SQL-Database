import os
import re
from typing import TypedDict, List
from langchain_core.prompts import PromptTemplate
from langchain_google_genai import ChatGoogleGenerativeAI
from .db_utils import validate_sql, execute_mysql_query
# from langchain_huggingface import HuggingFaceEmbeddings
from langchain_google_genai import GoogleGenerativeAIEmbeddings
# from langchain_chroma import Chroma
from langchain_community.vectorstores import Redis
from langchain_classic.memory import ConversationBufferWindowMemory


class RAGState(TypedDict, total=False):
    question: str
    retrieved_docs: List[str]
    generated_sql: str
    sql_result: List[dict]


CHROMA_DIR = "./chroma_mysql"
# EMBED_MODEL = "sentence-transformers/all-MiniLM-L6-v2"

# embeddings = HuggingFaceEmbeddings(model_name=EMBED_MODEL)
embeddings = GoogleGenerativeAIEmbeddings(
    model="gemini-embedding-2-preview",
    google_api_key=os.getenv("GOOGLE_API_KEY"),
    batch_size=50
)


# vectordb = Chroma(
#     persist_directory=CHROMA_DIR,
#     embedding_function=embeddings,
#     collection_name="mysql_schema"
# )

vectordb = Redis(
    redis_url="redis://localhost:6379",
    index_name="mysql_schema",
    embedding=embeddings
)
retriever = vectordb.as_retriever(search_kwargs={"k": 10})

llm = ChatGoogleGenerativeAI(
    model="gemini-2.5-flash", 
    temperature=0,
    google_api_key=os.getenv("GOOGLE_API_KEY")
)

memory = ConversationBufferWindowMemory(
    k=3,  # number of past interactions to keep
    memory_key="chat_history",
    return_messages=True
)

sql_prompt = PromptTemplate.from_template("""
You are a MySQL expert. Generate a SINGLE READ-ONLY SELECT query.

Chat History:
{chat_history}
                                          
Rules:
- Only use tables from the context.
- No INSERT, UPDATE, DELETE, DROP, CREATE.
- Return ONLY the SQL, no markdown.
Mandatory Output Rules:
- Never return only *_id fields.
- If a foreign key like party_id, product_id, dispatch_id appears in SELECT,
  you MUST join the related table and include a readable column
  such as name, code, title, invoice_num, order_num.
                                          
Context:
{context}

Question:
{question}
""")


def retriever_node(state: RAGState) -> RAGState:
    docs = retriever.invoke(state["question"])
    state["retrieved_docs"] = [
    f"Table: {d.metadata.get('table')}\n{d.page_content}"
        for d in docs
        ]
    return state


def sql_generator_node(state: RAGState) -> RAGState:
    context = "\n\n".join(state.get("retrieved_docs", []))
    # prompt_text = sql_prompt.format(context=context, question=state["question"])
    memory_vars = memory.load_memory_variables({})
    messages = memory_vars.get("chat_history", [])

    chat_history = "\n".join(
        f"{m.type.upper()}: {m.content}" for m in messages
    )

    prompt_text = sql_prompt.format(
        chat_history=chat_history,
        context=context,
        question=state["question"]
    )
    response = llm.invoke(prompt_text)
    raw_sql = str(getattr(response, "content", "")).strip()
    
    # Clean
    raw_sql = re.sub(r"```(?:sql)?", "", raw_sql, flags=re.IGNORECASE)
    raw_sql = raw_sql.replace("```", "").strip()
    
    # Extract first SELECT statement
    # match = re.search(r"(SELECT\s+.+?)(?:;|$)", raw_sql, re.IGNORECASE | re.DOTALL)
    # sql = match.group(1).strip() if match else ""
    sql = ""

    if raw_sql:
        sql = raw_sql.rstrip(";")

        # Enforce LIMIT
        if "LIMIT" not in sql.upper():
            sql += " LIMIT 100"

    state["generated_sql"] = sql
    return state


def process_text_to_sql(question: str):
    state: RAGState = {"question": question}
    
    state =  retriever_node(state)
    state =  sql_generator_node(state)
    
    sql = state["generated_sql"]
    if not sql:
        raise ValueError("Could not generate valid SQL.")
    
    # Validate strictly
    is_safe, reason = validate_sql(sql)
    if not is_safe:
        raise ValueError(f"Unsafe SQL: {reason}")
    
    # Execute on MySQL
    result = execute_mysql_query(sql)

    memory.save_context(
    {"input": question},
    {"output": sql}
    )

    return {
        "sql": sql,
        "result": result
    }