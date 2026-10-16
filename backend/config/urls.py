from django.contrib import admin
from django.urls import include, path

from apps.core.views import AttachmentDownloadView

urlpatterns = [
    path("accounts/", include("apps.core.urls")),
    path(
        "attachments/<int:pk>/download/",
        AttachmentDownloadView.as_view(),
        name="attachment_download",
    ),
    path("admin/", admin.site.urls),
]
