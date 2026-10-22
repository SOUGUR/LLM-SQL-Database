"""Index the MySQL schema into the vector store.

Kept for backwards compatibility; equivalent to:

    python manage.py index_schema [--force] [--dry-run] [--show-chunks]
"""

import os
import sys

import django
from django.core.management import call_command

if __name__ == "__main__":
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "SQL_AI.settings")
    django.setup()
    call_command("index_schema", *sys.argv[1:])
