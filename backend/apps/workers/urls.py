from django.urls import path

from apps.workers.views import WorkerListView

app_name = "workers"

urlpatterns = [
    path("", WorkerListView.as_view(), name="list"),
]
