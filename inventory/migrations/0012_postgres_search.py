from django.db import migrations


def enable_postgres_search(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    with schema_editor.connection.cursor() as cursor:
        cursor.execute("CREATE EXTENSION IF NOT EXISTS unaccent")
        cursor.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
        cursor.execute(
            """
            CREATE INDEX IF NOT EXISTS inventory_item_search_gin
            ON inventory_item USING gin (
                (
                    to_tsvector(
                        'simple',
                        coalesce(key, '') || ' ' || coalesce(name, '') || ' ' ||
                        coalesce(description, '') || ' ' || coalesce(category, '')
                    )
                )
            )
            """
        )
        cursor.execute(
            """
            CREATE INDEX IF NOT EXISTS inventory_location_search_gin
            ON inventory_location USING gin (
                (
                    to_tsvector(
                        'simple',
                        coalesce(key, '') || ' ' || coalesce(name, '') || ' ' ||
                        coalesce(description, '') || ' ' || coalesce(kind, '')
                    )
                )
            )
            """
        )


class Migration(migrations.Migration):
    dependencies = [("inventory", "0011_accessevent")]

    operations = [migrations.RunPython(enable_postgres_search, migrations.RunPython.noop)]
