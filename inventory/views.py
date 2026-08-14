from django.shortcuts import get_object_or_404
from rest_framework import filters, viewsets

from .models import Holding, Item, Location, LocationRelation, Workspace
from .serializers import (
    HoldingSerializer,
    ItemSerializer,
    LocationRelationSerializer,
    LocationSerializer,
)


class WorkspaceScopedViewSet(viewsets.ModelViewSet):
    workspace = None

    def get_workspace(self):
        if self.workspace is None:
            self.workspace = get_object_or_404(
                Workspace,
                slug=self.kwargs["workspace_slug"],
                memberships__user=self.request.user,
            )
        return self.workspace

    def get_queryset(self):
        return super().get_queryset().filter(workspace=self.get_workspace())

    def perform_create(self, serializer):
        serializer.save(workspace=self.get_workspace())


class LocationViewSet(WorkspaceScopedViewSet):
    queryset = Location.objects.select_related("parent")
    serializer_class = LocationSerializer
    filter_backends = [filters.SearchFilter]
    search_fields = ["key", "name", "description", "kind"]


class LocationRelationViewSet(WorkspaceScopedViewSet):
    queryset = LocationRelation.objects.select_related("subject", "object")
    serializer_class = LocationRelationSerializer


class ItemViewSet(WorkspaceScopedViewSet):
    queryset = Item.objects.all()
    serializer_class = ItemSerializer
    filter_backends = [filters.SearchFilter]
    search_fields = ["key", "name", "description", "category"]


class HoldingViewSet(WorkspaceScopedViewSet):
    queryset = Holding.objects.select_related("item", "location")
    serializer_class = HoldingSerializer
    filter_backends = [filters.SearchFilter]
    search_fields = [
        "item__key",
        "item__name",
        "item__description",
        "item__category",
        "location__key",
        "location__name",
    ]
