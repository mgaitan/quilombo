from importlib import import_module

from django.apps import AppConfig


class InventoryConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "inventory"

    def ready(self):
        import_module("inventory.schema")
        import_module("inventory.signals")
