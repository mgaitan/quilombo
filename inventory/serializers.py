from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from .models import ApiToken, Holding, InventoryEvent, Item, Location, LocationRelation, Workspace
from .services import normalize_aliases


class StringListField(serializers.ListField):
    child = serializers.CharField(max_length=200)

    def to_internal_value(self, data):
        return normalize_aliases(super().to_internal_value(data))


class BookLookupResultSerializer(serializers.Serializer):
    provider = serializers.CharField()
    isbn = serializers.CharField()
    source_url = serializers.URLField()
    retrieved_at = serializers.DateTimeField()
    suggested_item = serializers.JSONField()


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
            "minimum_quantity",
            "target_quantity",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]

    def validate(self, attrs):
        minimum = attrs.get("minimum_quantity", getattr(self.instance, "minimum_quantity", None))
        target = attrs.get("target_quantity", getattr(self.instance, "target_quantity", None))
        if minimum is not None and minimum < 0:
            raise serializers.ValidationError(
                {"minimum_quantity": "Minimum quantity cannot be negative."}
            )
        if target is not None and target < 0:
            raise serializers.ValidationError(
                {"target_quantity": "Target quantity cannot be negative."}
            )
        if minimum is not None and target is not None and target < minimum:
            raise serializers.ValidationError(
                {"target_quantity": "Target quantity must reach the minimum."}
            )
        return attrs


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
    minimum_quantity = serializers.DecimalField(
        required=False, allow_null=True, max_digits=20, decimal_places=6, min_value=0
    )
    target_quantity = serializers.DecimalField(
        required=False, allow_null=True, max_digits=20, decimal_places=6, min_value=0
    )

    def validate(self, attrs):
        minimum = attrs.get("minimum_quantity")
        target = attrs.get("target_quantity")
        if minimum is not None and target is not None and target < minimum:
            raise serializers.ValidationError(
                {"target_quantity": "Target quantity must reach the minimum."}
            )
        return attrs


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

    def validate_metadata(self, value):
        if not isinstance(value, dict):
            raise serializers.ValidationError("Metadata must be a JSON object.")
        return value


class ItemRepairFieldsSerializer(serializers.Serializer):
    key = serializers.CharField(required=False, max_length=128)
    name = serializers.CharField(required=False, max_length=200)
    description = serializers.CharField(required=False, allow_blank=True)
    category = serializers.CharField(required=False, allow_blank=True, max_length=120)
    aliases = StringListField(required=False)
    attributes = serializers.JSONField(required=False)
    tracking_mode = serializers.ChoiceField(required=False, choices=Item.TrackingMode)
    unit = serializers.CharField(required=False, max_length=32)
    minimum_quantity = serializers.DecimalField(
        required=False, allow_null=True, max_digits=20, decimal_places=6, min_value=0
    )
    target_quantity = serializers.DecimalField(
        required=False, allow_null=True, max_digits=20, decimal_places=6, min_value=0
    )


class HoldingRepairSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    location_id = serializers.UUIDField(required=False)
    quantity = serializers.DecimalField(
        required=False, max_digits=20, decimal_places=6, min_value=0
    )
    approximate = serializers.BooleanField(required=False)
    notes = serializers.CharField(required=False, allow_blank=True)

    def validate(self, attrs):
        if len(attrs) == 1:
            raise serializers.ValidationError("At least one holding field must be supplied.")
        return attrs


class ItemRepairSerializer(serializers.Serializer):
    item_id = serializers.UUIDField()
    idempotency_key = serializers.CharField(max_length=160)
    provenance = ProvenanceSerializer(required=False)
    item = ItemRepairFieldsSerializer(required=False)
    holdings = HoldingRepairSerializer(many=True, required=False, max_length=5000)

    def validate(self, attrs):
        if not attrs.get("item") and not attrs.get("holdings"):
            raise serializers.ValidationError("Supply item fields or holdings to update.")
        holding_ids = [row["id"] for row in attrs.get("holdings", [])]
        if len(holding_ids) != len(set(holding_ids)):
            raise serializers.ValidationError({"holdings": "Batch contains duplicate IDs."})
        item = attrs.get("item", {})
        minimum = item.get("minimum_quantity")
        target = item.get("target_quantity")
        if minimum is not None and target is not None and target < minimum:
            raise serializers.ValidationError(
                {"item": {"target_quantity": "Target quantity must reach the minimum."}}
            )
        return attrs


class ItemDeleteSerializer(serializers.Serializer):
    item_id = serializers.UUIDField()
    idempotency_key = serializers.CharField(max_length=160)
    provenance = ProvenanceSerializer(required=False)


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


class TransferLocationSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    key = serializers.CharField(max_length=128)
    name = serializers.CharField(max_length=160)
    description = serializers.CharField(required=False, allow_blank=True, default="")
    kind = serializers.CharField(required=False, allow_blank=True, max_length=64, default="")
    parent_id = serializers.UUIDField(required=False, allow_null=True, default=None)
    aliases = StringListField(required=False, default=list)
    metadata = serializers.JSONField(required=False, default=dict)


class TransferItemSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    key = serializers.CharField(max_length=128)
    name = serializers.CharField(max_length=200)
    description = serializers.CharField(required=False, allow_blank=True, default="")
    category = serializers.CharField(required=False, allow_blank=True, max_length=120, default="")
    aliases = StringListField(required=False, default=list)
    attributes = serializers.JSONField(required=False, default=dict)
    tracking_mode = serializers.ChoiceField(choices=Item.TrackingMode)
    unit = serializers.CharField(max_length=32)
    minimum_quantity = serializers.DecimalField(allow_null=True, max_digits=20, decimal_places=6)
    target_quantity = serializers.DecimalField(allow_null=True, max_digits=20, decimal_places=6)

    def validate(self, attrs):
        minimum = attrs["minimum_quantity"]
        target = attrs["target_quantity"]
        if minimum is not None and minimum < 0:
            raise serializers.ValidationError({"minimum_quantity": "Must not be negative."})
        if target is not None and target < 0:
            raise serializers.ValidationError({"target_quantity": "Must not be negative."})
        if minimum is not None and target is not None and target < minimum:
            raise serializers.ValidationError(
                {"target_quantity": "Must reach the minimum quantity."}
            )
        return attrs


class TransferHoldingSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    item_id = serializers.UUIDField()
    location_id = serializers.UUIDField()
    quantity = serializers.DecimalField(max_digits=20, decimal_places=6, min_value=0)
    approximate = serializers.BooleanField(default=False)
    notes = serializers.CharField(required=False, allow_blank=True, default="")


class TransferLocationRelationSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    subject_id = serializers.UUIDField()
    relation = serializers.ChoiceField(choices=LocationRelation.Relation)
    object_id = serializers.UUIDField()


class InventoryDocumentSerializer(serializers.Serializer):
    format_version = serializers.ChoiceField(choices=["1.0"])
    workspace = serializers.JSONField(required=False, default=dict)
    exported_at = serializers.DateTimeField(required=False)
    locations = TransferLocationSerializer(many=True, default=list, max_length=10000)
    items = TransferItemSerializer(many=True, default=list, max_length=10000)
    holdings = TransferHoldingSerializer(many=True, default=list, max_length=50000)
    location_relations = TransferLocationRelationSerializer(
        many=True, default=list, max_length=50000
    )

    def validate(self, attrs):
        for collection in ("locations", "items", "holdings", "location_relations"):
            ids = [row["id"] for row in attrs[collection]]
            if len(ids) != len(set(ids)):
                raise serializers.ValidationError({collection: "Contains duplicate IDs."})
        for collection in ("locations", "items"):
            keys = [row["key"] for row in attrs[collection]]
            if len(keys) != len(set(keys)):
                raise serializers.ValidationError({collection: "Contains duplicate keys."})
        holding_keys = [(row["item_id"], row["location_id"]) for row in attrs["holdings"]]
        if len(holding_keys) != len(set(holding_keys)):
            raise serializers.ValidationError(
                {"holdings": "Contains duplicate item and location pairs."}
            )
        relation_keys = [
            (row["subject_id"], row["relation"], row["object_id"])
            for row in attrs["location_relations"]
        ]
        if len(relation_keys) != len(set(relation_keys)):
            raise serializers.ValidationError(
                {"location_relations": "Contains duplicate relations."}
            )

        location_ids = {row["id"] for row in attrs["locations"]}
        item_ids = {row["id"] for row in attrs["items"]}
        for row in attrs["locations"]:
            if row["parent_id"] and row["parent_id"] not in location_ids:
                raise serializers.ValidationError(
                    {"locations": f"Unknown parent ID '{row['parent_id']}'."}
                )
        for row in attrs["holdings"]:
            if row["item_id"] not in item_ids or row["location_id"] not in location_ids:
                raise serializers.ValidationError(
                    {"holdings": "Every item_id and location_id must exist in the document."}
                )
        for row in attrs["location_relations"]:
            if row["subject_id"] not in location_ids or row["object_id"] not in location_ids:
                raise serializers.ValidationError(
                    {"location_relations": "Every location ID must exist in the document."}
                )
            if row["subject_id"] == row["object_id"]:
                raise serializers.ValidationError(
                    {"location_relations": "Related locations must differ."}
                )

        parent_by_id = {row["id"]: row["parent_id"] for row in attrs["locations"]}
        for location_id in parent_by_id:
            current = location_id
            seen = set()
            while current:
                if current in seen:
                    raise serializers.ValidationError(
                        {"locations": f"Hierarchy contains a cycle at '{current}'."}
                    )
                seen.add(current)
                current = parent_by_id.get(current)

        item_by_id = {row["id"]: row for row in attrs["items"]}
        for row in attrs["holdings"]:
            item = item_by_id[row["item_id"]]
            if (
                item["tracking_mode"] == Item.TrackingMode.DISCRETE
                and row["quantity"] != row["quantity"].to_integral_value()
            ):
                raise serializers.ValidationError(
                    {"holdings": f"Discrete item '{item['key']}' requires a whole quantity."}
                )
        return attrs


class InventoryImportSerializer(serializers.Serializer):
    format = serializers.ChoiceField(choices=["json", "csv"])
    document = serializers.JSONField(required=False)
    content = serializers.CharField(required=False, allow_blank=False)
    file = serializers.FileField(required=False)
    dry_run = serializers.BooleanField(required=False, default=False)
    idempotency_key = serializers.CharField(max_length=160)
    provenance = ProvenanceSerializer(required=False)

    def validate(self, attrs):
        sources = [name for name in ("document", "content", "file") if name in attrs]
        if len(sources) != 1:
            raise serializers.ValidationError("Supply exactly one of document, content, or file.")
        if "document" in attrs and attrs["format"] != "json":
            raise serializers.ValidationError("document is available only for JSON imports.")
        return attrs


class InventoryImportResultSerializer(serializers.Serializer):
    event_id = serializers.UUIDField(allow_null=True)
    replayed = serializers.BooleanField()
    dry_run = serializers.BooleanField()
    summary = serializers.JSONField()


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
    page = serializers.IntegerField(required=False, min_value=1)
    page_size = serializers.IntegerField(required=False, min_value=1, max_value=200)


class SearchHoldingSerializer(HoldingSerializer):
    item_description = serializers.CharField(source="item.description", read_only=True)
    item_aliases = serializers.ListField(source="item.aliases", read_only=True)
    search = serializers.SerializerMethodField()

    class Meta(HoldingSerializer.Meta):
        fields = [*HoldingSerializer.Meta.fields, "item_description", "item_aliases", "search"]

    @extend_schema_field(serializers.DictField())
    def get_search(self, holding):
        return getattr(holding, "_search_match", None)


class HoldingClueSerializer(SearchHoldingSerializer):
    item_attributes = serializers.JSONField(source="item.attributes", read_only=True)
    location_path = serializers.SerializerMethodField()
    nearby_items = serializers.SerializerMethodField()

    class Meta(SearchHoldingSerializer.Meta):
        fields = [
            *SearchHoldingSerializer.Meta.fields,
            "item_attributes",
            "location_path",
            "nearby_items",
        ]

    @extend_schema_field(serializers.ListField(child=serializers.DictField()))
    def get_location_path(self, holding):
        return self.context.get("location_paths", {}).get(holding.location_id, [])

    @extend_schema_field(serializers.ListField(child=serializers.DictField()))
    def get_nearby_items(self, holding):
        return self.context.get("nearby_by_holding", {}).get(holding.id, [])


class PaginationMetadataSerializer(serializers.Serializer):
    count = serializers.IntegerField()
    page = serializers.IntegerField()
    page_size = serializers.IntegerField()
    total_pages = serializers.IntegerField()
    next = serializers.URLField(allow_null=True)
    previous = serializers.URLField(allow_null=True)


class SearchResultSerializer(serializers.Serializer):
    query = serializers.CharField()
    count = serializers.IntegerField()
    truncated = serializers.BooleanField()
    pagination = PaginationMetadataSerializer()
    results = HoldingClueSerializer(many=True)


class StockStatusItemSerializer(serializers.Serializer):
    item_key = serializers.CharField()
    item_name = serializers.CharField()
    status = serializers.ChoiceField(choices=["missing", "low"])
    current_quantity = serializers.DecimalField(max_digits=20, decimal_places=6)
    minimum_quantity = serializers.DecimalField(max_digits=20, decimal_places=6)
    target_quantity = serializers.DecimalField(max_digits=20, decimal_places=6)
    recommended_add_quantity = serializers.DecimalField(max_digits=20, decimal_places=6)
    unit = serializers.CharField()
    locations = serializers.JSONField()


class StockStatusResultSerializer(serializers.Serializer):
    workspace = serializers.CharField()
    count = serializers.IntegerField()
    items = StockStatusItemSerializer(many=True)
