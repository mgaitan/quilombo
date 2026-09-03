from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Send one controlled test event to Sentry to verify the integration."

    def add_arguments(self, parser):
        parser.add_argument(
            "--error",
            action="store_true",
            help="Capture a handled exception instead of an info message.",
        )

    def handle(self, *args, **options):
        import sentry_sdk

        if not sentry_sdk.get_client().is_active():
            raise CommandError("Sentry is not configured. Set SENTRY_DSN and retry.")

        if options["error"]:
            try:
                raise RuntimeError("Quilombo Sentry verification error")
            except RuntimeError as error:
                event_id = sentry_sdk.capture_exception(error)
        else:
            event_id = sentry_sdk.capture_message(
                "Quilombo Sentry verification event", level="info"
            )

        sentry_sdk.flush(timeout=5)
        self.stdout.write(self.style.SUCCESS(f"Delivered verification event {event_id}."))
