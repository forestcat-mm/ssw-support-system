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
    path("password-reset/", views.PasswordResetRequestView.as_view(), name="password_reset"),
    path(
        "password-reset/sent/",
        views.PasswordResetDoneView.as_view(),
        name="password_reset_done",
    ),
    path(
        "password-reset/set/",
        views.PasswordResetSetView.as_view(),
        name="password_reset_set",
    ),
    path(
        "password-reset/complete/",
        views.PasswordResetCompleteView.as_view(),
        name="password_reset_complete",
    ),
    path(
        "password-reset/<str:token>/",
        views.PasswordResetOpenView.as_view(),
        name="password_reset_open",
    ),
    path(
        "invitations/<str:token>/",
        views.InvitationAcceptView.as_view(),
        name="invitation_accept",
    ),
]
