from django.db import migrations


def create_home_workspaces(apps, schema_editor):
    User = apps.get_model("auth", "User")
    Workspace = apps.get_model("inventory", "Workspace")
    Membership = apps.get_model("inventory", "Membership")

    for user in User.objects.all().iterator():
        if Membership.objects.filter(user_id=user.pk).exists():
            continue

        base_slug = f"home-{str(user.pk)[:8]}"
        slug = base_slug
        suffix = 2
        while Workspace.objects.filter(slug=slug).exists():
            slug = f"{base_slug}-{suffix}"
            suffix += 1

        workspace = Workspace.objects.create(name="Home", slug=slug)
        Membership.objects.create(
            workspace_id=workspace.pk,
            user_id=user.pk,
            role="owner",
        )


class Migration(migrations.Migration):
    dependencies = [
        ("inventory", "0004_oauthclient_oauthauthorizationrequest_and_more"),
    ]

    operations = [migrations.RunPython(create_home_workspaces, migrations.RunPython.noop)]
