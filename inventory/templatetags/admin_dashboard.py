from datetime import timedelta

from django import template
from django.contrib.auth import get_user_model
from django.urls import reverse
from django.utils import timezone

from inventory.models import AccessEvent, Item, Location

register = template.Library()


@register.simple_tag
def admin_weekly_stats():
    cutoff = timezone.now() - timedelta(days=7)
    user_model = get_user_model()
    sections = (
        {
            "label": "Users",
            "total": user_model.objects.count(),
            "recent": user_model.objects.filter(date_joined__gte=cutoff).order_by("-date_joined")[
                :5
            ],
            "recent_count": user_model.objects.filter(date_joined__gte=cutoff).count(),
            "list_url": reverse("admin:auth_user_changelist"),
            "change_url_name": "admin:auth_user_change",
        },
        {
            "label": "Objects",
            "total": Item.objects.count(),
            "recent": Item.objects.select_related("workspace")
            .filter(created_at__gte=cutoff)
            .order_by("-created_at")[:5],
            "recent_count": Item.objects.filter(created_at__gte=cutoff).count(),
            "list_url": reverse("admin:inventory_item_changelist"),
            "change_url_name": "admin:inventory_item_change",
        },
        {
            "label": "Locations",
            "total": Location.objects.count(),
            "recent": Location.objects.select_related("workspace")
            .filter(created_at__gte=cutoff)
            .order_by("-created_at")[:5],
            "recent_count": Location.objects.filter(created_at__gte=cutoff).count(),
            "list_url": reverse("admin:inventory_location_changelist"),
            "change_url_name": "admin:inventory_location_change",
        },
        {
            "label": "Web logins",
            "total": AccessEvent.objects.filter(channel=AccessEvent.Channel.WEB).count(),
            "recent": AccessEvent.objects.select_related("user")
            .filter(channel=AccessEvent.Channel.WEB, created_at__gte=cutoff)
            .order_by("-created_at")[:5],
            "recent_count": AccessEvent.objects.filter(
                channel=AccessEvent.Channel.WEB, created_at__gte=cutoff
            ).count(),
            "list_url": f"{reverse('admin:inventory_accessevent_changelist')}?channel__exact=web",
            "change_url_name": "admin:inventory_accessevent_change",
        },
        {
            "label": "MCP logins",
            "total": AccessEvent.objects.filter(channel=AccessEvent.Channel.MCP).count(),
            "recent": AccessEvent.objects.select_related("user")
            .filter(channel=AccessEvent.Channel.MCP, created_at__gte=cutoff)
            .order_by("-created_at")[:5],
            "recent_count": AccessEvent.objects.filter(
                channel=AccessEvent.Channel.MCP, created_at__gte=cutoff
            ).count(),
            "list_url": f"{reverse('admin:inventory_accessevent_changelist')}?channel__exact=mcp",
            "change_url_name": "admin:inventory_accessevent_change",
        },
    )
    return {"cutoff": cutoff, "sections": sections}


@register.simple_tag
def admin_change_url(url_name, object_id):
    return reverse(url_name, args=[object_id])
