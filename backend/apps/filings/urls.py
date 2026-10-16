from django.urls import path

from apps.filings import views

app_name = "filings"

urlpatterns = [
    path("new/", views.FilingCreateView.as_view(), name="create"),
    path("<int:pk>/", views.FilingDetailView.as_view(), name="detail"),
]
