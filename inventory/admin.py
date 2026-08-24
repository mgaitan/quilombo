from django.contrib import admin

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

admin.site.index_template = "inventory/admin_index.html"
admin.site.site_header = "Quilombo administration"
admin.site.site_title = "Quilombo admin"
admin.site.index_title = "Operations"

admin.site.register(
    [
        Workspace,
        Membership,
        Location,
        LocationRelation,
        Item,
        Holding,
        InventoryEvent,
        ApiToken,
    ]
)


@admin.register(AccessEvent)
class AccessEventAdmin(admin.ModelAdmin):
    list_display = ("created_at", "channel", "user", "client_name")
    list_filter = ("channel", "created_at")
    search_fields = ("user__username", "client_name")
    readonly_fields = ("user", "channel", "client_name", "created_at")
