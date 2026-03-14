import os
import json 
from pathlib import Path
from dotenv import load_dotenv
import pymysql
from langchain_community.vectorstores import Chroma
# from langchain_community.embeddings import HuggingFaceEmbeddings
from langchain_google_genai import GoogleGenerativeAIEmbeddings
from langchain_text_splitters import RecursiveCharacterTextSplitter
from langchain_core.documents import Document
import time


load_dotenv()

BASE_DIR = Path(__file__).resolve().parent

DESCRIPTIONS_PATH = BASE_DIR / "data" / "table_descriptions.json"

if DESCRIPTIONS_PATH.exists():
    with open(DESCRIPTIONS_PATH, "r", encoding="utf-8") as f:
        table_descriptions = json.load(f)
    print("Loaded table descriptions from data/table_descriptions.json")
else:
    print("Warning: table_descriptions.json not found in /data folder.")
    table_descriptions = {}

# connect to your local database
conn = pymysql.connect(
    host=os.getenv("DB_HOST"),
    user=os.getenv("DB_USER"),
    password=os.getenv("DB_PASSWORD"),
    database=os.getenv("DB_NAME"),
    port=int(os.getenv("DB_PORT", 3306)),
    charset='utf8mb4'
)

cursor = conn.cursor()
cursor.execute("SHOW TABLES")
# get all table names
tables = [row[0] for row in cursor.fetchall()]

cursor.execute(f"""
    SELECT 
        TABLE_NAME, 
        COLUMN_NAME, 
        REFERENCED_TABLE_NAME, 
        REFERENCED_COLUMN_NAME
    FROM INFORMATION_SCHEMA.KEY_COLUMN_USAGE
    WHERE REFERENCED_TABLE_NAME IS NOT NULL
      AND TABLE_SCHEMA = '{os.getenv("DB_NAME")}'
""")
fk_results = cursor.fetchall()

foreign_keys = {}
for (table, column, ref_table, ref_col) in fk_results:
    if table not in foreign_keys:
        foreign_keys[table] = []
    foreign_keys[table].append(
        f"{table}.{column} → {ref_table}.{ref_col}"
    )


docs = []

documents = []

for table in tables:
    cursor.execute(f"DESCRIBE `{table}`")
    cols = cursor.fetchall()

    schema = f"Table: {table}\n"

    # Add JSON description of tables
    if table in table_descriptions:
        schema += "\nDescription:\n"
        schema += table_descriptions[table] + "\n"

    # Columns
    schema += "\nColumns:\n"
    for col in cols:
        schema += f"- {col[0]} ({col[1]})\n"

    # Foreign Keys
    if table in foreign_keys:
        schema += "\nRelationships:\n"
        for rel in foreign_keys[table]:
            schema += f"- {rel}\n"

    # Create Document object ---- THIS IS THE NEW PART
    documents.append(
        Document(
            page_content=schema,
            metadata={
                "table": table,
                "module": table.split("_")[0] 
            }
        )
    )
conn.close()

# Embedding model name
# embeddings = HuggingFaceEmbeddings(model_name="sentence-transformers/all-MiniLM-L6-v2")


embeddings = GoogleGenerativeAIEmbeddings(
    model="gemini-embedding-2-preview",
    batch_size=50
)

# strategy for chunking - Recursive Charater Text Splitter
text_splitter = RecursiveCharacterTextSplitter(
    chunk_size=1200,
    chunk_overlap=100,
    separators=["\n\n", "\n", ".", " ", ""]
)

all_chunks = []

# for doc in docs:
#     chunks = text_splitter.split_text(doc)
#     all_chunks.extend(chunks)

split_docs = text_splitter.split_documents(documents)

# check embedding and store them in vector storage
# vectorstore = Chroma.from_texts(
#     texts=all_chunks,
#     embedding=embeddings,
#     persist_directory="./chroma_mysql",
#     collection_name="mysql_schema"
# )

# Store in Chroma using documents (NOT texts)
# vectorstore = Chroma.from_documents(
#     documents=split_docs,
#     embedding=embeddings,
#     persist_directory="./chroma_mysql",
#     collection_name="mysql_schema"
# )

batch_size = 5  # Small batches to stay safe under the 100 RPM limit
vectorstore = None

print(f"Indexing {len(split_docs)} chunks into Chroma...")

for i in range(0, len(split_docs), batch_size):
    batch = split_docs[i : i + batch_size]
    
    if vectorstore is None:
        vectorstore = Chroma.from_documents(
            documents=batch,
            embedding=embeddings,
            persist_directory="./chroma_mysql",
            collection_name="mysql_schema"
        )
    else:
        vectorstore.add_documents(batch)
    
    print(f"Indexed chunks {i} to {i + len(batch)}")
    # Pause for 2 seconds between batches to avoid 429 errors
    time.sleep(2) 

print("Indexing complete!")

vectorstore.persist()
print("MySQL schema + sample rows indexed into Chroma at ./chroma_mysql")