from django.contrib import admin

from .models import QueryLog


@admin.register(QueryLog)
class QueryLogAdmin(admin.ModelAdmin):
    list_display = ("created_at", "user", "status", "error_code", "row_count", "duration_ms", "question")
    list_filter = ("status", "error_code")
    search_fields = ("question", "generated_sql", "user__username")
    readonly_fields = [f.name for f in QueryLog._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False
