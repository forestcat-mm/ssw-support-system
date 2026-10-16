from django.urls import path

from apps.hosts import views

app_name = "hosts"

urlpatterns = [
    path("", views.HostListView.as_view(), name="list"),
    path("<int:pk>/", views.HostDetailView.as_view(), name="detail"),
]
