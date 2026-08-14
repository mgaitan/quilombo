from django.shortcuts import get_object_or_404
from drf_spectacular.utils import extend_schema
from rest_framework import filters, status, viewsets
from rest_framework.generics import GenericAPIView
from rest_framework.response import Response

from .models import Holding, Item, Location, LocationRelation, Workspace
from .serializers import (
    BulkUpsertResultSerializer,
    BulkUpsertSerializer,
    HoldingSerializer,
    ItemSerializer,
    LocationRelationSerializer,
    LocationSerializer,
)
from .services import BulkUpsertError, IdempotencyConflict, bulk_upsert_inventory, hash_request


class WorkspaceAccessMixin:
    workspace = None

    def get_workspace(self):
        if self.workspace is None:
            self.workspace = get_object_or_404(
                Workspace,
                slug=self.kwargs["workspace_slug"],
                memberships__user=self.request.user,
            )
        return self.workspace


class WorkspaceScopedViewSet(WorkspaceAccessMixin, viewsets.ModelViewSet):
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


class BulkUpsertView(WorkspaceAccessMixin, GenericAPIView):
    serializer_class = BulkUpsertSerializer

    @extend_schema(
        responses={
            status.HTTP_200_OK: BulkUpsertResultSerializer,
            status.HTTP_201_CREATED: BulkUpsertResultSerializer,
        }
    )
    def post(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            event, replayed = bulk_upsert_inventory(
                workspace=self.get_workspace(),
                actor=request.user,
                data=serializer.validated_data,
                request_hash=hash_request(serializer.validated_data),
            )
        except IdempotencyConflict as error:
            return Response({"detail": str(error)}, status=status.HTTP_409_CONFLICT)
        except BulkUpsertError as error:
            return Response({"detail": str(error)}, status=status.HTTP_400_BAD_REQUEST)

        result = {
            "event_id": event.id,
            "replayed": replayed,
            "processed": event.summary,
        }
        output = BulkUpsertResultSerializer(result)
        return Response(
            output.data, status=status.HTTP_200_OK if replayed else status.HTTP_201_CREATED
        )
