from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Promote an existing user to Django staff and superuser."

    def add_arguments(self, parser):
        parser.add_argument("username")

    def handle(self, *args, **options):
        user_model = get_user_model()
        try:
            user = user_model.objects.get(username=options["username"])
        except user_model.DoesNotExist as error:
            raise CommandError(f"User '{options['username']}' does not exist.") from error
        if user.is_staff and user.is_superuser:
            self.stdout.write(f"User '{user.username}' is already an administrator.")
            return
        user.is_staff = True
        user.is_superuser = True
        user.save(update_fields=["is_staff", "is_superuser"])
        self.stdout.write(self.style.SUCCESS(f"Promoted '{user.username}' to administrator."))
