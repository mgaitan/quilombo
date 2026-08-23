from urllib.parse import urlsplit

from django.conf import settings
from django.http import HttpResponsePermanentRedirect


class CanonicalHostMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request_host = request.get_host().partition(":")[0].lower()
        canonical_host = urlsplit(settings.PUBLIC_BASE_URL).hostname
        if request_host in settings.LEGACY_PUBLIC_HOSTS and request_host != canonical_host:
            target = f"{settings.PUBLIC_BASE_URL}{request.get_full_path()}"
            return HttpResponsePermanentRedirect(target, preserve_request=True)
        return self.get_response(request)
