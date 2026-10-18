from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import include, path
from django.views.generic import RedirectView

from config.views import healthz
from library.views import cover

admin.site.site_header = "Benrey Library Admin"
admin.site.site_title = "Benrey Library"
admin.site.index_title = "Circulation and catalogue"

urlpatterns = [
    path("healthz", healthz, name="healthz"),
    path("assistant/", include("assistant.urls")),
    path("covers/<str:name>", cover, name="cover"),
    path(settings.ADMIN_URL, admin.site.urls),
    # The admin is the whole app; send visitors to its login page.
    path("", RedirectView.as_view(pattern_name="admin:index", permanent=False)),
]

if settings.DEBUG and settings.USE_LOCAL_STORAGE:
    # Development only (static() is a no-op when DEBUG is off): serve uploads
    # from local disk. Production always uses the bucket.
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
