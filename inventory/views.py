import html
import json
from copy import deepcopy
from io import BytesIO
from uuid import UUID, uuid4
from zipfile import ZIP_DEFLATED, ZipFile

import segno
from django.conf import settings
from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Count, IntegerField, OuterRef, Subquery, Value
from django.db.models.functions import Coalesce
from django.db.models.query import QuerySet
from django.http import FileResponse, Http404, HttpResponse, HttpResponseRedirect
from django.shortcuts import get_object_or_404, render
from django.templatetags.static import static
from django.urls import reverse
from django.utils import timezone
from django.utils.html import format_html
from django.utils.translation import gettext as _
from django.utils.translation import ngettext
from django.views.decorators.http import require_http_methods
from drf_spectacular.utils import OpenApiParameter, OpenApiResponse, extend_schema
from rest_framework import filters, serializers, status, viewsets
from rest_framework.generics import GenericAPIView
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle

from .catalogs import (
    CatalogLookupError,
    CatalogRecordNotFound,
    lookup_book_by_isbn,
    lookup_book_details,
    normalize_edition_key,
    normalize_isbn,
)
from .forms import (
    HoldingForm,
    InventoryImportUploadForm,
    ItemForm,
    LocationForm,
    MemberAccessForm,
    PublicSearchLinkForm,
    WorkspaceCreateForm,
    WorkspaceRenameForm,
    WorkspaceShareForm,
)
from .models import (
    ApiToken,
    Holding,
    InventoryEvent,
    Item,
    ItemLabel,
    Location,
    LocationRelation,
    Membership,
    OAuthAuthorizationRequest,
    PublicSearchLink,
    VerificationStatus,
    Workspace,
)
from .oauth import create_authorization_grant
from .pagination import InventoryPagination, PublicSearchPagination
from .permissions import (
    membership_can_write,
    require_workspace_write,
    user_can_manage_workspace,
)
from .serializers import (
    ApiTokenCreateSerializer,
    ApiTokenIssuedSerializer,
    ApiTokenSerializer,
    BookLookupResultSerializer,
    BulkUpsertResultSerializer,
    BulkUpsertSerializer,
    HoldingSerializer,
    InventoryImportResultSerializer,
    InventoryImportSerializer,
    ItemLabelAssertionRequestSerializer,
    ItemLabelAssertionResultSerializer,
    ItemSerializer,
    LabelSuggestionQuerySerializer,
    LabelSuggestionSerializer,
    LocationRelationSerializer,
    LocationSerializer,
    PublicSearchLinkCreateSerializer,
    PublicSearchLinkSecretSerializer,
    PublicSearchLinkSerializer,
    PublicSearchQuerySerializer,
    PublicSearchResultSerializer,
    SearchQuerySerializer,
    SearchResultSerializer,
    StockStatusResultSerializer,
    WorkspaceSerializer,
)
from .services import (
    BulkUpsertError,
    IdempotencyConflict,
    InventoryUndoError,
    LabelConflictError,
    add_search_match_details,
    assert_item_labels,
    build_holding_clue_context,
    bulk_upsert_inventory,
    create_holding,
    create_item_with_holding,
    create_location,
    create_workspace,
    get_stock_status,
    hash_request,
    location_scope_ids,
    preview_inventory_undo,
    record_public_search_link_use,
    remove_holding,
    remove_item,
    remove_workspace_member,
    rename_workspace,
    resolve_public_search_link,
    search_holdings,
    share_workspace,
    suggest_labels,
    undo_inventory_event,
    update_holding,
    update_item,
    update_location,
    update_workspace_member,
)
from .transfers import (
    InventoryTransferError,
    export_inventory_csv,
    export_inventory_document,
    import_inventory_document,
    parse_inventory_document,
)


def home(request):
    if request.user.is_authenticated:
        return HttpResponseRedirect(reverse("dashboard"))
    language = getattr(request, "LANGUAGE_CODE", "es")
    workshop_image = "workshop-en-social.jpg" if language == "en" else "workshop-es-social.jpg"
    return render(
        request,
        "inventory/home.html",
        {
            "canonical_url": f"{settings.PUBLIC_BASE_URL}/",
            "social_image_url": (
                f"{settings.PUBLIC_BASE_URL}{static(f'inventory/home/{workshop_image}')}"
            ),
        },
    )


def privacy_policy(request):
    return render(request, "inventory/privacy.html")


def terms_of_service(request):
    return render(request, "inventory/terms.html")


@login_required
def dashboard(request):
    location_counts = (
        Location.objects.filter(workspace_id=OuterRef("pk"))
        .values("workspace_id")
        .annotate(count=Count("id"))
        .values("count")
    )
    item_counts = (
        Item.objects.filter(workspace_id=OuterRef("pk"))
        .values("workspace_id")
        .annotate(count=Count("id"))
        .values("count")
    )
    holding_counts = (
        Holding.objects.filter(workspace_id=OuterRef("pk"))
        .values("workspace_id")
        .annotate(count=Count("id"))
        .values("count")
    )
    workspaces = (
        Workspace.objects.filter(memberships__user=request.user)
        .annotate(
            location_count=Coalesce(
                Subquery(location_counts), Value(0), output_field=IntegerField()
            ),
            item_count=Coalesce(Subquery(item_counts), Value(0), output_field=IntegerField()),
            holding_count=Coalesce(Subquery(holding_counts), Value(0), output_field=IntegerField()),
        )
        .distinct()
        .order_by("name", "id")
    )
    page_obj = Paginator(workspaces, 25).get_page(request.GET.get("page"))
    preserved_query = request.GET.copy()
    preserved_query.pop("page", None)
    return render(
        request,
        "inventory/dashboard.html",
        {
            "workspaces": page_obj,
            "page_obj": page_obj,
            "preserved_query": preserved_query.urlencode(),
        },
    )


def _inventory_count(key, count):
    if key == "locations":
        return ngettext("%(count)s location", "%(count)s locations", count) % {"count": count}
    if key == "items":
        return ngettext("%(count)s item", "%(count)s items", count) % {"count": count}
    if key == "holdings":
        return ngettext("%(count)s holding", "%(count)s holdings", count) % {"count": count}
    if key == "labels":
        return ngettext("%(count)s label", "%(count)s labels", count) % {"count": count}
    if key == "label_aliases":
        return ngettext("%(count)s label alias", "%(count)s label aliases", count) % {
            "count": count
        }
    if key == "item_labels":
        return ngettext("%(count)s label assertion", "%(count)s label assertions", count) % {
            "count": count
        }
    return ngettext("%(count)s spatial relation", "%(count)s spatial relations", count) % {
        "count": count
    }


def _inventory_count_lines(summary):
    lines = []
    for key in (
        "locations",
        "items",
        "labels",
        "label_aliases",
        "item_labels",
        "holdings",
        "location_relations",
    ):
        value = summary.get(key, 0)
        if isinstance(value, dict):
            created = value.get("created", 0)
            updated = value.get("updated", 0)
            count = created + updated
            if count:
                lines.append(
                    _("%(records)s (%(created)s created, %(updated)s updated)")
                    % {
                        "records": _inventory_count(key, count),
                        "created": created,
                        "updated": updated,
                    }
                )
        elif value:
            lines.append(_inventory_count(key, value))
    return lines


def _event_change_lines(event):
    summary = event.summary
    if event.kind == InventoryEvent.Kind.BULK_UPSERT:
        return _inventory_count_lines(summary)
    if event.kind == InventoryEvent.Kind.MOVE:
        amount = _("%(quantity)s %(unit)s of %(item)s") % {
            "quantity": summary.get("quantity", "?"),
            "unit": summary.get("unit", "unit"),
            "item": summary.get("item_key", _("unknown item")),
        }
        route = _("From %(source)s to %(destination)s") % {
            "source": summary.get("from_location_key", _("unknown location")),
            "destination": summary.get("to_location_key", _("unknown location")),
        }
        return [amount, route]
    if event.kind == InventoryEvent.Kind.AUDIT:
        holdings = summary.get("holdings", [])
        corrections = sum(bool(row.get("corrected_fields")) for row in holdings)
        status = summary.get("location_status", VerificationStatus.UNKNOWN)
        status_label = dict(VerificationStatus.choices).get(status, status)
        lines = [
            _("Location %(location)s: %(status)s")
            % {
                "location": summary.get("location_key", _("unknown location")),
                "status": status_label,
            },
            ngettext("%(count)s holding checked", "%(count)s holdings checked", len(holdings))
            % {"count": len(holdings)},
        ]
        if corrections:
            lines.append(
                ngettext(
                    "%(count)s holding corrected",
                    "%(count)s holdings corrected",
                    corrections,
                )
                % {"count": corrections}
            )
        return lines
    if event.kind == InventoryEvent.Kind.ITEM_UPDATE:
        lines = [_("Item: %(item)s") % {"item": summary.get("item_key", "?")}]
        fields = summary.get("item_fields", [])
        if fields:
            lines.append(_("Fields: %(fields)s") % {"fields": ", ".join(fields)})
        holding_count = len(summary.get("holdings", []))
        if holding_count:
            lines.append(
                ngettext("%(count)s holding updated", "%(count)s holdings updated", holding_count)
                % {"count": holding_count}
            )
        return lines
    if event.kind == InventoryEvent.Kind.ITEM_DELETE:
        item = summary.get("item_name") or summary.get("item_key", "?")
        lines = [_("Deleted item: %(item)s") % {"item": item}]
        holding_count = summary.get("deleted_holdings", 0)
        if holding_count:
            lines.append(
                ngettext("%(count)s holding removed", "%(count)s holdings removed", holding_count)
                % {"count": holding_count}
            )
        return lines
    if event.kind == InventoryEvent.Kind.UNDO:
        original_kind = summary.get("original_kind", "")
        original_kind_label = dict(InventoryEvent.Kind.choices).get(
            original_kind, original_kind.replace("_", " ")
        )
        lines = [_("Undid: %(event)s") % {"event": original_kind_label or _("inventory event")}]
        lines.extend(_inventory_count_lines(summary.get("restored", {})))
        return lines
    return [
        _("%(field)s: %(value)s") % {"field": key.replace("_", " ").capitalize(), "value": value}
        for key, value in summary.items()
        if not isinstance(value, (dict, list))
    ]


def _managed_workspace(user, workspace_slug):
    return get_object_or_404(
        Workspace.objects.filter(
            slug=workspace_slug,
            memberships__user=user,
            memberships__role__in=[Membership.Role.OWNER, Membership.Role.ADMIN],
        )
    )


def _workspace_membership(user, workspace_slug):
    return get_object_or_404(
        Membership.objects.select_related("workspace"),
        user=user,
        workspace__slug=workspace_slug,
    )


def _writable_workspace(user, workspace_slug):
    membership = _workspace_membership(user, workspace_slug)
    if not membership_can_write(membership):
        raise PermissionDenied(_("This inventory is shared as read-only."))
    return membership.workspace


@login_required
@require_http_methods(["GET", "POST"])
def workspace_create(request):
    form = WorkspaceCreateForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        workspace = create_workspace(user=request.user, name=form.cleaned_data["name"])
        return HttpResponseRedirect(reverse("workspace-inventory", args=[workspace.slug]))
    return render(request, "inventory/workspace_form.html", {"form": form})


def _workspace_settings_context(workspace, *, rename_form=None, share_form=None):
    memberships = workspace.memberships.select_related("user").order_by("role", "user__username")
    for membership in memberships:
        membership.effective_can_write = membership_can_write(membership)
    return {
        "workspace": workspace,
        "memberships": memberships,
        "rename_form": rename_form or WorkspaceRenameForm(initial={"name": workspace.name}),
        "share_form": share_form or WorkspaceShareForm(),
    }


@login_required
@require_http_methods(["GET", "POST"])
def workspace_settings(request, workspace_slug):
    workspace = _managed_workspace(request.user, workspace_slug)
    rename_form = WorkspaceRenameForm(request.POST or None)
    if request.method == "POST" and rename_form.is_valid():
        workspace = rename_workspace(workspace=workspace, name=rename_form.cleaned_data["name"])
        return HttpResponseRedirect(reverse("workspace-settings", args=[workspace.slug]))
    return render(
        request,
        "inventory/workspace_settings.html",
        _workspace_settings_context(workspace, rename_form=rename_form),
    )


@login_required
@require_http_methods(["POST"])
def workspace_share(request, workspace_slug):
    workspace = _managed_workspace(request.user, workspace_slug)
    form = WorkspaceShareForm(request.POST)
    if form.is_valid():
        user = (
            get_user_model().objects.filter(username__iexact=form.cleaned_data["username"]).first()
        )
        if user:
            share_workspace(
                workspace=workspace,
                user=user,
                can_write=form.cleaned_data["can_write"],
            )
            return HttpResponseRedirect(reverse("workspace-settings", args=[workspace.slug]))
        form.add_error("username", _("No user has that username."))
    return render(
        request,
        "inventory/workspace_settings.html",
        _workspace_settings_context(workspace, share_form=form),
        status=400,
    )


@login_required
@require_http_methods(["POST"])
def workspace_member(request, workspace_slug, user_id):
    workspace = _managed_workspace(request.user, workspace_slug)
    get_object_or_404(Membership, workspace=workspace, user_id=user_id)
    if request.POST.get("action") == "remove":
        remove_workspace_member(workspace=workspace, user_id=user_id)
    else:
        form = MemberAccessForm(request.POST)
        if form.is_valid():
            update_workspace_member(
                workspace=workspace,
                user_id=user_id,
                can_write=form.cleaned_data["can_write"],
            )
    return HttpResponseRedirect(reverse("workspace-settings", args=[workspace.slug]))


@login_required
@require_http_methods(["GET"])
def workspace_export(request, workspace_slug):
    workspace = _workspace_membership(request.user, workspace_slug).workspace
    format_name = request.GET.get("format", "json").casefold()
    if format_name not in {"json", "csv"}:
        raise Http404("format must be json or csv.")
    document = export_inventory_document(workspace)
    if format_name == "csv":
        body = export_inventory_csv(document)
        content_type = "text/csv; charset=utf-8"
    else:
        body = json.dumps(document, ensure_ascii=False, indent=2)
        content_type = "application/json"
    response = HttpResponse(body, content_type=content_type)
    response["Content-Disposition"] = (
        f'attachment; filename="{workspace.slug}-inventory.{format_name}"'
    )
    return response


def _run_web_import(*, workspace, actor, format_name, content, dry_run, idempotency_key):
    document = parse_inventory_document(format_name=format_name, content=content)
    provenance = {"source_kind": "import", "client_actor": "web"}
    return import_inventory_document(
        workspace=workspace,
        actor=actor,
        document=document,
        dry_run=dry_run,
        idempotency_key=idempotency_key,
        provenance=provenance,
        request_hash=hash_request(
            {"document": document, "idempotency_key": idempotency_key, "provenance": provenance}
        ),
    )


@login_required
@require_http_methods(["GET", "POST"])
def workspace_transfer(request, workspace_slug):
    membership = _workspace_membership(request.user, workspace_slug)
    workspace = membership.workspace
    can_write = membership_can_write(membership)
    context = {
        "workspace": workspace,
        "can_write": can_write,
        "upload_form": InventoryImportUploadForm(),
    }

    if request.method == "POST":
        if not can_write:
            raise PermissionDenied(_("This inventory is shared as read-only."))
        action = request.POST.get("action")

        if action == "apply":
            format_name = request.POST.get("format", "json")
            content = request.POST.get("content", "")
            idempotency_key = request.POST.get("idempotency_key") or str(uuid4())
            try:
                summary, event, replayed = _run_web_import(
                    workspace=workspace,
                    actor=request.user,
                    format_name=format_name,
                    content=content,
                    dry_run=False,
                    idempotency_key=idempotency_key,
                )
            except InventoryTransferError as error:
                context["import_error"] = str(error)
                return render(request, "inventory/workspace_transfer.html", context, status=400)
            if replayed:
                messages.info(request, _("This import was already applied."))
            else:
                messages.success(request, _("Import applied."))
            return HttpResponseRedirect(reverse("event-history", args=[workspace.slug]))

        form = InventoryImportUploadForm(request.POST, request.FILES)
        context["upload_form"] = form
        if form.is_valid():
            try:
                content = form.read_content()
                summary, _event, _replayed = _run_web_import(
                    workspace=workspace,
                    actor=request.user,
                    format_name=form.cleaned_data["format"],
                    content=content,
                    dry_run=True,
                    idempotency_key=str(uuid4()),
                )
            except (InventoryTransferError, ValidationError) as error:
                context["import_error"] = getattr(error, "message", None) or str(error)
            else:
                context["preview"] = {
                    "rows": [
                        {"name": name, "created": counts["created"], "updated": counts["updated"]}
                        for name, counts in summary.items()
                    ],
                    "format": form.cleaned_data["format"],
                    "content": content,
                    "idempotency_key": str(uuid4()),
                }

    status_code = 400 if context.get("import_error") else 200
    return render(request, "inventory/workspace_transfer.html", context, status=status_code)


@login_required
def first_inventory(request, workspace_slug):
    membership = _workspace_membership(request.user, workspace_slug)
    return render(
        request,
        "inventory/first_inventory.html",
        {
            "workspace": membership.workspace,
            "can_write": membership_can_write(membership),
        },
    )


def _location_tree_options(locations):
    locations = list(locations)
    children = {}
    for location in locations:
        children.setdefault(location.parent_id, []).append(location)
    for siblings in children.values():
        siblings.sort(key=lambda location: (location.name.casefold(), location.id))

    options = []
    stack = [(location, 0) for location in reversed(children.get(None, []))]
    while stack:
        location, depth = stack.pop()
        prefix = "\u00a0\u00a0" * depth + ("⤷ " if depth else "")
        options.append({"key": location.key, "label": f"{prefix}{location.name}"})
        stack.extend((child, depth + 1) for child in reversed(children.get(location.id, [])))
    return options


def _location_paths(locations):
    locations = list(locations)
    by_id = {location.id: location for location in locations}
    paths = {}
    for location in locations:
        current = location
        path = []
        seen = set()
        while current and current.id not in seen:
            seen.add(current.id)
            path.append(current.name)
            current = by_id.get(current.parent_id)
        paths[location.id] = " → ".join(reversed(path))
    return paths


@login_required
def workspace_inventory(request, workspace_slug):
    membership = _workspace_membership(request.user, workspace_slug)
    workspace = membership.workspace
    query = request.GET.get("q", "").strip()
    category = request.GET.get("category", "").strip()
    location_key = request.GET.get("location", "").strip()
    if query:
        matching_holdings = search_holdings(
            workspace=workspace,
            query=query,
            category=category,
            location=location_key,
            limit=1001,
        )
        matching_count = (
            matching_holdings.count()
            if isinstance(matching_holdings, QuerySet)
            else len(matching_holdings)
        )
        truncated = matching_count > 1000
        page_obj = Paginator(matching_holdings[:1000], 25).get_page(request.GET.get("page"))
        add_search_match_details(page_obj, query)
    else:
        matching_holdings = workspace.holdings.select_related(
            "item", "location", "last_observed_by"
        ).order_by("item__name", "location__name", "id")
        if location_key:
            matching_holdings = matching_holdings.filter(
                location_id__in=location_scope_ids(
                    workspace=workspace,
                    location_key=location_key,
                    include_descendants=True,
                )
            )
        if category:
            matching_holdings = matching_holdings.filter(item__category__iexact=category)
        page_obj = Paginator(matching_holdings, 25).get_page(request.GET.get("page"))
        truncated = False
    preserved_query = request.GET.copy()
    preserved_query.pop("page", None)
    stock_status = get_stock_status(workspace=workspace)
    locations = list(workspace.locations.only("id", "parent_id", "key", "name"))
    location_paths = _location_paths(locations)
    category_options = []
    seen_categories = set()
    for option in (
        workspace.items.exclude(category="").values_list("category", flat=True).order_by("category")
    ):
        normalized = option.casefold()
        if normalized not in seen_categories:
            seen_categories.add(normalized)
            category_options.append(normalized)
    for holding in page_obj:
        holding.location_path = location_paths.get(holding.location_id, holding.location.name)
    return render(
        request,
        "inventory/workspace.html",
        {
            "workspace": workspace,
            "holdings": page_obj,
            "page_obj": page_obj,
            "truncated": truncated,
            "preserved_query": preserved_query.urlencode(),
            "location_options": _location_tree_options(locations),
            "query": query,
            "category": category,
            "category_options": category_options,
            "location_key": location_key,
            "stock_status": stock_status,
            "can_manage": user_can_manage_workspace(request.user, workspace),
            "can_write": membership_can_write(membership),
        },
    )


@login_required
def event_history(request, workspace_slug):
    membership = _workspace_membership(request.user, workspace_slug)
    workspace = membership.workspace
    events = workspace.inventory_events.select_related("actor").order_by("-created_at", "-id")
    page_obj = Paginator(events, 25).get_page(request.GET.get("page"))
    latest_id = None
    if page_obj.number == 1:
        first_event = next(iter(page_obj), None)
        latest_id = first_event.id if first_event else None
    item_ids = set()
    for event in page_obj:
        if event.kind != InventoryEvent.Kind.ITEM_UPDATE:
            continue
        try:
            item_ids.add(UUID(str(event.summary.get("item_id"))))
        except AttributeError, TypeError, ValueError:
            continue
    items_by_id = {
        str(item.id): item
        for item in Item.objects.filter(workspace=workspace, id__in=item_ids).only("id", "name")
    }
    for event in page_obj:
        event.change_lines = _event_change_lines(event)
        event.updated_item = items_by_id.get(str(event.summary.get("item_id")))
        if event.updated_item:
            translated_item_line = _("Item: %(item)s") % {"item": "{}"}
            event.updated_item_line = format_html(
                translated_item_line,
                format_html(
                    '<a href="{}">{}</a>',
                    reverse("web-item-detail", args=[workspace.slug, event.updated_item.id]),
                    event.updated_item.name,
                ),
            )
        event.can_undo = (
            membership_can_write(membership)
            and event.id == latest_id
            and event.kind in {InventoryEvent.Kind.BULK_UPSERT, InventoryEvent.Kind.MOVE}
            and bool(event.undo_data)
        )
    return render(
        request,
        "inventory/event_history.html",
        {
            "workspace": workspace,
            "events": page_obj,
            "page_obj": page_obj,
            "can_write": membership_can_write(membership),
        },
    )


@login_required
@require_http_methods(["GET", "POST"])
def event_undo(request, workspace_slug, event_id):
    workspace = _writable_workspace(request.user, workspace_slug)
    event = get_object_or_404(InventoryEvent, workspace=workspace, id=event_id)
    preview = preview_inventory_undo(workspace=workspace, event=event)
    if request.method == "POST":
        try:
            undo_inventory_event(
                workspace=workspace,
                actor=request.user,
                event_id=event.id,
                preview_token=request.POST.get("preview_token", ""),
            )
        except InventoryUndoError as error:
            preview = {"allowed": False, "reason": str(error)}
            return render(
                request,
                "inventory/event_undo.html",
                {"workspace": workspace, "event": event, "preview": preview},
                status=409,
            )
        return HttpResponseRedirect(reverse("event-history", args=[workspace.slug]))
    return render(
        request,
        "inventory/event_undo.html",
        {"workspace": workspace, "event": event, "preview": preview},
    )


def _inventory_form_context(
    workspace, title, submit_label, *, item_form=None, holding_form=None, location_form=None
):
    return {
        "workspace": workspace,
        "title": title,
        "submit_label": submit_label,
        "item_form": item_form,
        "holding_form": holding_form,
        "location_form": location_form,
    }


@login_required
@require_http_methods(["GET", "POST"])
def item_create(request, workspace_slug):
    workspace = _writable_workspace(request.user, workspace_slug)
    item_form = ItemForm(request.POST or None, workspace=workspace)
    holding_form = HoldingForm(
        request.POST or None,
        workspace=workspace,
        item=item_form.instance,
        prefix="holding",
    )
    if request.method == "POST" and item_form.is_valid() and holding_form.is_valid():
        holding_data = dict(holding_form.cleaned_data)
        activity = holding_data.pop("activity", "") or InventoryEvent.Activity.UNSPECIFIED
        item = create_item_with_holding(
            workspace=workspace,
            item_data=item_form.cleaned_data,
            holding_data=holding_data,
            actor=request.user,
            activity=activity,
        )
        return HttpResponseRedirect(reverse("web-item-detail", args=[workspace.slug, item.id]))
    return render(
        request,
        "inventory/inventory_form.html",
        _inventory_form_context(
            workspace,
            _("New item"),
            _("Create item"),
            item_form=item_form,
            holding_form=holding_form,
        ),
    )


@login_required
def item_list(request, workspace_slug):
    membership = _workspace_membership(request.user, workspace_slug)
    workspace = membership.workspace
    items = workspace.items.annotate(holding_count=Count("holdings")).order_by("name", "id")
    return render(
        request,
        "inventory/item_list.html",
        {
            "workspace": workspace,
            "items": items,
            "can_write": membership_can_write(membership),
        },
    )


def _book_catalog_input(item):
    attributes = item.attributes if isinstance(item.attributes, dict) else {}
    if attributes.get("schema") != "book" and item.category.casefold() not in {"book", "books"}:
        return None
    book = attributes.get("book") if isinstance(attributes.get("book"), dict) else {}

    def strings(field):
        value = book.get(field, [])
        if not isinstance(value, list):
            return []
        return [entry.strip() for entry in value if isinstance(entry, str) and entry.strip()]

    authors = strings("authors")
    if not authors and isinstance(attributes.get("author"), str):
        authors = [attributes["author"].strip()] if attributes["author"].strip() else []
    publishers = strings("publishers")
    if not publishers and isinstance(attributes.get("publisher"), str):
        publishers = [attributes["publisher"].strip()] if attributes["publisher"].strip() else []

    identifiers = attributes.get("identifiers")
    if not isinstance(identifiers, dict):
        identifiers = {}
    isbn = ""
    edition = ""
    for field in ("isbn_13", "isbn_10", "isbn"):
        values = identifiers.get(field, [])
        values = [values] if isinstance(values, str) else values
        if isinstance(values, list) and values and isinstance(values[0], str):
            isbn = values[0]
            break
    if not isbn:
        values = identifiers.get("openlibrary_edition", [])
        values = [values] if isinstance(values, str) else values
        if isinstance(values, list) and values and isinstance(values[0], str):
            edition = values[0]
    return {
        "title": item.name,
        "authors": authors,
        "publishers": publishers,
        "isbn": isbn,
        "edition": edition,
    }


def _item_attribute_rows(attributes):
    if not isinstance(attributes, dict):
        return []

    rows = []
    book = attributes.get("book")
    if isinstance(book, dict):
        labels = {
            "authors": _("Authors"),
            "publishers": _("Publishers"),
            "publication_date": _("Publication date"),
            "publication_year": _("Year"),
            "edition": _("Edition"),
            "language": _("Language"),
            "page_count": _("Pages"),
        }
        for key in labels:
            if key in book and book[key] not in (None, "", [], {}):
                rows.append({"label": labels[key], "value": _attribute_value(book[key])})
        for key in sorted(set(book) - set(labels) - {"title"}):
            rows.append(
                {
                    "label": _("Book · %(key)s") % {"key": key},
                    "value": _attribute_value(book[key]),
                }
            )

    identifiers = attributes.get("identifiers")
    if isinstance(identifiers, dict):
        identifier_labels = {
            "isbn": "ISBN",
            "isbn_10": "ISBN-10",
            "isbn_13": "ISBN-13",
            "openlibrary_edition": _("Open Library edition"),
        }
        for key in identifier_labels:
            if key in identifiers and identifiers[key] not in (None, "", [], {}):
                rows.append(
                    {
                        "label": identifier_labels[key],
                        "value": _attribute_value(identifiers[key]),
                    }
                )
        for key in sorted(set(identifiers) - set(identifier_labels)):
            rows.append(
                {
                    "label": _("Identifier · %(key)s") % {"key": key},
                    "value": _attribute_value(identifiers[key]),
                }
            )

    for key in sorted(set(attributes) - {"schema", "book", "identifiers"}):
        labels = {
            "author": _("Author"),
            "publisher": _("Publisher"),
        }
        rows.append({"label": labels.get(key, key), "value": _attribute_value(attributes[key])})
    return rows


def _attribute_value(value):
    if isinstance(value, list):
        return ", ".join(_attribute_value(entry) for entry in value)
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return str(value)


def _catalog_result_isbns(catalog_result):
    identifiers = catalog_result.get("identifiers", {})
    if not isinstance(identifiers, dict):
        return set()
    values = set()
    for field in ("isbn_13", "isbn_10", "isbn"):
        entries = identifiers.get(field, [])
        entries = [entries] if isinstance(entries, str) else entries
        if not isinstance(entries, list):
            continue
        for entry in entries:
            try:
                values.add(normalize_isbn(entry))
            except TypeError, ValueError:
                continue
    return values


@login_required
def item_detail(request, workspace_slug, item_id):
    membership = _workspace_membership(request.user, workspace_slug)
    workspace = membership.workspace
    item = get_object_or_404(Item, workspace=workspace, id=item_id)
    holdings = list(
        item.holdings.select_related("location", "last_observed_by").order_by(
            "location__name", "id"
        )
    )
    location_paths = _location_paths(workspace.locations.only("id", "parent_id", "name"))
    for holding in holdings:
        holding.location_path = location_paths.get(holding.location_id, holding.location.name)
    latest_item_edit = (
        workspace.inventory_events.select_related("actor")
        .filter(
            kind=InventoryEvent.Kind.ITEM_UPDATE,
            summary__item_id=str(item.id),
        )
        .order_by("-created_at", "-id")
        .first()
    )
    catalog_result = None
    catalog_error = ""
    catalog_input = _book_catalog_input(item)
    if catalog_input:
        try:
            catalog_result = lookup_book_details(**catalog_input)
        except ValueError as error:
            catalog_error = str(error)
        except CatalogRecordNotFound as error:
            catalog_error = str(error)
        except CatalogLookupError as error:
            catalog_error = str(error)
    return render(
        request,
        "inventory/item_detail.html",
        {
            "workspace": workspace,
            "item": item,
            "holdings": holdings,
            "latest_item_edit": latest_item_edit,
            "can_write": membership_can_write(membership),
            "catalog_result": catalog_result,
            "catalog_error": catalog_error,
            "item_attribute_rows": _item_attribute_rows(item.attributes),
            "item_labels": item.label_assertions.select_related("label").order_by("label__name"),
        },
    )


@login_required
@require_http_methods(["POST"])
def item_book_confirm(request, workspace_slug, item_id):
    workspace = _writable_workspace(request.user, workspace_slug)
    item = get_object_or_404(Item, workspace=workspace, id=item_id)
    if _book_catalog_input(item) is None:
        messages.error(request, _("The selected item is not a book."))
        return HttpResponseRedirect(reverse("web-item-detail", args=[workspace.slug, item.id]))
    isbn = request.POST.get("isbn", "").strip()
    edition = request.POST.get("edition", "").strip()
    if not isbn and not edition:
        messages.error(request, _("That edition has no confirmable identifier."))
        return HttpResponseRedirect(reverse("web-item-detail", args=[workspace.slug, item.id]))

    try:
        normalized_isbn = normalize_isbn(isbn) if isbn else ""
        normalized_edition = normalize_edition_key(edition) if edition else ""
        if normalized_isbn and normalized_edition:
            catalog_result = lookup_book_details(
                title=item.name,
                authors=[],
                publishers=[],
                edition=normalized_edition,
            )
            edition_isbns = _catalog_result_isbns(catalog_result)
            if edition_isbns and normalized_isbn not in edition_isbns:
                messages.error(
                    request,
                    _("The selected ISBN does not belong to that Open Library edition."),
                )
                return HttpResponseRedirect(
                    reverse("web-item-detail", args=[workspace.slug, item.id])
                )
        else:
            catalog_result = lookup_book_details(
                title=item.name,
                authors=[],
                publishers=[],
                isbn=normalized_isbn,
                edition=normalized_edition,
            )
    except ValueError as error:
        messages.error(request, str(error))
        return HttpResponseRedirect(reverse("web-item-detail", args=[workspace.slug, item.id]))
    except CatalogRecordNotFound as error:
        messages.error(request, str(error))
        return HttpResponseRedirect(reverse("web-item-detail", args=[workspace.slug, item.id]))
    except CatalogLookupError as error:
        messages.error(request, str(error))
        return HttpResponseRedirect(reverse("web-item-detail", args=[workspace.slug, item.id]))

    attributes = deepcopy(item.attributes) if isinstance(item.attributes, dict) else {}
    attributes["schema"] = "book"
    identifiers = attributes.get("identifiers")
    if not isinstance(identifiers, dict):
        identifiers = {}
    if normalized_isbn:
        identifiers["isbn"] = [normalized_isbn]
    if normalized_edition:
        identifiers["openlibrary_edition"] = [normalized_edition]
    attributes["identifiers"] = identifiers
    update_item(workspace=workspace, item=item, data={"attributes": attributes}, actor=request.user)
    messages.success(request, _("The Open Library edition was confirmed."))
    return HttpResponseRedirect(reverse("web-item-detail", args=[workspace.slug, item.id]))


@login_required
@require_http_methods(["GET", "POST"])
def item_edit(request, workspace_slug, item_id):
    workspace = _writable_workspace(request.user, workspace_slug)
    item = get_object_or_404(Item, workspace=workspace, id=item_id)
    form = ItemForm(request.POST or None, instance=item, workspace=workspace)
    if request.method == "POST" and form.is_valid():
        item = update_item(
            workspace=workspace,
            item=item,
            data=form.cleaned_data,
            actor=request.user,
        )
        return HttpResponseRedirect(reverse("web-item-detail", args=[workspace.slug, item.id]))
    return render(
        request,
        "inventory/inventory_form.html",
        _inventory_form_context(workspace, _("Edit item"), _("Save changes"), item_form=form),
    )


@login_required
@require_http_methods(["GET", "POST"])
def item_delete(request, workspace_slug, item_id):
    workspace = _writable_workspace(request.user, workspace_slug)
    item = get_object_or_404(Item, workspace=workspace, id=item_id)
    if request.method == "POST":
        remove_item(workspace=workspace, item=item)
        return HttpResponseRedirect(reverse("workspace-inventory", args=[workspace.slug]))
    return render(
        request,
        "inventory/confirm_delete.html",
        {
            "workspace": workspace,
            "object_name": item.name,
            "detail": _("All holdings for this item will also be deleted."),
        },
    )


@login_required
@require_http_methods(["GET", "POST"])
def holding_create(request, workspace_slug, item_id):
    workspace = _writable_workspace(request.user, workspace_slug)
    item = get_object_or_404(Item, workspace=workspace, id=item_id)
    form = HoldingForm(request.POST or None, workspace=workspace, item=item)
    if request.method == "POST" and form.is_valid():
        data = dict(form.cleaned_data)
        activity = data.pop("activity", "") or InventoryEvent.Activity.UNSPECIFIED
        create_holding(
            workspace=workspace, item=item, data=data, actor=request.user, activity=activity
        )
        return HttpResponseRedirect(reverse("web-item-detail", args=[workspace.slug, item.id]))
    return render(
        request,
        "inventory/inventory_form.html",
        _inventory_form_context(workspace, _("Add holding"), _("Add holding"), holding_form=form),
    )


@login_required
@require_http_methods(["GET", "POST"])
def holding_edit(request, workspace_slug, item_id, holding_id):
    workspace = _writable_workspace(request.user, workspace_slug)
    item = get_object_or_404(Item, workspace=workspace, id=item_id)
    holding = get_object_or_404(Holding, workspace=workspace, item=item, id=holding_id)
    form = HoldingForm(
        request.POST or None,
        instance=holding,
        workspace=workspace,
        item=item,
    )
    if request.method == "POST" and form.is_valid():
        data = dict(form.cleaned_data)
        activity = data.pop("activity", "") or InventoryEvent.Activity.UNSPECIFIED
        update_holding(
            workspace=workspace,
            item=item,
            holding=holding,
            data=data,
            actor=request.user,
            activity=activity,
        )
        return HttpResponseRedirect(reverse("web-item-detail", args=[workspace.slug, item.id]))
    return render(
        request,
        "inventory/inventory_form.html",
        _inventory_form_context(workspace, _("Edit holding"), _("Save changes"), holding_form=form),
    )


@login_required
@require_http_methods(["GET", "POST"])
def holding_delete(request, workspace_slug, item_id, holding_id):
    workspace = _writable_workspace(request.user, workspace_slug)
    item = get_object_or_404(Item, workspace=workspace, id=item_id)
    holding = get_object_or_404(Holding, workspace=workspace, item=item, id=holding_id)
    if request.method == "POST":
        remove_holding(workspace=workspace, item=item, holding=holding)
        return HttpResponseRedirect(reverse("web-item-detail", args=[workspace.slug, item.id]))
    return render(
        request,
        "inventory/confirm_delete.html",
        {
            "workspace": workspace,
            "object_name": holding.location.name,
            "detail": _("The item itself will remain."),
        },
    )


@login_required
@require_http_methods(["POST"])
def item_label_add(request, workspace_slug, item_id):
    workspace = _writable_workspace(request.user, workspace_slug)
    item = get_object_or_404(Item, workspace=workspace, id=item_id)
    value = (request.POST.get("value") or "").strip()
    if value:
        payload = {
            "assertions": [{"item_key": item.key, "value": value, "source": "user"}],
            "idempotency_key": f"web-label-{uuid4()}",
            "provenance": {"source_kind": "manual", "client_actor": "web"},
        }
        try:
            assert_item_labels(
                workspace=workspace,
                actor=request.user,
                data=payload,
                request_hash=hash_request(payload),
            )
        except (BulkUpsertError, LabelConflictError, ValidationError) as error:
            messages.error(request, _("Could not add the label: %(error)s") % {"error": error})
    return HttpResponseRedirect(reverse("web-item-detail", args=[workspace.slug, item.id]))


@login_required
@require_http_methods(["POST"])
def item_label_remove(request, workspace_slug, item_id, assertion_id):
    workspace = _writable_workspace(request.user, workspace_slug)
    item = get_object_or_404(Item, workspace=workspace, id=item_id)
    ItemLabel.objects.filter(workspace=workspace, item=item, id=assertion_id).delete()
    return HttpResponseRedirect(reverse("web-item-detail", args=[workspace.slug, item.id]))


@login_required
@require_http_methods(["GET", "POST"])
def workspace_public_links(request, workspace_slug):
    membership = _workspace_membership(request.user, workspace_slug)
    workspace = membership.workspace
    can_write = membership_can_write(membership)
    form = PublicSearchLinkForm(workspace=workspace)

    if request.method == "POST":
        if not can_write:
            raise PermissionDenied(_("This inventory is shared as read-only."))
        action = request.POST.get("action")
        link_id = request.POST.get("link_id")

        if action in {"revoke", "rotate"} and link_id:
            link = get_object_or_404(workspace.public_search_links, id=link_id)
            if action == "revoke":
                link.revoke()
                messages.success(request, _("Public link revoked."))
            else:
                secret = link.rotate_secret()
                messages.success(
                    request,
                    _("New URL (copy it now): %(url)s") % {"url": _public_link_url(secret)},
                )
            return HttpResponseRedirect(reverse("web-public-links", args=[workspace.slug]))

        form = PublicSearchLinkForm(request.POST, workspace=workspace)
        if form.is_valid():
            link, secret = PublicSearchLink.issue(
                workspace=workspace,
                location=form.cleaned_data["location"],
                name=form.cleaned_data["name"],
                created_by=request.user,
                category=form.cleaned_data["category"],
                include_descendants=form.cleaned_data["include_descendants"],
                expires_at=form.cleaned_data["expires_at"],
            )
            messages.success(
                request,
                _("Public link created. URL (copy it now): %(url)s")
                % {"url": _public_link_url(secret)},
            )
            return HttpResponseRedirect(reverse("web-public-links", args=[workspace.slug]))

    links = workspace.public_search_links.select_related("location")
    return render(
        request,
        "inventory/workspace_public_links.html",
        {"workspace": workspace, "can_write": can_write, "form": form, "links": links},
    )


def _public_link_url(secret):
    return f"{settings.PUBLIC_BASE_URL}{reverse('public-inventory-search', args=[secret])}"


@login_required
def location_list(request, workspace_slug):
    membership = _workspace_membership(request.user, workspace_slug)
    workspace = membership.workspace
    locations = list(workspace.locations.select_related("last_observed_by").order_by("name", "id"))
    paths = _location_paths(locations)
    for location in locations:
        location.path_label = paths[location.id]
    return render(
        request,
        "inventory/location_list.html",
        {
            "workspace": workspace,
            "locations": locations,
            "can_write": membership_can_write(membership),
        },
    )


@login_required
@require_http_methods(["GET", "POST"])
def location_create(request, workspace_slug):
    workspace = _writable_workspace(request.user, workspace_slug)
    form = LocationForm(request.POST or None, workspace=workspace)
    if request.method == "POST" and form.is_valid():
        create_location(workspace=workspace, data=form.cleaned_data)
        return HttpResponseRedirect(reverse("web-location-list", args=[workspace.slug]))
    return render(
        request,
        "inventory/inventory_form.html",
        _inventory_form_context(
            workspace, _("New location"), _("Create location"), location_form=form
        ),
    )


@login_required
@require_http_methods(["GET", "POST"])
def location_edit(request, workspace_slug, location_id):
    workspace = _writable_workspace(request.user, workspace_slug)
    location = get_object_or_404(Location, workspace=workspace, id=location_id)
    form = LocationForm(request.POST or None, instance=location, workspace=workspace)
    if request.method == "POST" and form.is_valid():
        update_location(workspace=workspace, location=location, data=form.cleaned_data)
        return HttpResponseRedirect(reverse("web-location-list", args=[workspace.slug]))
    return render(
        request,
        "inventory/inventory_form.html",
        _inventory_form_context(
            workspace, _("Edit location"), _("Save changes"), location_form=form
        ),
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

    def require_write_access(self):
        workspace = self.get_workspace()
        require_workspace_write(self.request, workspace)
        return workspace


class WorkspaceScopedViewSet(WorkspaceAccessMixin, viewsets.ModelViewSet):
    def get_queryset(self):
        return super().get_queryset().filter(workspace=self.get_workspace())

    def perform_create(self, serializer):
        with transaction.atomic():
            workspace = self.require_write_access()
            Workspace.objects.select_for_update().get(pk=workspace.pk)
            serializer.save(workspace=workspace)

    def perform_update(self, serializer):
        with transaction.atomic():
            Workspace.objects.select_for_update().get(pk=self.require_write_access().pk)
            serializer.save()

    def perform_destroy(self, instance):
        with transaction.atomic():
            Workspace.objects.select_for_update().get(pk=self.require_write_access().pk)
            instance.delete()


class LocationViewSet(WorkspaceScopedViewSet):
    queryset = Location.objects.select_related("parent").order_by("name", "id")
    serializer_class = LocationSerializer
    filter_backends = [filters.SearchFilter]
    search_fields = ["key", "name", "description", "kind"]


class LocationRelationViewSet(WorkspaceScopedViewSet):
    queryset = LocationRelation.objects.select_related("subject", "object").order_by(
        "subject__name", "relation", "object__name", "id"
    )
    serializer_class = LocationRelationSerializer


class ItemViewSet(WorkspaceScopedViewSet):
    queryset = Item.objects.order_by("name", "id")
    serializer_class = ItemSerializer
    filter_backends = [filters.SearchFilter]
    search_fields = ["key", "name", "description", "category"]

    def perform_update(self, serializer):
        try:
            serializer.instance = update_item(
                workspace=self.require_write_access(),
                item=serializer.instance,
                data=serializer.validated_data,
                actor=self.request.user,
            )
        except ValidationError as error:
            raise serializers.ValidationError(error.messages)


class HoldingViewSet(WorkspaceScopedViewSet):
    queryset = Holding.objects.select_related("item", "location").order_by(
        "item__name", "location__name", "id"
    )
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
        self.require_write_access()
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


class LabelSuggestionView(WorkspaceAccessMixin, GenericAPIView):
    serializer_class = LabelSuggestionQuerySerializer

    @extend_schema(
        parameters=[LabelSuggestionQuerySerializer],
        responses=LabelSuggestionSerializer(many=True),
    )
    def get(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.query_params)
        serializer.is_valid(raise_exception=True)
        suggestions = suggest_labels(
            workspace=self.get_workspace(),
            query=serializer.validated_data["q"],
            limit=serializer.validated_data["limit"],
        )
        return Response(LabelSuggestionSerializer(suggestions, many=True).data)


class ItemLabelAssertionView(WorkspaceAccessMixin, GenericAPIView):
    serializer_class = ItemLabelAssertionRequestSerializer

    @extend_schema(
        responses={
            status.HTTP_200_OK: ItemLabelAssertionResultSerializer,
            status.HTTP_201_CREATED: ItemLabelAssertionResultSerializer,
        }
    )
    def post(self, request, *args, **kwargs):
        self.require_write_access()
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            event, replayed = assert_item_labels(
                workspace=self.get_workspace(),
                actor=request.user,
                data=serializer.validated_data,
                request_hash=hash_request(serializer.validated_data),
            )
        except (IdempotencyConflict, LabelConflictError) as error:
            return Response({"detail": str(error)}, status=status.HTTP_409_CONFLICT)
        except BulkUpsertError as error:
            return Response({"detail": str(error)}, status=status.HTTP_400_BAD_REQUEST)

        output = ItemLabelAssertionResultSerializer(
            {
                "event_id": event.id,
                "replayed": replayed,
                "processed": event.summary,
            }
        )
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
        self.require_write_access()
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


class BookLookupView(WorkspaceAccessMixin, GenericAPIView):
    serializer_class = BookLookupResultSerializer

    @extend_schema(responses=BookLookupResultSerializer)
    def get(self, request, isbn, *args, **kwargs):
        self.get_workspace()
        try:
            result = lookup_book_by_isbn(isbn)
        except ValueError as error:
            return Response({"detail": str(error)}, status=status.HTTP_400_BAD_REQUEST)
        except CatalogRecordNotFound as error:
            return Response({"detail": str(error)}, status=status.HTTP_404_NOT_FOUND)
        except CatalogLookupError as error:
            return Response({"detail": str(error)}, status=status.HTTP_502_BAD_GATEWAY)
        return Response(self.get_serializer(result).data)


class WorkspaceViewSet(viewsets.ModelViewSet):
    queryset = Workspace.objects.order_by("name", "id")
    serializer_class = WorkspaceSerializer
    http_method_names = ["get", "post", "head", "options"]

    def get_queryset(self):
        queryset = (
            Workspace.objects.filter(memberships__user=self.request.user)
            .distinct()
            .order_by("name", "id")
        )
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
        workspace = self.require_write_access()
        token, raw_token = ApiToken.issue(
            workspace=workspace,
            user=request.user,
            name=serializer.validated_data["name"],
            can_write=serializer.validated_data["can_write"],
        )
        output = ApiTokenIssuedSerializer(
            {
                "id": token.id,
                "name": token.name,
                "prefix": token.prefix,
                "created_at": token.created_at,
                "revoked_at": token.revoked_at,
                "can_write": token.can_write,
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
            self.require_write_access().api_tokens,
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
            limit=1001,
        )
        result_count = results.count() if isinstance(results, QuerySet) else len(results)
        truncated = result_count > 1000
        results = results[:1000]
        paginator = InventoryPagination()
        page_results = paginator.paginate_queryset(results, request, view=self)
        add_search_match_details(page_results, query)
        clue_context = build_holding_clue_context(
            workspace=self.get_workspace(), holdings=page_results
        )
        output = SearchResultSerializer(
            {
                "query": query,
                "count": min(result_count, 1000),
                "truncated": truncated,
                "pagination": paginator.metadata(),
                "results": page_results,
            },
            context=clue_context,
        )
        return Response(output.data)


def _public_link_secret_payload(link, raw_secret):
    """Serialize a link plus its one-time shareable URL."""
    data = PublicSearchLinkSerializer(link).data
    path = reverse("public-inventory-search", args=[raw_secret])
    data["url"] = f"{settings.PUBLIC_BASE_URL}{path}"
    return data


class PublicSearchLinkView(WorkspaceAccessMixin, GenericAPIView):
    """Members list a workspace's public search links; writers create them."""

    serializer_class = PublicSearchLinkCreateSerializer

    @extend_schema(responses=PublicSearchLinkSerializer(many=True))
    def get(self, request, *args, **kwargs):
        links = self.get_workspace().public_search_links.select_related("location")
        return Response(PublicSearchLinkSerializer(links, many=True).data)

    @extend_schema(
        request=PublicSearchLinkCreateSerializer,
        responses={status.HTTP_201_CREATED: PublicSearchLinkSecretSerializer},
    )
    def post(self, request, *args, **kwargs):
        workspace = self.require_write_access()
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        location = get_object_or_404(Location, workspace=workspace, key=data["location_key"])
        link, raw_secret = PublicSearchLink.issue(
            workspace=workspace,
            location=location,
            name=data["name"],
            created_by=request.user,
            category=data.get("category", ""),
            include_descendants=data["include_descendants"],
            expires_at=data.get("expires_at"),
        )
        return Response(
            _public_link_secret_payload(link, raw_secret), status=status.HTTP_201_CREATED
        )


class PublicSearchLinkLookupMixin(WorkspaceAccessMixin):
    def get_link(self, *, writable):
        workspace = self.require_write_access() if writable else self.get_workspace()
        return get_object_or_404(
            PublicSearchLink.objects.select_related("location"),
            workspace=workspace,
            pk=self.kwargs["link_id"],
        )


class PublicSearchLinkDetailView(PublicSearchLinkLookupMixin, GenericAPIView):
    serializer_class = PublicSearchLinkSerializer

    @extend_schema(responses=PublicSearchLinkSerializer)
    def get(self, request, *args, **kwargs):
        return Response(PublicSearchLinkSerializer(self.get_link(writable=False)).data)

    @extend_schema(request=None, responses={status.HTTP_204_NO_CONTENT: None})
    def delete(self, request, *args, **kwargs):
        self.get_link(writable=True).revoke()
        return Response(status=status.HTTP_204_NO_CONTENT)


class PublicSearchLinkRotateView(PublicSearchLinkLookupMixin, GenericAPIView):
    serializer_class = PublicSearchLinkSecretSerializer

    @extend_schema(request=None, responses=PublicSearchLinkSecretSerializer)
    def post(self, request, *args, **kwargs):
        link = self.get_link(writable=True)
        if not link.is_active:
            return Response(
                {"detail": _("Cannot rotate a revoked or expired link.")},
                status=status.HTTP_409_CONFLICT,
            )
        raw_secret = link.rotate_secret()
        return Response(_public_link_secret_payload(link, raw_secret))


def _qr_label_svg(qr, caption):
    """A printable SVG: the QR code with the link's scope name underneath."""
    data_uri = qr.svg_data_uri(scale=8, border=2)
    safe_caption = html.escape(caption)
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" width="360" height="420" '
        'viewBox="0 0 360 420">'
        '<rect width="360" height="420" fill="#ffffff"/>'
        f'<image x="40" y="24" width="280" height="280" href="{data_uri}"/>'
        f'<text x="180" y="338" text-anchor="middle" font-family="sans-serif" '
        f'font-size="20" font-weight="bold" fill="#111111">{safe_caption}</text>'
        '<text x="180" y="368" text-anchor="middle" font-family="sans-serif" '
        'font-size="14" fill="#444444">Scan to search</text>'
        "</svg>"
    )


class PublicSearchLinkQRView(PublicSearchLinkLookupMixin, GenericAPIView):
    """Return a QR code, or a printable label, that encodes only the public URL."""

    serializer_class = PublicSearchLinkSerializer

    @extend_schema(
        parameters=[
            OpenApiParameter(
                "format",
                str,
                OpenApiParameter.QUERY,
                enum=["svg", "png"],
                description="Image format for the bare QR code (default svg).",
            ),
            OpenApiParameter(
                "label",
                bool,
                OpenApiParameter.QUERY,
                description="Return a printable SVG label captioned with the scope name.",
            ),
        ],
        responses={
            200: OpenApiResponse(description="QR image (image/svg+xml or image/png)."),
        },
    )
    def get(self, request, *args, **kwargs):
        link = self.get_link(writable=False)
        if not link.is_active:
            raise Http404("This public search link is revoked or expired.")
        path = reverse("public-inventory-search", args=[link.secret])
        qr = segno.make(f"{settings.PUBLIC_BASE_URL}{path}", error="m")

        if request.query_params.get("label", "").lower() in ("1", "true", "yes"):
            return HttpResponse(_qr_label_svg(qr, link.name), content_type="image/svg+xml")

        buffer = BytesIO()
        if request.query_params.get("format", "svg").lower() == "png":
            qr.save(buffer, kind="png", scale=8, border=2)
            return HttpResponse(buffer.getvalue(), content_type="image/png")
        qr.save(buffer, kind="svg", scale=8, border=2)
        return HttpResponse(buffer.getvalue(), content_type="image/svg+xml")


class PublicInventorySearchView(GenericAPIView):
    """Unauthenticated, GET-only, read-only search bound to one public link."""

    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "public-search"
    serializer_class = PublicSearchQuerySerializer

    @extend_schema(parameters=[PublicSearchQuerySerializer], responses=PublicSearchResultSerializer)
    def get(self, request, *args, **kwargs):
        link = resolve_public_search_link(kwargs["secret"])
        if link is None:
            raise Http404("Unknown or inactive public search link.")
        serializer = self.get_serializer(data=request.query_params)
        serializer.is_valid(raise_exception=True)
        query = serializer.validated_data["q"].strip()
        results = search_holdings(
            workspace=link.workspace,
            query=query,
            category=link.category,
            location=link.location.key,
            include_descendants=link.include_descendants,
            limit=1001,
        )
        result_count = results.count() if isinstance(results, QuerySet) else len(results)
        truncated = result_count > 1000
        results = results[:1000]
        paginator = PublicSearchPagination()
        page_results = paginator.paginate_queryset(results, request, view=self)
        add_search_match_details(page_results, query)
        clue_context = build_holding_clue_context(workspace=link.workspace, holdings=page_results)
        record_public_search_link_use(link)
        output = PublicSearchResultSerializer(
            {
                "scope": link.name,
                "query": query,
                "count": min(result_count, 1000),
                "truncated": truncated,
                "pagination": paginator.metadata(),
                "results": page_results,
            },
            context={"location_paths": clue_context["location_paths"]},
        )
        return Response(output.data)


class StockStatusView(WorkspaceAccessMixin, GenericAPIView):
    serializer_class = StockStatusResultSerializer

    @extend_schema(responses=StockStatusResultSerializer)
    def get(self, request, *args, **kwargs):
        result = get_stock_status(workspace=self.get_workspace())
        return Response(StockStatusResultSerializer(result).data)


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
                    can_write=request.POST.get("can_write") == "on",
                )
                authorization_request.delete()
            redirect_params.update(
                code=raw_code,
                iss=settings.PUBLIC_BASE_URL,
            )
        from mcp.server.auth.provider import construct_redirect_uri

        return HttpResponseRedirect(construct_redirect_uri(redirect_uri, **redirect_params))

    memberships = Membership.objects.filter(user=request.user).select_related("workspace")
    if not memberships.exists():
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
            or _("An application"),
            "memberships": [
                {
                    "workspace": membership.workspace,
                    "can_write": membership_can_write(membership),
                }
                for membership in memberships
            ],
        },
    )
