from django.urls import path

from apps.contracts import template_views
from apps.forms_engine.views import ConsultationRecordView, PeriodicReportView
from apps.hosts import application_views, career_plan_views, document_views, edit_views, views

app_name = "hosts"

urlpatterns = [
    path("", views.HostListView.as_view(), name="list"),
    path("new/", edit_views.HostEditView.as_view(), name="create"),
    path("<int:pk>/", views.HostDetailView.as_view(), name="detail"),
    path("<int:pk>/edit/", edit_views.HostEditView.as_view(), name="edit"),
    path(
        "<int:pk>/career-plans/new/",
        career_plan_views.CareerPlanEditView.as_view(),
        name="career_plan_create",
    ),
    path(
        "<int:pk>/career-plans/<int:plan_id>/",
        career_plan_views.CareerPlanDetailView.as_view(),
        name="career_plan",
    ),
    path(
        "<int:pk>/career-plans/<int:plan_id>/versions/new/",
        career_plan_views.CareerPlanEditView.as_view(),
        name="career_plan_version_create",
    ),
    path(
        "<int:pk>/career-plans/<int:plan_id>/versions/<int:number>/edit/",
        career_plan_views.CareerPlanEditView.as_view(),
        name="career_plan_version_edit",
    ),
    path(
        "<int:pk>/consultation-records/",
        ConsultationRecordView.as_view(),
        name="consultation_records",
    ),
    path("<int:pk>/periodic-report/", PeriodicReportView.as_view(), name="periodic_report"),
    path(
        "<int:pk>/condition-templates/new/",
        template_views.ConditionTemplateEditView.as_view(),
        name="condition_template_create",
    ),
    path(
        "<int:pk>/condition-templates/<int:template_id>/",
        template_views.ConditionTemplateEditView.as_view(),
        name="condition_template_edit",
    ),
    path(
        "<int:pk>/documents/new/",
        document_views.HostDocumentAddView.as_view(),
        name="host_document_create",
    ),
    path(
        "<int:pk>/documents/<int:document_id>/void/",
        document_views.HostDocumentVoidView.as_view(),
        name="host_document_void",
    ),
    # 一括申請の項目（T46b）
    path(
        "<int:pk>/application/declarations/new/",
        application_views.DeclarationCreateView.as_view(),
        name="declaration_create",
    ),
    path(
        "<int:pk>/application/declarations/<int:row_id>/void/",
        application_views.DeclarationVoidView.as_view(),
        {"kind": "declarations"},
        name="declaration_void",
    ),
    path(
        "<int:pk>/application/<str:kind>/new/",
        application_views.HostRowEditView.as_view(),
        name="application_row_create",
    ),
    path(
        "<int:pk>/application/<str:kind>/<int:row_id>/",
        application_views.HostRowEditView.as_view(),
        name="application_row_edit",
    ),
    path(
        "<int:pk>/application/<str:kind>/<int:row_id>/void/",
        application_views.HostRowVoidView.as_view(),
        name="application_row_void",
    ),
    path("<int:pk>/sites/new/", edit_views.SiteEditView.as_view(), name="site_create"),
    path(
        "<int:pk>/sites/<int:site_id>/edit/",
        edit_views.SiteEditView.as_view(),
        name="site_edit",
    ),
    path(
        "<int:pk>/memberships/new/",
        edit_views.MembershipEditView.as_view(),
        name="membership_create",
    ),
    path(
        "<int:pk>/memberships/<int:membership_id>/edit/",
        edit_views.MembershipEditView.as_view(),
        name="membership_edit",
    ),
    path(
        "<int:pk>/memberships/<int:membership_id>/certificate/",
        edit_views.CertificateView.as_view(),
        name="membership_certificate",
    ),
    path(
        "<int:pk>/cooperation/new/",
        edit_views.CooperationEditView.as_view(),
        name="cooperation_create",
    ),
    path(
        "<int:pk>/cooperation/<int:confirmation_id>/edit/",
        edit_views.CooperationEditView.as_view(),
        name="cooperation_edit",
    ),
    path(
        "<int:pk>/support-contracts/new/",
        edit_views.SupportContractEditView.as_view(),
        name="support_contract_create",
    ),
    path(
        "<int:pk>/support-contracts/<int:contract_id>/edit/",
        edit_views.SupportContractEditView.as_view(),
        name="support_contract_edit",
    ),
    path(
        "<int:pk>/support-contracts/<int:contract_id>/fees/",
        edit_views.SupportFeesView.as_view(),
        name="support_contract_fees",
    ),
    path(
        "<int:pk>/support-contracts/<int:contract_id>/terminate/",
        edit_views.SupportTerminateView.as_view(),
        name="support_contract_terminate",
    ),
    path(
        "<int:pk>/support-contracts/<int:contract_id>/reopen/",
        edit_views.SupportReopenView.as_view(),
        name="support_contract_reopen",
    ),
    path(
        "<int:pk>/<str:section>/<int:object_id>/attachments/new/",
        edit_views.AttachmentAddView.as_view(),
        name="attachment_add",
    ),
    path(
        "<int:pk>/attachments/<int:attachment_id>/void/",
        edit_views.AttachmentVoidView.as_view(),
        name="attachment_void",
    ),
]
