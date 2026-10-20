from django.urls import path

from apps.forms_engine import bulk_views, efile_views, views

app_name = "forms"

urlpatterns = [
    path("documents/<int:pk>/", views.GeneratedDocumentView.as_view(), name="document"),
    path(
        "documents/<int:pk>/filled/",
        views.GeneratedFilledDownloadView.as_view(),
        name="document_filled",
    ),
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
    path("efile/", efile_views.EfileListView.as_view(), name="efile_list"),
    path("efile/new/", efile_views.EfileCreateView.as_view(), name="efile_create"),
    path("efile/<int:pk>/", efile_views.EfileDetailView.as_view(), name="efile"),
    path("efile/<int:pk>/filings/", efile_views.EfileAddFilingsView.as_view(), name="efile_add"),
    path(
        "efile/<int:pk>/submission/",
        efile_views.EfileSubmissionView.as_view(),
        name="efile_submission",
    ),
    path(
        "efile/<int:pk>/rows/<int:row_id>/",
        efile_views.EfileRowView.as_view(),
        name="efile_row",
    ),
    path(
        "efile/<int:pk>/rows/<int:row_id>/remove/",
        efile_views.EfileRowRemoveView.as_view(),
        name="efile_row_remove",
    ),
    path(
        "efile/files/<int:file_id>/download/",
        efile_views.EfileFileDownloadView.as_view(),
        name="efile_file",
    ),
    path(
        "bulk/files/<int:file_id>/download/",
        bulk_views.BulkFileDownloadView.as_view(),
        name="bulk_file",
    ),
]
