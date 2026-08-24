from django.conf import settings
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
    event_history,
    event_undo,
    first_inventory,
    holding_create,
    holding_delete,
    holding_edit,
    home,
    item_create,
    item_delete,
    item_detail,
    item_edit,
    item_list,
    location_create,
    location_edit,
    location_list,
    oauth_consent,
    workspace_create,
    workspace_inventory,
    workspace_member,
    workspace_settings,
    workspace_share,
)


def health_check(request):
    with connection.cursor() as cursor:
        cursor.execute("SELECT 1")
        cursor.fetchone()
    return JsonResponse({"status": "ok", "version": settings.APP_VERSION})


urlpatterns = [
    path("i18n/", include("django.conf.urls.i18n")),
    path("", home, name="home"),
    path("app/", dashboard, name="dashboard"),
    path("app/new/", workspace_create, name="workspace-create"),
    path("app/<slug:workspace_slug>/settings/", workspace_settings, name="workspace-settings"),
    path("app/<slug:workspace_slug>/members/", workspace_share, name="workspace-share"),
    path(
        "app/<slug:workspace_slug>/members/<int:user_id>/",
        workspace_member,
        name="workspace-member",
    ),
    path(
        "app/<slug:workspace_slug>/first-inventory/",
        first_inventory,
        name="first-inventory",
    ),
    path("app/<slug:workspace_slug>/items/", item_list, name="web-item-list"),
    path("app/<slug:workspace_slug>/history/", event_history, name="event-history"),
    path(
        "app/<slug:workspace_slug>/history/<uuid:event_id>/undo/",
        event_undo,
        name="event-undo",
    ),
    path("app/<slug:workspace_slug>/items/new/", item_create, name="web-item-create"),
    path(
        "app/<slug:workspace_slug>/items/<uuid:item_id>/",
        item_detail,
        name="web-item-detail",
    ),
    path(
        "app/<slug:workspace_slug>/items/<uuid:item_id>/edit/",
        item_edit,
        name="web-item-edit",
    ),
    path(
        "app/<slug:workspace_slug>/items/<uuid:item_id>/delete/",
        item_delete,
        name="web-item-delete",
    ),
    path(
        "app/<slug:workspace_slug>/items/<uuid:item_id>/holdings/new/",
        holding_create,
        name="web-holding-create",
    ),
    path(
        "app/<slug:workspace_slug>/items/<uuid:item_id>/holdings/<uuid:holding_id>/edit/",
        holding_edit,
        name="web-holding-edit",
    ),
    path(
        "app/<slug:workspace_slug>/items/<uuid:item_id>/holdings/<uuid:holding_id>/delete/",
        holding_delete,
        name="web-holding-delete",
    ),
    path("app/<slug:workspace_slug>/locations/", location_list, name="web-location-list"),
    path(
        "app/<slug:workspace_slug>/locations/new/",
        location_create,
        name="web-location-create",
    ),
    path(
        "app/<slug:workspace_slug>/locations/<uuid:location_id>/edit/",
        location_edit,
        name="web-location-edit",
    ),
    path("app/<slug:workspace_slug>/", workspace_inventory, name="workspace-inventory"),
    path("connect/", connector_guide, name="connector-guide"),
    path("skills/manage-quilombo-inventory.zip", download_skill, name="skill-download"),
    path("health/", health_check, name="health-check"),
    path("admin/", admin.site.urls),
    path("accounts/login/", auth_views.LoginView.as_view(), name="login"),
    path("accounts/logout/", auth_views.LogoutView.as_view(), name="logout"),
    path("accounts/signup/", SignupView.as_view(), name="signup"),
    path("accounts/", include("allauth.urls")),
    path("oauth/consent/", oauth_consent, name="oauth-consent"),
    path("api/schema/", SpectacularAPIView.as_view(), name="schema"),
    path(
        "api/docs/",
        SpectacularSwaggerView.as_view(url_name="schema"),
        name="api-docs",
    ),
    path("api/", include("inventory.urls")),
]
