from django.urls import include, path
from rest_framework.routers import DefaultRouter

from .views import (
    ApiTokenRevokeView,
    ApiTokenView,
    BookLookupView,
    BulkUpsertView,
    HoldingViewSet,
    InventoryExportView,
    InventoryImportView,
    InventorySearchView,
    ItemLabelAssertionView,
    ItemViewSet,
    LabelSuggestionView,
    LocationRelationViewSet,
    LocationViewSet,
    PublicInventorySearchView,
    PublicSearchLinkDetailView,
    PublicSearchLinkRotateView,
    PublicSearchLinkView,
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
        "workspaces/<slug:workspace_slug>/export/",
        InventoryExportView.as_view(),
        name="inventory-export",
    ),
    path(
        "workspaces/<slug:workspace_slug>/import/",
        InventoryImportView.as_view(),
        name="inventory-import",
    ),
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
    path(
        "public/search/<str:secret>/",
        PublicInventorySearchView.as_view(),
        name="public-inventory-search",
    ),
    path(
        "workspaces/<slug:workspace_slug>/public-search-links/",
        PublicSearchLinkView.as_view(),
        name="public-search-link-list",
    ),
    path(
        "workspaces/<slug:workspace_slug>/public-search-links/<uuid:link_id>/",
        PublicSearchLinkDetailView.as_view(),
        name="public-search-link-detail",
    ),
    path(
        "workspaces/<slug:workspace_slug>/public-search-links/<uuid:link_id>/rotate/",
        PublicSearchLinkRotateView.as_view(),
        name="public-search-link-rotate",
    ),
    path(
        "workspaces/<slug:workspace_slug>/labels/",
        LabelSuggestionView.as_view(),
        name="label-suggestions",
    ),
    path(
        "workspaces/<slug:workspace_slug>/label-assertions/",
        ItemLabelAssertionView.as_view(),
        name="label-assertions",
    ),
    path("workspaces/<slug:workspace_slug>/", include(router.urls)),
]
