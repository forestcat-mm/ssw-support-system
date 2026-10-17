from django.urls import path

from apps.support import record_views

app_name = "consultations"

urlpatterns = [
    path("", record_views.ConsultationListView.as_view(), name="list"),
]
