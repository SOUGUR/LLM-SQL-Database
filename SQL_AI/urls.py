"""URL configuration for SQL_AI project.

``/healthz/`` is answered by ``chatbot.middleware.HealthCheckMiddleware``.
"""
from django.contrib import admin
from django.contrib.auth.views import LogoutView
from django.urls import include, path

from chatbot.views import ChatView, RateLimitedLoginView

urlpatterns = [
    path('admin/', admin.site.urls),
    path('accounts/login/', RateLimitedLoginView.as_view(), name='login'),
    path('accounts/logout/', LogoutView.as_view(), name='logout'),
    path('chatbot/', include('chatbot.urls')),
    path('', ChatView.as_view(), name='home'),
]
