from django.contrib import admin

from .models import (
    Holding,
    InventoryEvent,
    Item,
    Location,
    LocationRelation,
    Membership,
    Workspace,
)

admin.site.register(
    [Workspace, Membership, Location, LocationRelation, Item, Holding, InventoryEvent]
)
