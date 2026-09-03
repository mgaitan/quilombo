"""Populate a workspace with small, obviously synthetic data for staging.

Safe to run against an empty database on every deploy (``--ensure``) or to
rebuild the demo from scratch (``--refresh``). It never touches data outside the
``demo-*`` workspaces or the demo user.
"""

import os
from decimal import Decimal

from allauth.account.models import EmailAddress
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.db import transaction

from inventory.models import (
    Holding,
    Item,
    ItemLabel,
    Label,
    LabelAlias,
    Location,
    Membership,
    Workspace,
)

DEMO_USERNAME = "demo"
DEMO_EMAIL = "demo@example.com"
DEMO_WORKSPACE_SLUGS = ("demo-workshop", "demo-library")


class Command(BaseCommand):
    help = "Create synthetic demo data (user, workspaces, inventory) for staging."

    def add_arguments(self, parser):
        group = parser.add_mutually_exclusive_group()
        group.add_argument(
            "--ensure",
            action="store_true",
            help="Do nothing if the demo workspaces already exist.",
        )
        group.add_argument(
            "--refresh",
            action="store_true",
            help="Delete the existing demo data first, then recreate it.",
        )

    @transaction.atomic
    def handle(self, *args, **options):
        user = self._demo_user()
        exists = Workspace.objects.filter(slug__in=DEMO_WORKSPACE_SLUGS).exists()

        if exists and options["ensure"]:
            self.stdout.write("Demo data already present; nothing to do.")
            return
        if exists:
            for workspace in Workspace.objects.filter(slug__in=DEMO_WORKSPACE_SLUGS):
                self._delete_demo(workspace)

        self._build_workshop(user)
        self._build_library(user)
        self.stdout.write(
            self.style.SUCCESS(
                f"Seeded demo data. Sign in as '{DEMO_USERNAME}' / '{self._demo_password()}'."
            )
        )

    def _delete_demo(self, workspace):
        # Clear the FKs guarded by PROTECT before the workspace cascade.
        ItemLabel.objects.filter(workspace=workspace).delete()
        Label.objects.filter(workspace=workspace).delete()
        Location.objects.filter(workspace=workspace).update(parent=None)
        workspace.delete()

    def _demo_password(self):
        return os.environ.get("DEMO_USER_PASSWORD", "quilombo-demo")

    def _demo_user(self):
        user_model = get_user_model()
        user, _ = user_model.objects.get_or_create(
            username=DEMO_USERNAME, defaults={"email": DEMO_EMAIL}
        )
        user.email = DEMO_EMAIL
        user.set_password(self._demo_password())
        user.save()
        EmailAddress.objects.update_or_create(
            user=user,
            email=DEMO_EMAIL,
            defaults={"verified": True, "primary": True},
        )
        return user

    def _build_workshop(self, user):
        workspace = Workspace.objects.create(name="Demo Workshop", slug="demo-workshop")
        Membership.objects.create(workspace=workspace, user=user, role=Membership.Role.OWNER)
        garage = Location.objects.create(workspace=workspace, key="garage", name="Garage")
        cabinet = Location.objects.create(
            workspace=workspace, key="cabinet", name="Tool cabinet", parent=garage
        )
        drawer = Location.objects.create(
            workspace=workspace, key="drawer-1", name="Drawer 1", parent=cabinet
        )

        drill = Item.objects.create(
            workspace=workspace,
            key="cordless-drill",
            name="Cordless drill",
            category="power-tools",
            aliases=["taladro"],
            unit="unit",
            tracking_mode=Item.TrackingMode.DISCRETE,
        )
        screws = Item.objects.create(
            workspace=workspace,
            key="wood-screws-4x40",
            name="Wood screws 4x40",
            category="fasteners",
            unit="piece",
            minimum_quantity=Decimal("50"),
            target_quantity=Decimal("200"),
        )
        Holding.objects.create(
            workspace=workspace, item=drill, location=cabinet, quantity=Decimal("1")
        )
        Holding.objects.create(
            workspace=workspace,
            item=screws,
            location=drawer,
            quantity=Decimal("30"),
            approximate=True,
        )

        bosch = Label.objects.create(workspace=workspace, name="Bosch")
        LabelAlias.objects.create(workspace=workspace, label=bosch, value="herramientas Bosch")
        ItemLabel.objects.create(
            workspace=workspace,
            item=drill,
            label=bosch,
            original_value="Bosch",
            source=ItemLabel.Source.USER,
            created_by=user,
        )

    def _build_library(self, user):
        workspace = Workspace.objects.create(name="Demo Library", slug="demo-library")
        Membership.objects.create(workspace=workspace, user=user, role=Membership.Role.OWNER)
        room = Location.objects.create(workspace=workspace, key="reading-room", name="Reading room")
        shelf = Location.objects.create(
            workspace=workspace, key="shelf-a", name="Shelf A", parent=room
        )

        book = Item.objects.create(
            workspace=workspace,
            key="the-gray-angel",
            name="The Gray Angel Chronicles",
            category="book",
            attributes={
                "schema": "book",
                "book": {"authors": ["Alejandro Dolina"], "language": "es"},
            },
            unit="copy",
            tracking_mode=Item.TrackingMode.DISCRETE,
        )
        Holding.objects.create(
            workspace=workspace, item=book, location=shelf, quantity=Decimal("2")
        )
