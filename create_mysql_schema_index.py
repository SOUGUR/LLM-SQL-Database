import os
import json 
from pathlib import Path
from dotenv import load_dotenv
import pymysql
from langchain_community.vectorstores import Chroma
from langchain_community.embeddings import HuggingFaceEmbeddings

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
for table in tables:
    cursor.execute(f"DESCRIBE `{table}`")  
    cols = cursor.fetchall()
    schema = f"Table: {table}\nColumns:\n"
    for col in cols:
        schema += f"- {col[0]} ({col[1]})\n"

    if table in foreign_keys:
        schema += "\nRelationships:\n"
        for rel in foreign_keys[table]:
            schema += f"- {rel}\n"

    try:
        cursor.execute(f"SELECT * FROM `{table}` LIMIT 20")
        samples = cursor.fetchall()
        if samples:
            schema += "\nSample rows:\n"
            for row in samples:
                schema += f"{row}\n"

    except Exception as e:
        print(f"Warning: Could not fetch samples from {table}: {e}")

    docs.append(schema)

conn.close()

# Embed and store
embeddings = HuggingFaceEmbeddings(model_name="sentence-transformers/all-MiniLM-L6-v2")
vectorstore = Chroma.from_texts(
    texts=docs,
    embedding=embeddings,
    persist_directory="./chroma_mysql",
    collection_name="mysql_schema"
)
vectorstore.persist()
print("MySQL schema + sample rows indexed into Chroma at ./chroma_mysql")