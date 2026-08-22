import json
from io import BytesIO
from zipfile import ZIP_DEFLATED, ZipFile

from django.conf import settings
from django.contrib.auth import login
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import UserCreationForm
from django.db import transaction
from django.db.models import Count
from django.http import FileResponse, Http404, HttpResponse, HttpResponseRedirect
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_http_methods
from django.views.generic import FormView
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import filters, status, viewsets
from rest_framework.generics import GenericAPIView
from rest_framework.response import Response

from .models import (
    ApiToken,
    Holding,
    Item,
    Location,
    LocationRelation,
    Membership,
    OAuthAuthorizationRequest,
    Workspace,
)
from .oauth import create_authorization_grant
from .serializers import (
    ApiTokenCreateSerializer,
    ApiTokenIssuedSerializer,
    ApiTokenSerializer,
    BulkUpsertResultSerializer,
    BulkUpsertSerializer,
    HoldingSerializer,
    InventoryImportResultSerializer,
    InventoryImportSerializer,
    ItemSerializer,
    LocationRelationSerializer,
    LocationSerializer,
    SearchQuerySerializer,
    SearchResultSerializer,
    StockStatusResultSerializer,
    WorkspaceSerializer,
)
from .services import (
    BulkUpsertError,
    IdempotencyConflict,
    build_holding_clue_context,
    bulk_upsert_inventory,
    get_stock_status,
    hash_request,
    search_holdings,
)
from .transfers import (
    InventoryTransferError,
    export_inventory_csv,
    export_inventory_document,
    import_inventory_document,
    parse_inventory_document,
)


def home(request):
    return render(request, "inventory/home.html")


@login_required
def dashboard(request):
    workspaces = (
        Workspace.objects.filter(memberships__user=request.user)
        .annotate(
            location_count=Count("locations", distinct=True),
            item_count=Count("items", distinct=True),
            holding_count=Count("holdings", distinct=True),
        )
        .distinct()
    )
    return render(request, "inventory/dashboard.html", {"workspaces": workspaces})


@login_required
def first_inventory(request, workspace_slug):
    workspace = get_object_or_404(
        Workspace.objects.filter(memberships__user=request.user),
        slug=workspace_slug,
    )
    return render(request, "inventory/first_inventory.html", {"workspace": workspace})


@login_required
def workspace_inventory(request, workspace_slug):
    workspace = get_object_or_404(
        Workspace.objects.filter(memberships__user=request.user),
        slug=workspace_slug,
    )
    query = request.GET.get("q", "").strip()
    location_key = request.GET.get("location", "").strip()
    holdings = search_holdings(
        workspace=workspace,
        query=query,
        location=location_key,
        limit=200,
    )
    stock_status = get_stock_status(workspace=workspace)
    return render(
        request,
        "inventory/workspace.html",
        {
            "workspace": workspace,
            "holdings": holdings,
            "locations": workspace.locations.only("key", "name"),
            "query": query,
            "location_key": location_key,
            "stock_status": stock_status,
        },
    )


def connector_guide(request):
    return render(
        request,
        "inventory/connect.html",
        {"mcp_url": f"{settings.PUBLIC_BASE_URL}/mcp"},
    )


def download_skill(request):
    skill_root = settings.BASE_DIR / "skills" / "manage-quilombo-inventory"
    archive = BytesIO()
    with ZipFile(archive, "w", ZIP_DEFLATED) as zip_file:
        for path in sorted(skill_root.rglob("*")):
            if path.is_file():
                zip_file.write(path, f"manage-quilombo-inventory/{path.relative_to(skill_root)}")
    archive.seek(0)
    return FileResponse(
        archive,
        as_attachment=True,
        filename="manage-quilombo-inventory.zip",
        content_type="application/zip",
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
        with transaction.atomic():
            workspace = self.get_workspace()
            Workspace.objects.select_for_update().get(pk=workspace.pk)
            serializer.save(workspace=workspace)

    def perform_update(self, serializer):
        with transaction.atomic():
            Workspace.objects.select_for_update().get(pk=self.get_workspace().pk)
            serializer.save()

    def perform_destroy(self, instance):
        with transaction.atomic():
            Workspace.objects.select_for_update().get(pk=self.get_workspace().pk)
            instance.delete()


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


class InventoryExportView(WorkspaceAccessMixin, GenericAPIView):
    @extend_schema(
        parameters=[
            OpenApiParameter(
                name="format",
                type=str,
                enum=["json", "csv"],
                default="json",
                description="Portable inventory document format.",
            )
        ],
        responses={(200, "application/json"): bytes, (200, "text/csv"): bytes},
    )
    def get(self, request, *args, **kwargs):
        format_name = request.query_params.get("format", "json").casefold()
        if format_name not in {"json", "csv"}:
            return Response(
                {"detail": "format must be json or csv."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        workspace = self.get_workspace()
        document = export_inventory_document(workspace)
        if format_name == "csv":
            response = HttpResponse(
                export_inventory_csv(document),
                content_type="text/csv; charset=utf-8",
            )
        else:
            response = HttpResponse(
                json.dumps(document, ensure_ascii=False, indent=2),
                content_type="application/json",
            )
        response["Content-Disposition"] = (
            f'attachment; filename="{workspace.slug}-inventory.{format_name}"'
        )
        return response


class InventoryImportView(WorkspaceAccessMixin, GenericAPIView):
    serializer_class = InventoryImportSerializer

    @extend_schema(responses={status.HTTP_200_OK: InventoryImportResultSerializer})
    def post(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        content = data.get("content")
        if uploaded_file := data.get("file"):
            try:
                content = uploaded_file.read().decode("utf-8-sig")
            except UnicodeDecodeError:
                return Response(
                    {"detail": "Import files must be UTF-8 encoded."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
        try:
            document = parse_inventory_document(
                format_name=data["format"],
                document=data.get("document"),
                content=content,
            )
            summary, event, replayed = import_inventory_document(
                workspace=self.get_workspace(),
                actor=request.user,
                document=document,
                dry_run=data["dry_run"],
                idempotency_key=data["idempotency_key"],
                provenance=data.get("provenance", {}),
                request_hash=hash_request(
                    {
                        "document": document,
                        "idempotency_key": data["idempotency_key"],
                        "provenance": data.get("provenance", {}),
                    }
                ),
            )
        except InventoryTransferError as error:
            return Response({"detail": str(error)}, status=status.HTTP_400_BAD_REQUEST)

        output = InventoryImportResultSerializer(
            {
                "event_id": event.id if event else None,
                "replayed": replayed,
                "dry_run": data["dry_run"],
                "summary": summary,
            }
        )
        return Response(output.data)


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
            include_descendants=serializer.validated_data["include_descendants"],
        )
        clue_context = build_holding_clue_context(workspace=self.get_workspace(), holdings=results)
        output = SearchResultSerializer(
            {"query": query, "count": len(results), "results": results},
            context=clue_context,
        )
        return Response(output.data)


class StockStatusView(WorkspaceAccessMixin, GenericAPIView):
    serializer_class = StockStatusResultSerializer

    @extend_schema(responses=StockStatusResultSerializer)
    def get(self, request, *args, **kwargs):
        result = get_stock_status(workspace=self.get_workspace())
        return Response(StockStatusResultSerializer(result).data)


class SignupView(FormView):
    template_name = "registration/signup.html"
    form_class = UserCreationForm

    @transaction.atomic
    def form_valid(self, form):
        user = form.save()
        workspace = Workspace.objects.create(name="Home", slug=f"home-{str(user.id)[:8]}")
        Membership.objects.create(
            workspace=workspace,
            user=user,
            role=Membership.Role.OWNER,
        )
        login(self.request, user)
        return HttpResponseRedirect(reverse("dashboard"))


@login_required
@require_http_methods(["GET", "POST"])
def oauth_consent(request):
    request_id = request.GET.get("request") or request.POST.get("request")
    authorization_request = get_object_or_404(
        OAuthAuthorizationRequest.objects.select_related("client"),
        id=request_id,
    )
    if authorization_request.expires_at <= timezone.now():
        authorization_request.delete()
        raise Http404("Authorization request expired.")

    redirect_uri = authorization_request.redirect_uri
    redirect_params = {"state": authorization_request.state or None}
    if request.method == "POST":
        if request.POST.get("action") == "deny":
            authorization_request.delete()
            redirect_params.update(error="access_denied")
        else:
            workspace = get_object_or_404(
                Workspace.objects.filter(memberships__user=request.user),
                id=request.POST.get("workspace"),
            )
            with transaction.atomic():
                raw_code = create_authorization_grant(
                    authorization_request=authorization_request,
                    user=request.user,
                    workspace=workspace,
                )
                authorization_request.delete()
            redirect_params.update(
                code=raw_code,
                iss=settings.PUBLIC_BASE_URL,
            )
        from mcp.server.auth.provider import construct_redirect_uri

        return HttpResponseRedirect(construct_redirect_uri(redirect_uri, **redirect_params))

    workspaces = Workspace.objects.filter(memberships__user=request.user).distinct()
    if not workspaces.exists():
        return render(
            request,
            "inventory/oauth_consent_error.html",
            status=400,
        )
    return render(
        request,
        "inventory/oauth_consent.html",
        {
            "authorization_request": authorization_request,
            "client_name": authorization_request.client.metadata.get("client_name")
            or "Una aplicación",
            "workspaces": workspaces,
        },
    )
