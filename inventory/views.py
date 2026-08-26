import json
from io import BytesIO
from uuid import UUID
from zipfile import ZIP_DEFLATED, ZipFile

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
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
from django.utils.translation import gettext as _
from django.utils.translation import ngettext
from django.views.decorators.http import require_http_methods
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import filters, status, viewsets
from rest_framework.generics import GenericAPIView
from rest_framework.response import Response

from .catalogs import CatalogLookupError, CatalogRecordNotFound, lookup_book_by_isbn
from .forms import (
    HoldingForm,
    ItemForm,
    LocationForm,
    MemberAccessForm,
    WorkspaceCreateForm,
    WorkspaceRenameForm,
    WorkspaceShareForm,
)
from .models import (
    ApiToken,
    Holding,
    InventoryEvent,
    Item,
    Location,
    LocationRelation,
    Membership,
    OAuthAuthorizationRequest,
    VerificationStatus,
    Workspace,
)
from .oauth import create_authorization_grant
from .pagination import InventoryPagination
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
    InventoryUndoError,
    add_search_match_details,
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
    remove_holding,
    remove_item,
    remove_workspace_member,
    rename_workspace,
    search_holdings,
    share_workspace,
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
    return ngettext("%(count)s spatial relation", "%(count)s spatial relations", count) % {
        "count": count
    }


def _inventory_count_lines(summary):
    lines = []
    for key in ("locations", "items", "holdings", "location_relations"):
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
    location_key = request.GET.get("location", "").strip()
    if query:
        matching_holdings = search_holdings(
            workspace=workspace,
            query=query,
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
        page_obj = Paginator(matching_holdings, 25).get_page(request.GET.get("page"))
        truncated = False
    preserved_query = request.GET.copy()
    preserved_query.pop("page", None)
    stock_status = get_stock_status(workspace=workspace)
    locations = list(workspace.locations.only("id", "parent_id", "key", "name"))
    location_paths = _location_paths(locations)
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
    latest_id = events.values_list("id", flat=True).first()
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
        item = create_item_with_holding(
            workspace=workspace,
            item_data=item_form.cleaned_data,
            holding_data=holding_form.cleaned_data,
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
    return render(
        request,
        "inventory/item_detail.html",
        {
            "workspace": workspace,
            "item": item,
            "holdings": holdings,
            "can_write": membership_can_write(membership),
        },
    )


@login_required
@require_http_methods(["GET", "POST"])
def item_edit(request, workspace_slug, item_id):
    workspace = _writable_workspace(request.user, workspace_slug)
    item = get_object_or_404(Item, workspace=workspace, id=item_id)
    form = ItemForm(request.POST or None, instance=item, workspace=workspace)
    if request.method == "POST" and form.is_valid():
        item = update_item(workspace=workspace, item=item, data=form.cleaned_data)
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
        create_holding(workspace=workspace, item=item, data=form.cleaned_data)
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
        update_holding(
            workspace=workspace,
            item=item,
            holding=holding,
            data=form.cleaned_data,
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
