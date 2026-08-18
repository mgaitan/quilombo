from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.db import connection
from django.http import JsonResponse
from django.urls import include, path
from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView

from inventory.views import (
    SignupView,
    connector_guide,
    dashboard,
    download_skill,
    first_inventory,
    home,
    oauth_consent,
    workspace_inventory,
)


def health_check(request):
    with connection.cursor() as cursor:
        cursor.execute("SELECT 1")
        cursor.fetchone()
    return JsonResponse({"status": "ok"})


urlpatterns = [
    path("", home, name="home"),
    path("app/", dashboard, name="dashboard"),
    path(
        "app/<slug:workspace_slug>/first-inventory/",
        first_inventory,
        name="first-inventory",
    ),
    path("app/<slug:workspace_slug>/", workspace_inventory, name="workspace-inventory"),
    path("connect/", connector_guide, name="connector-guide"),
    path("skills/manage-quilombo-inventory.zip", download_skill, name="skill-download"),
    path("health/", health_check, name="health-check"),
    path("admin/", admin.site.urls),
    path("accounts/login/", auth_views.LoginView.as_view(), name="login"),
    path("accounts/logout/", auth_views.LogoutView.as_view(), name="logout"),
    path("accounts/signup/", SignupView.as_view(), name="signup"),
    path("oauth/consent/", oauth_consent, name="oauth-consent"),
    path("api/schema/", SpectacularAPIView.as_view(), name="schema"),
    path(
        "api/docs/",
        SpectacularSwaggerView.as_view(url_name="schema"),
        name="api-docs",
    ),
    path("api/", include("inventory.urls")),
]
