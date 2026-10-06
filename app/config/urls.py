from api.v1.router import api as api_v1
from common.views import health, health_live, health_ready, metrics_view
from django.contrib import admin
from django.urls import include, path
from django.views.generic import RedirectView

urlpatterns = [
    path("", RedirectView.as_view(pattern_name="dashboard:home", permanent=False)),
    path("dashboard/", include("dashboard.urls")),
    path("admin/", admin.site.urls),
    path("health", health),
    path("health/live", health_live),
    path("health/ready", health_ready),
    path("metrics", metrics_view),
    path("api/v1/", api_v1.urls),
]
