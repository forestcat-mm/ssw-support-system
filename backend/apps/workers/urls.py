from django.urls import path

from apps.support import record_views
from apps.support import views as support_views
from apps.workers import document_views, views

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
    path(
        "<int:pk>/support/consultations/new/",
        record_views.ConsultationCreateView.as_view(),
        name="support_consultation_create",
    ),
    path(
        "<int:pk>/support/consultations/<int:consultation_id>/",
        record_views.ConsultationDetailView.as_view(),
        name="support_consultation",
    ),
    path(
        "<int:pk>/support/consultations/<int:consultation_id>/entries/new/",
        record_views.ConsultationEntryView.as_view(),
        name="support_consultation_entry",
    ),
    path(
        "<int:pk>/support/consultations/<int:consultation_id>/void/",
        record_views.ConsultationVoidView.as_view(),
        name="support_consultation_void",
    ),
    path(
        "<int:pk>/support/briefings/new/<str:kind>/",
        record_views.BriefingCreateView.as_view(),
        name="support_briefing_create",
    ),
    path(
        "<int:pk>/support/briefings/<int:briefing_id>/",
        record_views.BriefingDetailView.as_view(),
        name="support_briefing",
    ),
    path(
        "<int:pk>/support/briefings/<int:briefing_id>/confirmation/",
        record_views.BriefingConfirmationView.as_view(),
        name="support_briefing_confirmation",
    ),
    path(
        "<int:pk>/support/briefings/<int:briefing_id>/void/",
        record_views.BriefingVoidView.as_view(),
        name="support_briefing_void",
    ),
    path(
        "<int:pk>/support/deliveries/",
        record_views.DeliveryListView.as_view(),
        name="support_deliveries",
    ),
    path(
        "<int:pk>/support/deliveries/new/<str:kind>/",
        record_views.DeliveryCreateView.as_view(),
        name="support_delivery_create",
    ),
    path(
        "<int:pk>/support/deliveries/<int:delivery_id>/",
        record_views.DeliveryDetailView.as_view(),
        name="support_delivery",
    ),
    path(
        "<int:pk>/support/deliveries/<int:delivery_id>/void/",
        record_views.DeliveryVoidView.as_view(),
        name="support_delivery_void",
    ),
    path(
        "<int:pk>/support/deliveries/<int:delivery_id>/attachments/new/",
        record_views.DeliveryAttachView.as_view(),
        name="support_delivery_attach",
    ),
    path(
        "<int:pk>/support/deliveries/<int:delivery_id>/attachments/<int:attachment_id>/void/",
        record_views.DeliveryAttachmentVoidView.as_view(),
        name="support_delivery_attachment_void",
    ),
    path(
        "<int:pk>/health-documents/new/",
        document_views.HealthDocumentAddView.as_view(),
        name="health_document_add",
    ),
    path(
        "<int:pk>/health-documents/<int:attachment_id>/void/",
        document_views.HealthDocumentVoidView.as_view(),
        name="health_document_void",
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
