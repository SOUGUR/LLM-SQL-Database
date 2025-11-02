from django.urls import path
from .views import TextToSQLView

urlpatterns = [
    path('api/query/', TextToSQLView.as_view(), name='text-to-sql'),
]