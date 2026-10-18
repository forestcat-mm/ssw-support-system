from django.urls import path

from apps.forms_engine import views

app_name = "forms"

urlpatterns = [
    path("documents/<int:pk>/", views.GeneratedDocumentView.as_view(), name="document"),
    path(
        "documents/<int:pk>/pdf/",
        views.GeneratedPdfDownloadView.as_view(),
        name="generated_pdf",
    ),
]
