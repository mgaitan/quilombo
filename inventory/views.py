from django.contrib.auth import login
from django.contrib.auth.forms import UserCreationForm
from django.db import transaction
from django.http import HttpResponseRedirect
from django.shortcuts import get_object_or_404
from django.urls import reverse
from django.utils import timezone
from django.views.generic import FormView
from drf_spectacular.utils import extend_schema
from rest_framework import filters, status, viewsets
from rest_framework.generics import GenericAPIView
from rest_framework.response import Response

from .models import ApiToken, Holding, Item, Location, LocationRelation, Membership, Workspace
from .serializers import (
    ApiTokenCreateSerializer,
    ApiTokenIssuedSerializer,
    ApiTokenSerializer,
    BulkUpsertResultSerializer,
    BulkUpsertSerializer,
    HoldingSerializer,
    ItemSerializer,
    LocationRelationSerializer,
    LocationSerializer,
    SearchQuerySerializer,
    SearchResultSerializer,
    WorkspaceSerializer,
)
from .services import (
    BulkUpsertError,
    IdempotencyConflict,
    bulk_upsert_inventory,
    hash_request,
    search_holdings,
)


class WorkspaceAccessMixin:
    workspace = None

    def get_workspace(self):
        if self.workspace is None:
            queryset = Workspace.objects.filter(memberships__user=self.request.user)
            if getattr(self.request.auth, "workspace_id", None):
                queryset = queryset.filter(pk=self.request.auth.workspace_id)
            self.workspace = get_object_or_404(
                queryset,
                slug=self.kwargs["workspace_slug"],
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


class WorkspaceViewSet(viewsets.ModelViewSet):
    queryset = Workspace.objects.all()
    serializer_class = WorkspaceSerializer
    http_method_names = ["get", "post", "head", "options"]

    def get_queryset(self):
        queryset = Workspace.objects.filter(memberships__user=self.request.user).distinct()
        if getattr(self.request.auth, "workspace_id", None):
            queryset = queryset.filter(pk=self.request.auth.workspace_id)
        return queryset

    def create(self, request, *args, **kwargs):
        if getattr(request.auth, "workspace_id", None):
            return Response(
                {"detail": "Use a browser session to create workspaces."},
                status=status.HTTP_403_FORBIDDEN,
            )
        return super().create(request, *args, **kwargs)

    @transaction.atomic
    def perform_create(self, serializer):
        workspace = serializer.save()
        Membership.objects.create(
            workspace=workspace,
            user=self.request.user,
            role=Membership.Role.OWNER,
        )


class ApiTokenView(WorkspaceAccessMixin, GenericAPIView):
    serializer_class = ApiTokenCreateSerializer

    @extend_schema(responses={status.HTTP_200_OK: ApiTokenSerializer(many=True)})
    def get(self, request, *args, **kwargs):
        tokens = self.get_workspace().api_tokens.filter(user=request.user)
        return Response(ApiTokenSerializer(tokens, many=True).data)

    @extend_schema(responses={status.HTTP_201_CREATED: ApiTokenIssuedSerializer})
    def post(self, request, *args, **kwargs):
        if getattr(request.auth, "workspace_id", None):
            return Response(
                {"detail": "Use a browser session to issue API tokens."},
                status=status.HTTP_403_FORBIDDEN,
            )
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        token, raw_token = ApiToken.issue(
            workspace=self.get_workspace(),
            user=request.user,
            name=serializer.validated_data["name"],
        )
        output = ApiTokenIssuedSerializer(
            {
                "id": token.id,
                "name": token.name,
                "prefix": token.prefix,
                "created_at": token.created_at,
                "revoked_at": token.revoked_at,
                "token": raw_token,
            }
        )
        return Response(output.data, status=status.HTTP_201_CREATED)


class ApiTokenRevokeView(WorkspaceAccessMixin, GenericAPIView):
    serializer_class = ApiTokenSerializer

    @extend_schema(responses={status.HTTP_204_NO_CONTENT: None})
    def delete(self, request, token_id, *args, **kwargs):
        if getattr(request.auth, "workspace_id", None):
            return Response(status=status.HTTP_403_FORBIDDEN)
        token = get_object_or_404(
            self.get_workspace().api_tokens,
            pk=token_id,
            user=request.user,
            revoked_at__isnull=True,
        )
        token.revoked_at = timezone.now()
        token.save(update_fields=["revoked_at"])
        return Response(status=status.HTTP_204_NO_CONTENT)


class InventorySearchView(WorkspaceAccessMixin, GenericAPIView):
    serializer_class = SearchQuerySerializer

    @extend_schema(parameters=[SearchQuerySerializer], responses=SearchResultSerializer)
    def get(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.query_params)
        serializer.is_valid(raise_exception=True)
        query = serializer.validated_data["q"].strip()
        results = search_holdings(
            workspace=self.get_workspace(),
            query=query,
            category=serializer.validated_data.get("category", ""),
            location=serializer.validated_data.get("location", ""),
        )
        output = SearchResultSerializer({"query": query, "count": len(results), "results": results})
        return Response(output.data)


class SignupView(FormView):
    template_name = "registration/signup.html"
    form_class = UserCreationForm

    def form_valid(self, form):
        user = form.save()
        login(self.request, user)
        return HttpResponseRedirect(reverse("workspace-list"))
