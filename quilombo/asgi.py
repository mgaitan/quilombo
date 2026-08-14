import os
from contextlib import asynccontextmanager

from django.core.asgi import get_asgi_application
from starlette.applications import Starlette
from starlette.routing import Mount

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "quilombo.settings")

django_application = get_asgi_application()

from django.conf import settings  # noqa: E402
from mcp.server.transport_security import TransportSecuritySettings  # noqa: E402

from inventory.mcp import server as mcp_server  # noqa: E402

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


@asynccontextmanager
async def lifespan(app):
    async with mcp_application.router.lifespan_context(mcp_application):
        yield


application = Starlette(
    debug=settings.DEBUG,
    routes=[*mcp_application.routes, Mount("/", app=django_application)],
    lifespan=lifespan,
)
