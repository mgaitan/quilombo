from django.contrib.auth.signals import user_logged_in
from django.dispatch import receiver

from .models import AccessEvent


@receiver(user_logged_in, dispatch_uid="inventory.record_web_login")
def record_web_login(sender, request, user, **kwargs):
    AccessEvent.objects.create(user=user, channel=AccessEvent.Channel.WEB)
