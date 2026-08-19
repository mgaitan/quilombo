"""Generate the hosted Quilombo architecture diagram.

Run from the repository root with::

    uv run --no-project --python 3.14 --with diagrams python docs/architecture_diagram.py

The ``diagrams`` package is intentionally an authoring dependency only. The
application does not need it at runtime; Graphviz (``dot``) must also be
installed on the machine generating the image.
"""

from pathlib import Path

from diagrams import Cluster, Diagram, Edge
from diagrams.generic.blank import Blank
from diagrams.generic.network import Router
from diagrams.onprem.auth import Oauth2Proxy
from diagrams.onprem.client import User
from diagrams.onprem.database import PostgreSQL
from diagrams.programming.framework import Django

OUTPUT = Path(__file__).parent / "_static" / "quilombo-architecture"


def build_diagram() -> None:
    """Render the architecture to ``docs/_static/quilombo-architecture.png``."""

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)

    graph_attrs = {
        "bgcolor": "white",
        "fontname": "Helvetica",
        "nodesep": "0.7",
        "pad": "0.4",
        "ranksep": "1.0",
        "splines": "ortho",
    }
    node_attrs = {
        "fontname": "Helvetica",
        "fontsize": "11",
    }

    with Diagram(
        "Quilombo hosted architecture",
        filename=str(OUTPUT),
        outformat="png",
        show=False,
        direction="LR",
        graph_attr=graph_attrs,
        node_attr=node_attrs,
    ):
        agent = Blank("Agent\nChatGPT / Claude\nreasoning + vision")
        person = User("Person\nbrowser consent")

        with Cluster("Quilombo hosted service"):
            oauth = Oauth2Proxy("OAuth\nPKCE + consent")
            mcp = Router("MCP\nStreamable HTTP\n/mcp")
            server = Django("Django server\nstores facts")

        database = PostgreSQL("Neon\nPostgreSQL")

        agent >> Edge(label="authorize", style="dashed", color="darkgreen") >> oauth
        person >> Edge(label="consent", style="dotted") >> oauth
        oauth >> Edge(label="access token", style="dashed", color="darkgreen") >> agent
        agent >> Edge(label="tool calls") >> mcp
        mcp >> Edge(label="validated request") >> server
        oauth >> Edge(label="workspace-scoped grant", style="dotted") >> server
        server >> Edge(label="ORM + transactions") >> database


if __name__ == "__main__":
    build_diagram()
