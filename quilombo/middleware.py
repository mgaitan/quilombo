from urllib.parse import urlsplit

from django.conf import settings
from starlette.responses import RedirectResponse


class CanonicalHostASGIMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        headers = dict(scope["headers"])
        request_host = headers.get(b"host", b"").decode("latin-1").partition(":")[0].lower()
        canonical_host = urlsplit(settings.PUBLIC_BASE_URL).hostname
        if request_host in settings.LEGACY_PUBLIC_HOSTS and request_host != canonical_host:
            path = scope.get("raw_path", scope["path"].encode()).decode("latin-1")
            query = scope.get("query_string", b"").decode("latin-1")
            target = f"{settings.PUBLIC_BASE_URL}{path}"
            if query:
                target = f"{target}?{query}"
            return await RedirectResponse(target, status_code=308)(scope, receive, send)

        return await self.app(scope, receive, send)
