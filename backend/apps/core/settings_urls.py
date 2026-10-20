from django.urls import path

from apps.core import settings_views as views
from apps.forms_engine import master_views

app_name = "settings"

urlpatterns = [
    path("", views.SettingsIndexView.as_view(), name="index"),
    path("users/", views.UserListView.as_view(), name="users"),
    path("users/invite/", views.UserInviteView.as_view(), name="user_invite"),
    path("users/<int:pk>/", views.UserDetailView.as_view(), name="user"),
    path("users/<int:pk>/<str:action>/", views.UserActionView.as_view(), name="user_action"),
    path("staff/", views.StaffListView.as_view(), name="staff"),
    path("staff/new/", views.StaffFormView.as_view(), name="staff_create"),
    path("staff/<int:pk>/", views.StaffFormView.as_view(), name="staff_edit"),
    path("organization/", views.OrganizationView.as_view(), name="organization"),
    path("organization/edit/", views.OrganizationEditView.as_view(), name="organization_edit"),
    path(
        "organization/certificate/",
        views.CertificateView.as_view(),
        name="organization_certificate",
    ),
    path("organization/officers/new/", views.OfficerEditView.as_view(), name="officer_create"),
    path("organization/officers/<int:pk>/", views.OfficerEditView.as_view(), name="officer_edit"),
    path(
        "organization/officers/<int:pk>/void/",
        views.OfficerVoidView.as_view(),
        name="officer_void",
    ),
    path("forms/", master_views.FormMasterView.as_view(), name="forms"),
    path("forms/<int:pk>/", master_views.FormVersionView.as_view(), name="form_version"),
    path("offices/", views.OfficeListView.as_view(), name="offices"),
    path("offices/new/", views.OfficeFormView.as_view(), name="office_create"),
    path("offices/<int:pk>/", views.OfficeFormView.as_view(), name="office_edit"),
    path(
        "offices/<int:pk>/manager/",
        views.OfficeManagerChangeView.as_view(),
        name="office_manager_change",
    ),
    path(
        "offices/<int:pk>/manager/<int:period_id>/void/",
        views.OfficeManagerVoidView.as_view(),
        name="office_manager_void",
    ),
    path("support-plan/", views.StandardPlanView.as_view(), name="support_plan"),
    path("support-plan/edit/", views.StandardPlanEditView.as_view(), name="support_plan_edit"),
    path("audit/", views.AuditListView.as_view(), name="audit"),
    path("audit/<int:pk>/", views.AuditDetailView.as_view(), name="audit_entry"),
]
