import uuid

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q


class Workspace(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=120)
    slug = models.SlugField(max_length=80, unique=True)
    members = models.ManyToManyField(
        settings.AUTH_USER_MODEL, through="Membership", related_name="workspaces"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class Membership(models.Model):
    class Role(models.TextChoices):
        OWNER = "owner", "Owner"
        ADMIN = "admin", "Admin"
        MEMBER = "member", "Member"

    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE, related_name="memberships")
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="memberships"
    )
    role = models.CharField(max_length=12, choices=Role, default=Role.MEMBER)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["workspace", "user"], name="unique_membership")
        ]

    def __str__(self):
        return f"{self.user} @ {self.workspace} ({self.role})"


class Location(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE, related_name="locations")
    key = models.CharField(max_length=128)
    name = models.CharField(max_length=160)
    description = models.TextField(blank=True)
    kind = models.CharField(max_length=64, blank=True)
    parent = models.ForeignKey(
        "self",
        null=True,
        blank=True,
        related_name="children",
        on_delete=models.PROTECT,
    )
    aliases = models.JSONField(default=list, blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(fields=["workspace", "key"], name="unique_location_key")
        ]

    def clean(self):
        super().clean()
        if not self.parent_id:
            return
        if self.parent_id == self.id:
            raise ValidationError({"parent": "A location cannot contain itself."})
        if self.parent.workspace_id != self.workspace_id:
            raise ValidationError({"parent": "Parent location belongs to another workspace."})

        ancestor = self.parent
        seen = {self.id}
        while ancestor:
            if ancestor.id in seen:
                raise ValidationError({"parent": "Location hierarchy cannot contain cycles."})
            seen.add(ancestor.id)
            ancestor = ancestor.parent

    def __str__(self):
        return f"{self.name} [{self.key}]"


class LocationRelation(models.Model):
    class Relation(models.TextChoices):
        LEFT_OF = "left_of", "Left of"
        RIGHT_OF = "right_of", "Right of"
        ABOVE = "above", "Above"
        BELOW = "below", "Below"
        NEAR = "near", "Near"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workspace = models.ForeignKey(
        Workspace, on_delete=models.CASCADE, related_name="location_relations"
    )
    subject = models.ForeignKey(
        Location, on_delete=models.CASCADE, related_name="outgoing_relations"
    )
    relation = models.CharField(max_length=16, choices=Relation)
    object = models.ForeignKey(
        Location, on_delete=models.CASCADE, related_name="incoming_relations"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["subject__name", "relation", "object__name"]
        constraints = [
            models.UniqueConstraint(
                fields=["workspace", "subject", "relation", "object"],
                name="unique_location_relation",
            ),
            models.CheckConstraint(
                condition=~Q(subject=models.F("object")), name="relation_locations_differ"
            ),
        ]

    def clean(self):
        super().clean()
        for field in ("subject", "object"):
            location = getattr(self, field)
            if location.workspace_id != self.workspace_id:
                raise ValidationError({field: "Location belongs to another workspace."})

    def __str__(self):
        return f"{self.subject.key} {self.relation} {self.object.key}"


class Item(models.Model):
    class TrackingMode(models.TextChoices):
        BULK = "bulk", "Bulk quantity"
        DISCRETE = "discrete", "Discrete count"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE, related_name="items")
    key = models.CharField(max_length=128)
    name = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    category = models.CharField(max_length=120, blank=True)
    aliases = models.JSONField(default=list, blank=True)
    attributes = models.JSONField(default=dict, blank=True)
    tracking_mode = models.CharField(max_length=12, choices=TrackingMode, default=TrackingMode.BULK)
    unit = models.CharField(max_length=32, default="unit")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]
        constraints = [models.UniqueConstraint(fields=["workspace", "key"], name="unique_item_key")]

    def __str__(self):
        return f"{self.name} [{self.key}]"


class Holding(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE, related_name="holdings")
    item = models.ForeignKey(Item, on_delete=models.CASCADE, related_name="holdings")
    location = models.ForeignKey(Location, on_delete=models.CASCADE, related_name="holdings")
    quantity = models.DecimalField(max_digits=20, decimal_places=6)
    approximate = models.BooleanField(default=False)
    notes = models.TextField(blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["item__name", "location__name"]
        constraints = [
            models.UniqueConstraint(
                fields=["workspace", "item", "location"], name="unique_holding"
            ),
            models.CheckConstraint(condition=Q(quantity__gte=0), name="holding_nonnegative"),
        ]

    def clean(self):
        super().clean()
        for field in ("item", "location"):
            related = getattr(self, field)
            if related.workspace_id != self.workspace_id:
                raise ValidationError({field: "Object belongs to another workspace."})
        if self.quantity is not None and self.quantity < 0:
            raise ValidationError({"quantity": "Quantity cannot be negative."})
        if (
            self.quantity is not None
            and self.item.tracking_mode == Item.TrackingMode.DISCRETE
            and self.quantity != self.quantity.to_integral_value()
        ):
            raise ValidationError({"quantity": "Discrete items require a whole quantity."})

    def __str__(self):
        approximate = "~" if self.approximate else ""
        return f"{self.item.name} @ {self.location.name}: {approximate}{self.quantity}"


class InventoryEvent(models.Model):
    class Kind(models.TextChoices):
        BULK_UPSERT = "bulk_upsert", "Bulk upsert"
        ADJUSTMENT = "adjustment", "Adjustment"
        MOVE = "move", "Move"

    class SourceKind(models.TextChoices):
        MANUAL = "manual", "Manual"
        PHOTO = "photo", "Photo"
        VIDEO = "video", "Video"
        IMPORT = "import", "Import"
        AGENT = "agent", "Agent"
        OTHER = "other", "Other"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workspace = models.ForeignKey(
        Workspace, on_delete=models.CASCADE, related_name="inventory_events"
    )
    kind = models.CharField(max_length=20, choices=Kind)
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="inventory_events",
    )
    client_actor = models.CharField(max_length=160, blank=True)
    idempotency_key = models.CharField(max_length=160, blank=True)
    request_hash = models.CharField(max_length=64, blank=True)
    source_kind = models.CharField(max_length=16, choices=SourceKind, default=SourceKind.MANUAL)
    source_reference = models.TextField(blank=True)
    observed_at = models.DateTimeField(null=True, blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    summary = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["workspace", "idempotency_key"],
                condition=~Q(idempotency_key=""),
                name="unique_event_idempotency_key",
            )
        ]

    def __str__(self):
        return f"{self.kind} @ {self.workspace} ({self.created_at})"
