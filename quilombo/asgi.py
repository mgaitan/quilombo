import os

from django.core.asgi import get_asgi_application
from starlette.routing import Mount, Route

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "quilombo.settings")

django_application = get_asgi_application()

from django.conf import settings  # noqa: E402
from mcp.server.transport_security import TransportSecuritySettings  # noqa: E402

from inventory.mcp import server as mcp_server  # noqa: E402
from quilombo.middleware import CanonicalHostASGIMiddleware  # noqa: E402


def create_application():
    mcp_application = mcp_server.streamable_http_app(
        streamable_http_path="/mcp",
        stateless_http=True,
        json_response=True,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=settings.MCP_ALLOWED_HOSTS,
            allowed_origins=settings.MCP_ALLOWED_ORIGINS,
        ),
    )

    oauth_metadata_route = next(
        route
        for route in mcp_application.router.routes
        if getattr(route, "path", None) == "/.well-known/oauth-authorization-server"
    )
    mcp_application.router.routes.append(
        Route(
            "/.well-known/openid-configuration",
            endpoint=oauth_metadata_route.endpoint,
            methods=["GET", "OPTIONS"],
        )
    )
    mcp_application.router.routes.append(Mount("/", app=django_application))
    mcp_application.add_middleware(CanonicalHostASGIMiddleware)
    return mcp_application


application = create_application()
