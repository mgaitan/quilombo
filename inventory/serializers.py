from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from .models import ApiToken, Holding, InventoryEvent, Item, Location, LocationRelation, Workspace
from .services import normalize_aliases


class StringListField(serializers.ListField):
    child = serializers.CharField(max_length=200)

    def to_internal_value(self, data):
        return normalize_aliases(super().to_internal_value(data))


class LocationSerializer(serializers.ModelSerializer):
    aliases = StringListField(required=False)
    parent_key = serializers.CharField(source="parent.key", read_only=True)

    class Meta:
        model = Location
        fields = [
            "id",
            "key",
            "name",
            "description",
            "kind",
            "parent",
            "parent_key",
            "aliases",
            "metadata",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]

    def validate_parent(self, parent):
        workspace = self.context["view"].get_workspace()
        if parent and parent.workspace_id != workspace.id:
            raise serializers.ValidationError("Parent location belongs to another workspace.")
        ancestor = parent
        while ancestor:
            if self.instance and ancestor.id == self.instance.id:
                raise serializers.ValidationError("Location hierarchy cannot contain cycles.")
            ancestor = ancestor.parent
        return parent


class LocationRelationSerializer(serializers.ModelSerializer):
    class Meta:
        model = LocationRelation
        fields = ["id", "subject", "relation", "object", "created_at"]
        read_only_fields = ["id", "created_at"]

    def validate(self, attrs):
        workspace = self.context["view"].get_workspace()
        for field in ("subject", "object"):
            location = attrs.get(field, getattr(self.instance, field, None))
            if location and location.workspace_id != workspace.id:
                raise serializers.ValidationError({field: "Location belongs to another workspace."})
        if attrs.get("subject") == attrs.get("object"):
            raise serializers.ValidationError("Related locations must differ.")
        return attrs


class ItemSerializer(serializers.ModelSerializer):
    aliases = StringListField(required=False)

    class Meta:
        model = Item
        fields = [
            "id",
            "key",
            "name",
            "description",
            "category",
            "aliases",
            "attributes",
            "tracking_mode",
            "unit",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]


class HoldingSerializer(serializers.ModelSerializer):
    item_key = serializers.CharField(source="item.key", read_only=True)
    item_name = serializers.CharField(source="item.name", read_only=True)
    location_key = serializers.CharField(source="location.key", read_only=True)
    location_name = serializers.CharField(source="location.name", read_only=True)
    unit = serializers.CharField(source="item.unit", read_only=True)

    class Meta:
        model = Holding
        fields = [
            "id",
            "item",
            "item_key",
            "item_name",
            "location",
            "location_key",
            "location_name",
            "quantity",
            "unit",
            "approximate",
            "notes",
            "updated_at",
        ]
        read_only_fields = ["id", "updated_at"]

    def validate(self, attrs):
        workspace = self.context["view"].get_workspace()
        item = attrs.get("item", getattr(self.instance, "item", None))
        location = attrs.get("location", getattr(self.instance, "location", None))
        quantity = attrs.get("quantity", getattr(self.instance, "quantity", None))
        for field, value in (("item", item), ("location", location)):
            if value and value.workspace_id != workspace.id:
                raise serializers.ValidationError({field: "Object belongs to another workspace."})
        if quantity is not None and quantity < 0:
            raise serializers.ValidationError({"quantity": "Quantity cannot be negative."})
        if (
            quantity is not None
            and item
            and item.tracking_mode == Item.TrackingMode.DISCRETE
            and quantity != quantity.to_integral_value()
        ):
            raise serializers.ValidationError(
                {"quantity": "Discrete items require a whole quantity."}
            )
        return attrs


class BulkLocationSerializer(serializers.Serializer):
    key = serializers.CharField(max_length=128)
    name = serializers.CharField(max_length=160)
    description = serializers.CharField(required=False, allow_blank=True)
    kind = serializers.CharField(required=False, allow_blank=True, max_length=64)
    parent_key = serializers.CharField(
        required=False, allow_blank=True, allow_null=True, max_length=128
    )
    aliases = StringListField(required=False)
    metadata = serializers.JSONField(required=False)


class BulkItemSerializer(serializers.Serializer):
    key = serializers.CharField(max_length=128)
    name = serializers.CharField(max_length=200)
    description = serializers.CharField(required=False, allow_blank=True)
    category = serializers.CharField(required=False, allow_blank=True, max_length=120)
    aliases = StringListField(required=False)
    attributes = serializers.JSONField(required=False)
    tracking_mode = serializers.ChoiceField(required=False, choices=Item.TrackingMode)
    unit = serializers.CharField(required=False, max_length=32)


class BulkHoldingSerializer(serializers.Serializer):
    item_key = serializers.CharField(max_length=128)
    location_key = serializers.CharField(max_length=128)
    quantity = serializers.DecimalField(max_digits=20, decimal_places=6, min_value=0)
    approximate = serializers.BooleanField(required=False)
    notes = serializers.CharField(required=False, allow_blank=True)


class BulkLocationRelationSerializer(serializers.Serializer):
    subject_key = serializers.CharField(max_length=128)
    relation = serializers.ChoiceField(choices=LocationRelation.Relation)
    object_key = serializers.CharField(max_length=128)


class ProvenanceSerializer(serializers.Serializer):
    client_actor = serializers.CharField(required=False, allow_blank=True, max_length=160)
    source_kind = serializers.ChoiceField(required=False, choices=InventoryEvent.SourceKind)
    source_reference = serializers.CharField(required=False, allow_blank=True)
    observed_at = serializers.DateTimeField(required=False, allow_null=True)
    metadata = serializers.JSONField(required=False)


class BulkUpsertSerializer(serializers.Serializer):
    idempotency_key = serializers.CharField(max_length=160)
    provenance = ProvenanceSerializer(required=False)
    locations = BulkLocationSerializer(many=True, required=False, max_length=2000)
    items = BulkItemSerializer(many=True, required=False, max_length=2000)
    holdings = BulkHoldingSerializer(many=True, required=False, max_length=5000)
    location_relations = BulkLocationRelationSerializer(many=True, required=False, max_length=5000)

    def validate(self, attrs):
        collection_keys = {
            "locations": lambda row: row["key"],
            "items": lambda row: row["key"],
            "holdings": lambda row: (row["item_key"], row["location_key"]),
            "location_relations": lambda row: (
                row["subject_key"],
                row["relation"],
                row["object_key"],
            ),
        }
        if not any(attrs.get(name) for name in collection_keys):
            raise serializers.ValidationError("At least one collection must contain data.")
        for name, identity in collection_keys.items():
            values = [identity(row) for row in attrs.get(name, [])]
            if len(values) != len(set(values)):
                raise serializers.ValidationError({name: "Batch contains duplicate keys."})
        return attrs


class BulkUpsertResultSerializer(serializers.Serializer):
    event_id = serializers.UUIDField()
    replayed = serializers.BooleanField()
    processed = serializers.DictField(child=serializers.IntegerField())


class WorkspaceSerializer(serializers.ModelSerializer):
    role = serializers.SerializerMethodField()

    class Meta:
        model = Workspace
        fields = ["id", "name", "slug", "role", "created_at"]
        read_only_fields = ["id", "role", "created_at"]

    def get_role(self, workspace) -> str | None:
        request = self.context.get("request")
        if not request or not request.user.is_authenticated:
            return None
        membership = workspace.memberships.filter(user=request.user).first()
        return membership.role if membership else None


class ApiTokenSerializer(serializers.ModelSerializer):
    class Meta:
        model = ApiToken
        fields = ["id", "name", "prefix", "created_at", "revoked_at"]
        read_only_fields = fields


class ApiTokenCreateSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=120)


class ApiTokenIssuedSerializer(ApiTokenSerializer):
    token = serializers.CharField()

    class Meta(ApiTokenSerializer.Meta):
        fields = [*ApiTokenSerializer.Meta.fields, "token"]


class SearchQuerySerializer(serializers.Serializer):
    q = serializers.CharField(max_length=200)
    category = serializers.CharField(required=False, max_length=120)
    location = serializers.CharField(required=False, max_length=128)
    include_descendants = serializers.BooleanField(required=False, default=True)


class SearchHoldingSerializer(HoldingSerializer):
    item_description = serializers.CharField(source="item.description", read_only=True)
    item_aliases = serializers.ListField(source="item.aliases", read_only=True)
    search = serializers.SerializerMethodField()

    class Meta(HoldingSerializer.Meta):
        fields = [*HoldingSerializer.Meta.fields, "item_description", "item_aliases", "search"]

    @extend_schema_field(serializers.DictField())
    def get_search(self, holding):
        return getattr(holding, "_search_match", None)


class SearchResultSerializer(serializers.Serializer):
    query = serializers.CharField()
    count = serializers.IntegerField()
    results = SearchHoldingSerializer(many=True)
