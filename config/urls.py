from django.conf import settings
from django.contrib import admin
from django.urls import include, path
from django.views.generic import RedirectView

from config.views import healthz

admin.site.site_header = "Benrey Library Admin"
admin.site.site_title = "Benrey Library"
admin.site.index_title = "Circulation and catalogue"

urlpatterns = [
    path("healthz", healthz, name="healthz"),
    path("assistant/", include("assistant.urls")),
    path(settings.ADMIN_URL, admin.site.urls),
    # The admin is the whole app; send visitors to its login page.
    path("", RedirectView.as_view(pattern_name="admin:index", permanent=False)),
]
