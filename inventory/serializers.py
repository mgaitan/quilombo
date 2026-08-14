from rest_framework import serializers

from .models import Holding, Item, Location, LocationRelation


class StringListField(serializers.ListField):
    child = serializers.CharField(max_length=200)


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
