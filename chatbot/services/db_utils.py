import os
import sqlglot
import pymysql
from dotenv import load_dotenv

load_dotenv()

def validate_sql(sql: str):
    try:
        parsed = sqlglot.parse(sql, dialect="mysql")[0]
        if not parsed:
            return False, "Empty or invalid SQL"
        
        # Check for dangerous operations
        unsafe_ops = ["Insert", "Update", "Delete", "Drop", "Create", "Alter", "Truncate"]
        for node in parsed.walk():
            class_name = node.__class__.__name__
            if class_name in unsafe_ops:
                return False, f"Prohibited operation: {class_name}"
        
    
        if parsed.__class__.__name__ != "Select":
            return False, "Only SELECT queries allowed"
            
        return True, ""
    except Exception as e:
        return False, f"Parsing error: {str(e)}"


def execute_mysql_query(sql: str):
    conn = pymysql.connect(
        host=os.getenv("DB_HOST"),
        user=os.getenv("DB_USER"),
        password=os.getenv("DB_PASSWORD"),
        database=os.getenv("DB_NAME"),
        port=int(os.getenv("DB_PORT", 3306)),
        charset='utf8mb4',
        read_timeout=10,
        cursorclass=pymysql.cursors.DictCursor
    )
    try:
        with conn.cursor() as cur:
            cur.execute(sql)
            rows = cur.fetchall()
        return list(rows)
    finally:
        conn.close()