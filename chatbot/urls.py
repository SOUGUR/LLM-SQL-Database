from django.urls import path

from .views import ResetConversationView, TextToSQLStreamView, TextToSQLView

urlpatterns = [
    path('api/query/', TextToSQLView.as_view(), name='text-to-sql'),
    path('api/query/stream/', TextToSQLStreamView.as_view(), name='text-to-sql-stream'),
    path('api/conversation/reset/', ResetConversationView.as_view(), name='conversation-reset'),
]
