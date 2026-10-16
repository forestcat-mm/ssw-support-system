from django.urls import path

from apps.workers import views

app_name = "workers"

urlpatterns = [
    path("", views.WorkerListView.as_view(), name="list"),
    path("<int:pk>/", views.WorkerDetailView.as_view(), name="detail"),
    path("<int:pk>/contracts/", views.WorkerContractsView.as_view(), name="contracts"),
    path("<int:pk>/filings/", views.WorkerFilingsView.as_view(), name="filings"),
    path("<int:pk>/history/", views.WorkerHistoryView.as_view(), name="history"),
]
