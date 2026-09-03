import hashlib
import secrets
import uuid
from datetime import timedelta

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.db.models import Q
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from .labels import label_display_value, normalize_label_identity, normalize_label_search


class VerificationStatus(models.TextChoices):
    CONFIRMED = "confirmed", _("Confirmed")
    UNKNOWN = "unknown", _("Unknown")


class FreshnessMixin:
    @property
    def freshness_status(self) -> str:
        if self.verification_status != VerificationStatus.CONFIRMED:
            return "unknown"
        if not self.last_observed_at:
            return "never"
        cutoff = timezone.now() - timedelta(days=settings.INVENTORY_FRESHNESS_DAYS)
        return "stale" if self.last_observed_at < cutoff else "current"

    def _invalidate_verification(self, *, fact_fields, update_fields):
        if not self.pk:
            return update_fields
        active_fields = fact_fields
        if update_fields is not None:
            active_fields = {
                field
                for field in fact_fields
                if field in update_fields or field.removesuffix("_id") in update_fields
            }
        if not active_fields:
            return update_fields
        observed_fields = ["verification_status", "last_observed_at", "last_observed_by_id"]
        previous = (
            type(self).objects.filter(pk=self.pk).values(*active_fields, *observed_fields).first()
        )
        if not previous or not any(
            previous[field] != getattr(self, field) for field in active_fields
        ):
            return update_fields
        verification_refreshed = any(
            previous[field] != getattr(self, field) for field in observed_fields
        )
        if verification_refreshed:
            return update_fields
        self.verification_status = VerificationStatus.UNKNOWN
        self.last_observed_at = None
        self.last_observed_by = None
        if update_fields is None:
            return None
        return set(update_fields) | {
            "verification_status",
            "last_observed_at",
            "last_observed_by",
        }


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
    can_write = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["workspace", "user"], name="unique_membership")
        ]

    def __str__(self):
        return f"{self.user} @ {self.workspace} ({self.role})"


class Location(FreshnessMixin, models.Model):
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
    verification_status = models.CharField(
        max_length=12, choices=VerificationStatus, default=VerificationStatus.UNKNOWN
    )
    last_observed_at = models.DateTimeField(null=True, blank=True)
    last_observed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="observed_locations",
    )
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

    def save(self, *args, **kwargs):
        kwargs["update_fields"] = self._invalidate_verification(
            fact_fields={
                "key",
                "name",
                "description",
                "kind",
                "parent_id",
                "aliases",
                "metadata",
            },
            update_fields=kwargs.get("update_fields"),
        )
        if kwargs["update_fields"] is None:
            kwargs.pop("update_fields")
        return super().save(*args, **kwargs)

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
        BULK = "bulk", _("Bulk quantity")
        DISCRETE = "discrete", _("Discrete count")

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
    minimum_quantity = models.DecimalField(max_digits=20, decimal_places=6, null=True, blank=True)
    target_quantity = models.DecimalField(max_digits=20, decimal_places=6, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(fields=["workspace", "key"], name="unique_item_key"),
            models.CheckConstraint(
                condition=Q(minimum_quantity__isnull=True) | Q(minimum_quantity__gte=0),
                name="item_minimum_nonnegative",
            ),
            models.CheckConstraint(
                condition=Q(target_quantity__isnull=True) | Q(target_quantity__gte=0),
                name="item_target_nonnegative",
            ),
            models.CheckConstraint(
                condition=(
                    Q(minimum_quantity__isnull=True)
                    | Q(target_quantity__isnull=True)
                    | Q(target_quantity__gte=models.F("minimum_quantity"))
                ),
                name="item_target_reaches_minimum",
            ),
        ]

    def clean(self):
        super().clean()
        if self.minimum_quantity is not None and self.minimum_quantity < 0:
            raise ValidationError({"minimum_quantity": "Minimum quantity cannot be negative."})
        if self.target_quantity is not None and self.target_quantity < 0:
            raise ValidationError({"target_quantity": "Target quantity cannot be negative."})
        if (
            self.minimum_quantity is not None
            and self.target_quantity is not None
            and self.target_quantity < self.minimum_quantity
        ):
            raise ValidationError({"target_quantity": "Target quantity must reach the minimum."})

    def __str__(self):
        return f"{self.name} [{self.key}]"


class Label(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE, related_name="labels")
    name = models.CharField(max_length=200)
    normalized_key = models.CharField(max_length=400, editable=False)
    search_key = models.CharField(max_length=400, editable=False, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["workspace", "normalized_key"], name="unique_workspace_label_key"
            )
        ]

    def clean(self):
        super().clean()
        display_name = label_display_value(self.name)
        if not normalize_label_identity(display_name):
            raise ValidationError({"name": "A label cannot be empty."})
        if len(display_name) > 200:
            raise ValidationError({"name": "A normalized label cannot exceed 200 characters."})

    def save(self, *args, **kwargs):
        self.name = label_display_value(self.name)
        self.normalized_key = normalize_label_identity(self.name)
        self.search_key = normalize_label_search(self.name)
        if update_fields := kwargs.get("update_fields"):
            if "name" in update_fields:
                kwargs["update_fields"] = set(update_fields) | {"normalized_key", "search_key"}
        return super().save(*args, **kwargs)

    def __str__(self):
        return self.name


class LabelAlias(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE, related_name="label_aliases")
    label = models.ForeignKey(Label, on_delete=models.CASCADE, related_name="aliases")
    value = models.CharField(max_length=200)
    normalized_key = models.CharField(max_length=400, editable=False)
    search_key = models.CharField(max_length=400, editable=False, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["value", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["workspace", "normalized_key"],
                name="unique_workspace_label_alias_key",
            )
        ]

    def clean(self):
        super().clean()
        if self.label_id and self.label.workspace_id != self.workspace_id:
            raise ValidationError({"label": "Label belongs to another workspace."})
        display_value = label_display_value(self.value)
        if not normalize_label_identity(display_value):
            raise ValidationError({"value": "A label alias cannot be empty."})
        if len(display_value) > 200:
            raise ValidationError({"value": "A normalized alias cannot exceed 200 characters."})

    def save(self, *args, **kwargs):
        self.value = label_display_value(self.value)
        self.normalized_key = normalize_label_identity(self.value)
        self.search_key = normalize_label_search(self.value)
        if update_fields := kwargs.get("update_fields"):
            if "value" in update_fields:
                kwargs["update_fields"] = set(update_fields) | {"normalized_key", "search_key"}
        return super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.value} -> {self.label.name}"


class ItemLabel(models.Model):
    class Source(models.TextChoices):
        USER = "user", _("User")
        AGENT = "agent", _("Agent")
        IMPORT = "import", _("Import")
        CONFIRMATION = "confirmation", _("Confirmation")

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE, related_name="item_labels")
    item = models.ForeignKey(Item, on_delete=models.CASCADE, related_name="label_assertions")
    label = models.ForeignKey(Label, on_delete=models.PROTECT, related_name="item_assertions")
    original_value = models.CharField(max_length=200)
    source = models.CharField(max_length=16, choices=Source)
    confidence = models.DecimalField(
        max_digits=4,
        decimal_places=3,
        null=True,
        blank=True,
        validators=[MinValueValidator(0), MaxValueValidator(1)],
    )
    source_reference = models.TextField(blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="item_label_assertions",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "id"]
        constraints = [
            models.CheckConstraint(
                condition=Q(confidence__isnull=True)
                | (Q(confidence__gte=0) & Q(confidence__lte=1)),
                name="item_label_confidence_range",
            )
        ]

    def clean(self):
        super().clean()
        for field in ("item", "label"):
            if not getattr(self, f"{field}_id"):
                continue
            if getattr(self, field).workspace_id != self.workspace_id:
                raise ValidationError({field: "Object belongs to another workspace."})

    def __str__(self):
        return f"{self.item.key}: {self.original_value} -> {self.label.name}"


class Holding(FreshnessMixin, models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE, related_name="holdings")
    item = models.ForeignKey(Item, on_delete=models.CASCADE, related_name="holdings")
    location = models.ForeignKey(Location, on_delete=models.CASCADE, related_name="holdings")
    quantity = models.DecimalField(max_digits=20, decimal_places=6)
    approximate = models.BooleanField(default=False)
    notes = models.TextField(blank=True)
    verification_status = models.CharField(
        max_length=12, choices=VerificationStatus, default=VerificationStatus.UNKNOWN
    )
    last_observed_at = models.DateTimeField(null=True, blank=True)
    last_observed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="observed_holdings",
    )
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
            if not getattr(self, f"{field}_id"):
                continue
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

    def save(self, *args, **kwargs):
        kwargs["update_fields"] = self._invalidate_verification(
            fact_fields={"item_id", "location_id", "quantity", "approximate", "notes"},
            update_fields=kwargs.get("update_fields"),
        )
        if kwargs["update_fields"] is None:
            kwargs.pop("update_fields")
        return super().save(*args, **kwargs)

    def __str__(self):
        approximate = "~" if self.approximate else ""
        return f"{self.item.name} @ {self.location.name}: {approximate}{self.quantity}"


class InventoryEvent(models.Model):
    class Kind(models.TextChoices):
        BULK_UPSERT = "bulk_upsert", _("Bulk upsert")
        ADJUSTMENT = "adjustment", _("Adjustment")
        MOVE = "move", _("Move")
        ITEM_UPDATE = "item_update", _("Item update")
        ITEM_DELETE = "item_delete", _("Item delete")
        LABEL_ASSERTION = "label_assertion", _("Label assertion")
        AUDIT = "audit", _("Audit")
        UNDO = "undo", _("Undo")

    class SourceKind(models.TextChoices):
        MANUAL = "manual", _("Manual")
        PHOTO = "photo", _("Photo")
        VIDEO = "video", _("Video")
        IMPORT = "import", _("Import")
        AGENT = "agent", _("Agent")
        OTHER = "other", _("Other")

    class Activity(models.TextChoices):
        """Why a stock fact was recorded, distinct from the technical kind and the
        evidence source_kind."""

        UNSPECIFIED = "unspecified", _("Unspecified")
        OBSERVATION = "observation", _("Observation")
        PURCHASE = "purchase", _("Purchase")

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
    activity = models.CharField(max_length=16, choices=Activity, default=Activity.UNSPECIFIED)
    source_reference = models.TextField(blank=True)
    observed_at = models.DateTimeField(null=True, blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    summary = models.JSONField(default=dict, blank=True)
    undo_data = models.JSONField(default=dict, blank=True)
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


class AccessEvent(models.Model):
    class Channel(models.TextChoices):
        WEB = "web", "Web"
        MCP = "mcp", "MCP"

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="access_events"
    )
    channel = models.CharField(max_length=8, choices=Channel)
    client_name = models.CharField(max_length=200, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        label = f"{self.get_channel_display()} · {self.user or 'unknown user'}"
        if self.client_name:
            return f"{label} · {self.client_name}"
        return label


class ApiToken(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE, related_name="api_tokens")
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="api_tokens"
    )
    name = models.CharField(max_length=120)
    prefix = models.CharField(max_length=12, unique=True)
    token_hash = models.CharField(max_length=64)
    can_write = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    revoked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    @classmethod
    def issue(cls, *, workspace, user, name, can_write=True):
        prefix = secrets.token_hex(6)
        secret = secrets.token_urlsafe(32)
        raw_token = f"qlo_{prefix}_{secret}"
        token = cls.objects.create(
            workspace=workspace,
            user=user,
            name=name,
            can_write=can_write,
            prefix=prefix,
            token_hash=hashlib.sha256(raw_token.encode()).hexdigest(),
        )
        return token, raw_token

    def matches(self, raw_token):
        candidate = hashlib.sha256(raw_token.encode()).hexdigest()
        return secrets.compare_digest(candidate, self.token_hash)

    def __str__(self):
        return f"{self.name} ({self.prefix})"


def _new_link_secret():
    return secrets.token_urlsafe(32)


class PublicSearchLink(models.Model):
    """A revocable, read-only search URL scoped to one location and optional category.

    ``secret`` is an unguessable capability token that appears in the shareable
    URL and, later, in a QR code, so it is stored as-is rather than hashed. It is
    only ever returned to a workspace member. Rotating or revoking the link
    invalidates the previous URL (and any QR that encodes it).
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workspace = models.ForeignKey(
        Workspace, on_delete=models.CASCADE, related_name="public_search_links"
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="public_search_links",
    )
    name = models.CharField(max_length=120)
    secret = models.CharField(max_length=64, unique=True, default=_new_link_secret)
    location = models.ForeignKey(
        Location, on_delete=models.CASCADE, related_name="public_search_links"
    )
    include_descendants = models.BooleanField(default=True)
    category = models.CharField(max_length=120, blank=True)
    expires_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    last_used_at = models.DateTimeField(null=True, blank=True)
    use_count = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.name} ({str(self.id)[:8]})"

    @classmethod
    def issue(
        cls,
        *,
        workspace,
        location,
        name,
        created_by=None,
        category="",
        include_descendants=True,
        expires_at=None,
    ):
        link = cls.objects.create(
            workspace=workspace,
            location=location,
            name=name,
            created_by=created_by,
            category=category,
            include_descendants=include_descendants,
            expires_at=expires_at,
            secret=_new_link_secret(),
        )
        return link, link.secret

    def rotate_secret(self):
        self.secret = _new_link_secret()
        self.save(update_fields=["secret"])
        return self.secret

    def revoke(self):
        if self.revoked_at is None:
            self.revoked_at = timezone.now()
            self.save(update_fields=["revoked_at"])

    @property
    def is_active(self):
        if self.revoked_at is not None:
            return False
        if self.expires_at is not None and self.expires_at <= timezone.now():
            return False
        return True


class OAuthClient(models.Model):
    client_id = models.CharField(primary_key=True, max_length=128)
    metadata = models.JSONField()
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.metadata.get("client_name") or self.client_id


class OAuthAuthorizationRequest(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    client = models.ForeignKey(OAuthClient, on_delete=models.CASCADE)
    state = models.TextField(blank=True)
    scopes = models.JSONField(default=list)
    code_challenge = models.CharField(max_length=160)
    redirect_uri = models.URLField(max_length=1000)
    redirect_uri_provided_explicitly = models.BooleanField(default=True)
    resource = models.URLField(max_length=1000, blank=True)
    expires_at = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)


class OAuthAuthorizationGrant(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    code_prefix = models.CharField(max_length=12, unique=True)
    code_hash = models.CharField(max_length=64)
    client = models.ForeignKey(OAuthClient, on_delete=models.CASCADE)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE)
    can_write = models.BooleanField(default=True)
    scopes = models.JSONField(default=list)
    code_challenge = models.CharField(max_length=160)
    redirect_uri = models.URLField(max_length=1000)
    redirect_uri_provided_explicitly = models.BooleanField(default=True)
    resource = models.URLField(max_length=1000, blank=True)
    expires_at = models.DateTimeField()
    used_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def matches(self, raw_code):
        candidate = hashlib.sha256(raw_code.encode()).hexdigest()
        return secrets.compare_digest(candidate, self.code_hash)


class OAuthCredential(models.Model):
    class Kind(models.TextChoices):
        ACCESS = "access", "Access token"
        REFRESH = "refresh", "Refresh token"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    kind = models.CharField(max_length=12, choices=Kind)
    prefix = models.CharField(max_length=12, unique=True)
    token_hash = models.CharField(max_length=64)
    client = models.ForeignKey(OAuthClient, on_delete=models.CASCADE)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE)
    can_write = models.BooleanField(default=True)
    family_id = models.UUIDField(default=uuid.uuid4)
    scopes = models.JSONField(default=list)
    resource = models.URLField(max_length=1000, blank=True)
    expires_at = models.DateTimeField()
    revoked_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [models.Index(fields=["kind", "prefix"])]

    @classmethod
    def issue(
        cls,
        *,
        kind,
        client,
        user,
        workspace,
        family_id,
        scopes,
        resource,
        expires_at,
        can_write=True,
    ):
        prefix = secrets.token_hex(6)
        secret = secrets.token_urlsafe(32)
        raw_token = f"qlo_oauth_{prefix}_{secret}"
        credential = cls.objects.create(
            kind=kind,
            prefix=prefix,
            token_hash=hashlib.sha256(raw_token.encode()).hexdigest(),
            client=client,
            user=user,
            workspace=workspace,
            can_write=can_write,
            family_id=family_id,
            scopes=scopes,
            resource=resource or "",
            expires_at=expires_at,
        )
        return credential, raw_token

    def matches(self, raw_token):
        candidate = hashlib.sha256(raw_token.encode()).hexdigest()
        return secrets.compare_digest(candidate, self.token_hash)
