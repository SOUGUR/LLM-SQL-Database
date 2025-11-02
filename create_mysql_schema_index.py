import os
from dotenv import load_dotenv
import pymysql
from langchain_community.vectorstores import Chroma
from langchain_community.embeddings import HuggingFaceEmbeddings

load_dotenv()

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

docs = []
for table in tables:
    cursor.execute(f"DESCRIBE `{table}`")  
    cols = cursor.fetchall()
    schema = f"Table: {table}\nColumns:\n"
    for col in cols:
        schema += f"- {col[0]} ({col[1]})\n"

    try:
        cursor.execute(f"SELECT * FROM `{table}` LIMIT 3")
        samples = cursor.fetchall()
        if samples:
            sample_lines = ["Sample rows:"]
            for row in samples:
                sample_lines.append(str(row))
            schema += "\n" + "\n".join(sample_lines)
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