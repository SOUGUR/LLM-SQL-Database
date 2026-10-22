from django.conf import settings
from django.db import models


class QueryLog(models.Model):
    """Audit trail of natural-language queries and the SQL that was run."""

    class Status(models.TextChoices):
        SUCCESS = "success", "Success"
        REJECTED = "rejected", "Rejected"
        ERROR = "error", "Error"

    user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL)
    question = models.TextField()
    generated_sql = models.TextField(blank=True)
    status = models.CharField(max_length=16, choices=Status.choices)
    error_code = models.CharField(max_length=64, blank=True)
    row_count = models.IntegerField(null=True, blank=True)
    duration_ms = models.FloatField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]
        permissions = [("query_database", "Can query the database in natural language")]

    def __str__(self):
        return f"{self.created_at:%Y-%m-%d %H:%M} {self.status}: {self.question[:60]}"
