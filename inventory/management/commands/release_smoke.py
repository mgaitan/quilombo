from django.core.management.base import BaseCommand
from django.db import connection

from inventory.models import (
    Holding,
    InventoryEvent,
    Item,
    Location,
    Membership,
    Workspace,
)


class Command(BaseCommand):
    help = "Read-only ORM smoke check to run after applying release migrations."

    def handle(self, *args, **options):
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()

        counts = {
            model.__name__: model.objects.count()
            for model in (Workspace, Membership, Location, Item, Holding, InventoryEvent)
        }

        # Exercise a representative select_related / related-manager read path.
        latest_event = (
            InventoryEvent.objects.select_related("workspace", "actor")
            .order_by("-created_at")
            .first()
        )
        newest_item = Item.objects.order_by("-created_at").first()
        if newest_item is not None:
            list(newest_item.holdings.select_related("location")[:5])

        summary = ", ".join(f"{name}={count}" for name, count in counts.items())
        self.stdout.write(self.style.SUCCESS(f"Release smoke OK: {summary}"))
        if latest_event is not None:
            self.stdout.write(
                f"Latest event: {latest_event.kind} @ {latest_event.created_at.isoformat()}"
            )
