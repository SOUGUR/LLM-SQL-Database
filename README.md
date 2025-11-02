# LLM-SQL-Database

Chat with any complex database in English and get the results you want quickly — built with Django, local SQL DB, and LLM-powered translation.

## Project Description

This project enables users to interact with complex databases through natural language (English) prompts. It leverages retrieval-augmented generation (RAG), semantic embeddings, and a Django backend to convert English queries into accurate SQL statements, fetches results from your local database, and displays them to users. Inspired by the original FastAPI+Langchain implementation, this version is adapted for Django and connects to your local database.

## Features

- **Natural Language to SQL**: Users enter questions in English, and the system generates and validates SQL to fetch accurate results.
- **RAG (Retrieval-Augmented Generation) Pipeline**: Leverages semantic retrieval of your database schema for precise query grounding.
- **Django Integration**: Uses Django Views, URL routing, and template rendering for a full-stack solution.
- **Custom Database Support**: Works with your local database configuration; supports multi-table, normalized, or denormalized complex schemas.
- **Safety**: Only allows safe, validated, auto-generated SELECT queries (no write/DDL/DML operations).
- **Extensible**: Modular architecture, easily adaptable to new DB schemas or LLM providers, with clear separation of retrieval, generation, validation, and execution logic.

## Architecture

```text
User (Django Web/Template UI)
       ↓
Django View / API Endpoint
       ↓
RAG Pipeline (Retriever - Embeddings, Schema Index, LLM Prompt)
       ↓
(Django ORM / Raw SQL Executor)
       ↓
Database (Local: SQLite, MySQL, PostgreSQL, etc.)
       ↓
Result Rendered in Template/UI
```

## Setup

### 1. Clone the repository

```bash
git clone https://github.com/SOUGUR/LLM-SQL-Database.git
cd LLM-SQL-Database
```

### 2. Set up a virtual environment and install dependencies

```bash
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 3. Configure your database

- Set up your `DATABASES` config in `settings.py` for your local DB (SQLite/MySQL/PostgreSQL/etc).
- Prepare `.env` with model and DB credentials if required.

### 4. Prepare your schema embeddings

Use provided scripts in `SQL_AI` or similar (see docs/scripts inside) to index your DB schema and sample data for semantic retrieval. Typically, these scripts will:

- Extract table and column names from your DB.
- Embed them using HuggingFace or similar models.
- Persist embedding vectors for fast retrieval.

### 5. Run migrations & start the Django server

```bash
python manage.py migrate
python manage.py runserver
```

### 6. Access the chatbot interface

Open `http://localhost:8000/` and ask questions about your database in plain English.

**Example**: "Show total orders for each customer last month."

## Usage Example

**User Inputs**: "Which products sold most in October?"

**Backend Flow**:
1. Retrieves relevant schema using vector search.
2. Generates SQL via LLM, grounded to your actual schema.
3. Validates query to ensure safety.
4. Executes safely on your database.

**Display**: Tabular results shown on the frontend.

## Customization

- **Database Support**: Update your schema extraction/indexing scripts and ORM as needed.
- **LLM**: Swap out LLM provider in generation step (OpenAI, GPT, Code Llama, Gemini, etc).
- **Retrieval**: Enhance retrieval logic for joint/multi-table relationships or for more complex DB layouts.
- **Frontend**: Adapt Django templates for your organization's styling and needs.

## Security Notice

**Important**:
- The app only supports auto-generated, safe, read-only SELECT queries by default.
- Do not deploy in production without further security auditing and hardening.

<img width="1919" height="857" alt="Screenshot 2025-10-20 171520" src="https://github.com/user-attachments/assets/6ffd242f-fb10-4fb9-a853-016bca8df30e" />
<img width="1919" height="829" alt="Screenshot 2025-10-20 163343" src="https://github.com/user-attachments/assets/92f09edc-2bdc-42ff-a49d-76c35c88f3f9" />
<img width="1912" height="832" alt="Screenshot 2025-10-20 163327" src="https://github.com/user-attachments/assets/8add5208-ebb4-43ff-9b45-15947491d338" />

