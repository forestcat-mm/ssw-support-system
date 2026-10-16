from django.urls import path

from apps.workers import views

app_name = "workers"

urlpatterns = [
    path("", views.WorkerListView.as_view(), name="list"),
    path("new/", views.WorkerCreateView.as_view(), name="create"),
    path("<int:pk>/", views.WorkerDetailView.as_view(), name="detail"),
    path("<int:pk>/contracts/", views.WorkerContractsView.as_view(), name="contracts"),
    path("<int:pk>/filings/", views.WorkerFilingsView.as_view(), name="filings"),
    path("<int:pk>/history/", views.WorkerHistoryView.as_view(), name="history"),
    path("<int:pk>/edit/", views.WorkerUpdateView.as_view(), name="edit"),
    path("<int:pk>/deactivate/", views.WorkerDeactivateView.as_view(), name="deactivate"),
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
