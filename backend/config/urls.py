from django.contrib import admin
from django.urls import include, path

from apps.core.views import AttachmentDownloadView
from apps.core.web_views import UiShowcaseView
from apps.filings.views import DashboardView

urlpatterns = [
    path("", DashboardView.as_view(), name="home"),
    path("ui/", UiShowcaseView.as_view(), name="ui_showcase"),
    path("accounts/", include("apps.core.urls")),
    path("workers/", include("apps.workers.urls")),
    path("hosts/", include("apps.hosts.urls")),
    path("filings/", include("apps.filings.urls")),
    path(
        "attachments/<int:pk>/download/",
        AttachmentDownloadView.as_view(),
        name="attachment_download",
    ),
    path("admin/", admin.site.urls),
]
