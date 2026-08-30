from copy import deepcopy

from django.db import migrations

BOOK_CATEGORIES = {"book", "books", "libro", "libros"}


def normalize_book_schema(apps, schema_editor):
    Item = apps.get_model("inventory", "Item")
    Holding = apps.get_model("inventory", "Holding")

    books = []
    fractional_books = []
    for item in Item.objects.all().iterator():
        attributes = item.attributes if isinstance(item.attributes, dict) else {}
        if (
            item.category.strip().casefold() not in BOOK_CATEGORIES
            and attributes.get("schema") != "book"
        ):
            continue
        books.append(item)
        if any(
            quantity != quantity.to_integral_value()
            for quantity in Holding.objects.filter(item_id=item.pk).values_list(
                "quantity", flat=True
            )
        ):
            fractional_books.append(item.key)

    if fractional_books:
        keys = ", ".join(sorted(fractional_books))
        raise RuntimeError(
            "Cannot normalize book tracking with fractional holdings; "
            f"correct these items first: {keys}"
        )

    for item in books:
        if isinstance(item.attributes, dict):
            attributes = deepcopy(item.attributes)
        else:
            attributes = {"legacy_attributes": deepcopy(item.attributes)}
        changed = attributes.get("schema") != "book"
        attributes["schema"] = "book"

        if item.tracking_mode != "discrete":
            item.tracking_mode = "discrete"
            changed = True
        if item.unit != "copy":
            item.unit = "copy"
            changed = True
        if not changed:
            continue

        item.attributes = attributes
        item.save(update_fields=["attributes", "tracking_mode", "unit", "updated_at"])


class Migration(migrations.Migration):
    dependencies = [("inventory", "0012_postgres_search")]

    operations = [
        migrations.RunPython(normalize_book_schema, migrations.RunPython.noop),
    ]
