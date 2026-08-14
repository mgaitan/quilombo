from django.urls import include, path
from rest_framework.routers import DefaultRouter

from .views import (
    BulkUpsertView,
    HoldingViewSet,
    ItemViewSet,
    LocationRelationViewSet,
    LocationViewSet,
)

router = DefaultRouter()
router.register("locations", LocationViewSet)
router.register("location-relations", LocationRelationViewSet)
router.register("items", ItemViewSet)
router.register("holdings", HoldingViewSet)

urlpatterns = [
    path(
        "workspaces/<slug:workspace_slug>/bulk-upsert/",
        BulkUpsertView.as_view(),
        name="bulk-upsert",
    ),
    path("workspaces/<slug:workspace_slug>/", include(router.urls)),
]
