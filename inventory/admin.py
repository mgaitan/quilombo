from allauth.account.models import EmailAddress
from allauth.socialaccount.models import SocialAccount, SocialApp, SocialToken
from django.contrib import admin
from django.contrib.auth import get_user_model
from django.contrib.auth.admin import UserAdmin as AuthUserAdmin
from django.db.models import Count

from .models import (
    AccessEvent,
    ApiToken,
    Holding,
    InventoryEvent,
    Item,
    Location,
    LocationRelation,
    Membership,
    Workspace,
)

User = get_user_model()

admin.site.index_template = "inventory/admin_index.html"
admin.site.site_header = "Quilombo administration"
admin.site.site_title = "Quilombo admin"
admin.site.index_title = "Operations"


admin.site.unregister(User)


@admin.register(User)
class UserAdmin(AuthUserAdmin):
    list_display = ("username", "email", "is_active", "is_staff", "date_joined", "last_login")
    list_filter = ("is_active", "is_staff", "is_superuser", "date_joined")
    search_fields = ("username", "email", "first_name", "last_name")
    ordering = ("-date_joined", "username")
    date_hierarchy = "date_joined"


admin.site.unregister(EmailAddress)


@admin.register(EmailAddress)
class EmailAddressAdmin(admin.ModelAdmin):
    list_display = ("email", "user", "verified", "primary")
    list_filter = ("verified", "primary")
    search_fields = ("email", "user__username", "user__email")
    list_select_related = ("user",)


admin.site.unregister(SocialAccount)


@admin.register(SocialAccount)
class SocialAccountAdmin(admin.ModelAdmin):
    list_display = ("user", "provider", "uid", "last_login", "date_joined")
    list_filter = ("provider", "last_login", "date_joined")
    search_fields = ("user__username", "user__email", "uid")
    list_select_related = ("user",)
    date_hierarchy = "date_joined"


admin.site.unregister(SocialApp)


@admin.register(SocialApp)
class SocialAppAdmin(admin.ModelAdmin):
    list_display = ("provider", "name", "client_id")
    list_filter = ("provider",)
    search_fields = ("name", "client_id")
    exclude = ("secret", "key")


admin.site.unregister(SocialToken)


@admin.register(Workspace)
class WorkspaceAdmin(admin.ModelAdmin):
    list_display = ("name", "slug", "member_count", "item_count", "event_count", "created_at")
    list_filter = ("created_at",)
    search_fields = ("name", "slug")
    ordering = ("-created_at", "name")
    date_hierarchy = "created_at"

    def get_queryset(self, request):
        return (
            super()
            .get_queryset(request)
            .annotate(
                member_count=Count("members", distinct=True),
                item_count=Count("items", distinct=True),
                event_count=Count("inventory_events", distinct=True),
            )
        )

    @admin.display(description="Members", ordering="member_count")
    def member_count(self, obj):
        return obj.member_count

    @admin.display(description="Items", ordering="item_count")
    def item_count(self, obj):
        return obj.item_count

    @admin.display(description="Events", ordering="event_count")
    def event_count(self, obj):
        return obj.event_count


@admin.register(Membership)
class MembershipAdmin(admin.ModelAdmin):
    list_display = ("workspace", "user", "role", "can_write", "created_at")
    list_filter = ("role", "can_write", "created_at")
    search_fields = ("workspace__name", "workspace__slug", "user__username", "user__email")
    list_select_related = ("workspace", "user")
    ordering = ("-created_at", "workspace__name", "user__username")
    date_hierarchy = "created_at"


@admin.register(Location)
class LocationAdmin(admin.ModelAdmin):
    list_display = (
        "name",
        "key",
        "workspace",
        "kind",
        "verification_status",
        "last_observed_at",
        "updated_at",
    )
    list_filter = ("workspace", "kind", "verification_status", "last_observed_at")
    search_fields = ("name", "key", "workspace__name", "workspace__slug")
    list_select_related = ("workspace", "parent", "last_observed_by")
    ordering = ("-updated_at", "workspace__name", "name")
    date_hierarchy = "created_at"


@admin.register(LocationRelation)
class LocationRelationAdmin(admin.ModelAdmin):
    list_display = ("workspace", "subject", "relation", "object", "created_at")
    list_filter = ("workspace", "relation", "created_at")
    search_fields = (
        "workspace__name",
        "workspace__slug",
        "subject__name",
        "subject__key",
        "object__name",
        "object__key",
    )
    list_select_related = ("workspace", "subject", "object")
    ordering = ("-created_at", "workspace__name", "subject__name")
    date_hierarchy = "created_at"


@admin.register(Item)
class ItemAdmin(admin.ModelAdmin):
    list_display = (
        "name",
        "key",
        "workspace",
        "category",
        "tracking_mode",
        "holding_count",
        "created_at",
        "updated_at",
    )
    list_filter = ("workspace", "category", "tracking_mode", "created_at", "updated_at")
    search_fields = ("name", "key", "category", "description", "workspace__name", "workspace__slug")
    list_select_related = ("workspace",)
    ordering = ("-updated_at", "workspace__name", "name")
    date_hierarchy = "created_at"

    def get_queryset(self, request):
        return (
            super().get_queryset(request).annotate(holding_count=Count("holdings", distinct=True))
        )

    @admin.display(description="Holdings", ordering="holding_count")
    def holding_count(self, obj):
        return obj.holding_count


@admin.register(Holding)
class HoldingAdmin(admin.ModelAdmin):
    list_display = (
        "item",
        "location",
        "workspace",
        "quantity",
        "approximate",
        "verification_status",
        "last_observed_at",
        "updated_at",
    )
    list_filter = (
        "workspace",
        "item__category",
        "approximate",
        "verification_status",
        "last_observed_at",
    )
    search_fields = (
        "item__name",
        "item__key",
        "location__name",
        "location__key",
        "workspace__name",
        "workspace__slug",
        "notes",
    )
    list_select_related = ("workspace", "item", "location", "last_observed_by")
    ordering = ("-updated_at", "workspace__name", "item__name", "location__name")
    date_hierarchy = "updated_at"


@admin.register(InventoryEvent)
class InventoryEventAdmin(admin.ModelAdmin):
    list_display = (
        "created_at",
        "kind",
        "workspace",
        "actor",
        "source_kind",
        "client_actor",
        "observed_at",
    )
    list_filter = ("kind", "source_kind", "workspace", "created_at", "observed_at")
    search_fields = (
        "workspace__name",
        "workspace__slug",
        "actor__username",
        "actor__email",
        "client_actor",
        "source_reference",
    )
    list_select_related = ("workspace", "actor")
    ordering = ("-created_at", "-id")
    date_hierarchy = "created_at"
    exclude = ("idempotency_key", "request_hash", "undo_data")


@admin.register(ApiToken)
class ApiTokenAdmin(admin.ModelAdmin):
    list_display = ("name", "prefix", "workspace", "user", "can_write", "created_at", "revoked_at")
    list_filter = ("workspace", "can_write", "revoked_at", "created_at")
    search_fields = (
        "name",
        "prefix",
        "workspace__name",
        "workspace__slug",
        "user__username",
        "user__email",
    )
    list_select_related = ("workspace", "user")
    ordering = ("-created_at", "workspace__name", "name")
    date_hierarchy = "created_at"
    exclude = ("token_hash",)


@admin.register(AccessEvent)
class AccessEventAdmin(admin.ModelAdmin):
    list_display = ("created_at", "channel", "user", "client_name")
    list_filter = ("channel", "created_at")
    search_fields = ("user__username", "user__email", "client_name")
    list_select_related = ("user",)
    ordering = ("-created_at",)
    date_hierarchy = "created_at"
