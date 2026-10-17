from django.urls import path

from apps.support import record_views
from apps.support import views as support_views
from apps.workers import views

app_name = "workers"

urlpatterns = [
    path("", views.WorkerListView.as_view(), name="list"),
    path("new/", views.WorkerCreateView.as_view(), name="create"),
    path("<int:pk>/", views.WorkerDetailView.as_view(), name="detail"),
    path("<int:pk>/contracts/", views.WorkerContractsView.as_view(), name="contracts"),
    path("<int:pk>/filings/", views.WorkerFilingsView.as_view(), name="filings"),
    path("<int:pk>/history/", views.WorkerHistoryView.as_view(), name="history"),
    path("<int:pk>/edit/", views.WorkerUpdateView.as_view(), name="edit"),
    path("<int:pk>/support/", support_views.SupportPlanTabView.as_view(), name="support"),
    path(
        "<int:pk>/support/plans/new/",
        support_views.PlanCreateView.as_view(),
        name="support_plan_create",
    ),
    path(
        "<int:pk>/support/plans/<int:plan_id>/versions/new/",
        support_views.VersionCreateView.as_view(),
        name="support_version_create",
    ),
    path(
        "<int:pk>/support/records/",
        record_views.RecordListView.as_view(),
        name="support_records",
    ),
    path(
        "<int:pk>/support/records/new/<str:kind>/",
        record_views.RecordCreateView.as_view(),
        name="support_record_create",
    ),
    path(
        "<int:pk>/support/records/<int:record_id>/",
        record_views.RecordDetailView.as_view(),
        name="support_record",
    ),
    path(
        "<int:pk>/support/records/<int:record_id>/void/",
        record_views.RecordVoidView.as_view(),
        name="support_record_void",
    ),
    path(
        "<int:pk>/support/records/<int:record_id>/attachments/new/",
        record_views.AttachmentAddView.as_view(),
        name="support_record_attach",
    ),
    path(
        "<int:pk>/support/records/<int:record_id>/attachments/<int:attachment_id>/void/",
        record_views.AttachmentVoidView.as_view(),
        name="support_attachment_void",
    ),
    path(
        "<int:pk>/support/interviews/new/",
        record_views.InterviewCreateView.as_view(),
        name="support_interview_create",
    ),
    path(
        "<int:pk>/support/interviews/<int:interview_id>/",
        record_views.InterviewDetailView.as_view(),
        name="support_interview",
    ),
    path(
        "<int:pk>/support/interviews/<int:interview_id>/void/",
        record_views.InterviewVoidView.as_view(),
        name="support_interview_void",
    ),
    path("<int:pk>/deactivate/", views.WorkerDeactivateView.as_view(), name="deactivate"),
    path("<int:pk>/reactivate/", views.WorkerReactivateView.as_view(), name="reactivate"),
    path(
        "<int:pk>/<str:kind>/new/",
        views.RecordCreateView.as_view(),
        name="record_create",
    ),
    path(
        "<int:pk>/history/<str:kind>/<int:record_id>/void/",
        views.RecordVoidView.as_view(),
        name="record_void",
    ),
]
