from django.urls import path

from . import views

app_name = "assistant"

urlpatterns = [
    path("ask/", views.ask, name="ask"),
    path("history/", views.history, name="history"),
    path("history/clear/", views.clear_history, name="clear_history"),
]
