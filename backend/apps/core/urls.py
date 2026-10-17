from django.urls import path

from apps.core import views

app_name = "accounts"

urlpatterns = [
    path("login/", views.LoginView.as_view(), name="login"),
    path("logout/", views.LogoutView.as_view(), name="logout"),
    path("keepalive/", views.KeepAliveView.as_view(), name="keepalive"),
    path("mfa/setup/", views.MFASetupView.as_view(), name="mfa_setup"),
    path("mfa/verify/", views.MFAVerifyView.as_view(), name="mfa_verify"),
    path("mfa/recovery-codes/", views.RecoveryCodesView.as_view(), name="recovery_codes"),
    path(
        "invitations/<str:token>/",
        views.InvitationAcceptView.as_view(),
        name="invitation_accept",
    ),
]
