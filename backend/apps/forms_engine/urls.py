from django.urls import path

from apps.forms_engine import bulk_views, views

app_name = "forms"

urlpatterns = [
    path("documents/<int:pk>/", views.GeneratedDocumentView.as_view(), name="document"),
    path(
        "documents/<int:pk>/pdf/",
        views.GeneratedPdfDownloadView.as_view(),
        name="generated_pdf",
    ),
    # 一括申請（T48）
    path("bulk/", bulk_views.BulkListView.as_view(), name="bulk_list"),
    path("bulk/new/", bulk_views.BulkCreateView.as_view(), name="bulk_create"),
    path("bulk/<int:pk>/", bulk_views.BulkDetailView.as_view(), name="bulk"),
    path("bulk/<int:pk>/workers/", bulk_views.BulkAddWorkersView.as_view(), name="bulk_add"),
    path(
        "bulk/<int:pk>/entries/<int:entry_id>/",
        bulk_views.BulkEntryView.as_view(),
        name="bulk_entry",
    ),
    path(
        "bulk/<int:pk>/entries/<int:entry_id>/remove/",
        bulk_views.BulkEntryRemoveView.as_view(),
        name="bulk_entry_remove",
    ),
    path(
        "bulk/files/<int:file_id>/download/",
        bulk_views.BulkFileDownloadView.as_view(),
        name="bulk_file",
    ),
]
