from django.urls import include, path
from rest_framework.routers import DefaultRouter

from .views import (
    ApiTokenRevokeView,
    ApiTokenView,
    BookLookupView,
    BookLookupView,
    BulkUpsertView,
    HoldingViewSet,
    InventorySearchView,
    ItemViewSet,
    LocationRelationViewSet,
    LocationViewSet,
    StockStatusView,
    WorkspaceViewSet,
)

router = DefaultRouter()
router.register("locations", LocationViewSet)
router.register("location-relations", LocationRelationViewSet)
router.register("items", ItemViewSet)
router.register("holdings", HoldingViewSet)

workspace_router = DefaultRouter()
workspace_router.register("workspaces", WorkspaceViewSet, basename="workspace")

urlpatterns = [
    path("", include(workspace_router.urls)),
    path(
        "workspaces/<slug:workspace_slug>/stock-status/",
        StockStatusView.as_view(),
        name="stock-status",
    ),
    path(
        "workspaces/<slug:workspace_slug>/catalog/books/<str:isbn>/",
        BookLookupView.as_view(),
        name="book-lookup",
    ),
    path(
        "workspaces/<slug:workspace_slug>/search/",
        InventorySearchView.as_view(),
        name="inventory-search",
    ),
    path(
        "workspaces/<slug:workspace_slug>/tokens/",
        ApiTokenView.as_view(),
        name="api-token-list",
    ),
    path(
        "workspaces/<slug:workspace_slug>/tokens/<uuid:token_id>/",
        ApiTokenRevokeView.as_view(),
        name="api-token-revoke",
    ),
    path(
        "workspaces/<slug:workspace_slug>/bulk-upsert/",
        BulkUpsertView.as_view(),
        name="bulk-upsert",
    ),
    path("workspaces/<slug:workspace_slug>/", include(router.urls)),
]
