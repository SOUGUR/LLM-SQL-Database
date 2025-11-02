import os
import re
from typing import TypedDict, List
from langchain_core.prompts import PromptTemplate
from langchain_google_genai import ChatGoogleGenerativeAI
from .db_utils import validate_sql, execute_mysql_query
from langchain_huggingface import HuggingFaceEmbeddings
from langchain_chroma import Chroma

class RAGState(TypedDict, total=False):
    question: str
    retrieved_docs: List[str]
    generated_sql: str
    sql_result: List[dict]

# Initialize once
CHROMA_DIR = "./chroma_mysql"
EMBED_MODEL = "sentence-transformers/all-MiniLM-L6-v2"

embeddings = HuggingFaceEmbeddings(model_name=EMBED_MODEL)
vectordb = Chroma(
    persist_directory=CHROMA_DIR,
    embedding_function=embeddings,
    collection_name="mysql_schema"
)
retriever = vectordb.as_retriever(search_kwargs={"k": 8})

llm = ChatGoogleGenerativeAI(
    model="gemini-2.0-flash-exp",
    temperature=0,
    google_api_key=os.getenv("GOOGLE_API_KEY")
)

sql_prompt = PromptTemplate.from_template("""
You are a MySQL expert. Generate a SINGLE READ-ONLY SELECT query.
Rules:
- Only use tables from the context.
- No INSERT, UPDATE, DELETE, DROP, CREATE.
- Always include LIMIT 100.
- Return ONLY the SQL, no markdown.

Context:
{context}

Question:
{question}
""")


async def retriever_node(state: RAGState) -> RAGState:
    docs = await retriever.ainvoke(state["question"])
    state["retrieved_docs"] = [d.page_content for d in docs]
    return state


async def sql_generator_node(state: RAGState) -> RAGState:
    context = "\n\n".join(state.get("retrieved_docs", []))
    prompt_text = sql_prompt.format(context=context, question=state["question"])
    response = await llm.ainvoke(prompt_text)
    raw_sql = str(getattr(response, "content", "")).strip()
    
    # Clean
    raw_sql = re.sub(r"```(?:sql)?", "", raw_sql, flags=re.IGNORECASE)
    raw_sql = raw_sql.replace("```", "").strip()
    
    # Extract first SELECT statement
    match = re.search(r"(SELECT\s+.+?)(?:;|$)", raw_sql, re.IGNORECASE | re.DOTALL)
    sql = match.group(1).strip() if match else ""
    
    # Enforce LIMIT
    if sql and "LIMIT" not in sql.upper():
        sql += " LIMIT 100"
        
    state["generated_sql"] = sql
    return state


async def process_text_to_sql(question: str):
    state: RAGState = {"question": question}
    
    state = await retriever_node(state)
    state = await sql_generator_node(state)
    
    sql = state["generated_sql"]
    if not sql:
        raise ValueError("Could not generate valid SQL.")
    
    # Validate strictly
    is_safe, reason = validate_sql(sql)
    if not is_safe:
        raise ValueError(f"Unsafe SQL: {reason}")
    
    # Execute on MySQL
    result = execute_mysql_query(sql)
    
    return {
        "sql": sql,
        "result": result
    }