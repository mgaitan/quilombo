from django.contrib.admin.apps import AdminConfig


class QuilomboAdminConfig(AdminConfig):
    default_site = "inventory.admin_site.QuilomboAdminSite"
