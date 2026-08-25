# Quilombo

Quilombo is agent-first storage for physical inventories: workshops, libraries, tool rooms, and
anywhere people need to put things away and find them again.

The storage layer is deliberately conservative. Agents and clients interpret observations, ask for
confirmation, and decide when a fact is uncertain. Quilombo stores locations, items, holdings,
spatial relations, provenance metadata, and workspace-scoped permissions.

```{mermaid}
flowchart LR
    A[Person] --> B[Agent or client]
    B -->|MCP or REST| C[Quilombo]
    C --> D[(Workspace inventory)]
    B -->|interprets photos,
    language, intent| B
```

```{toctree}
:maxdepth: 2
:caption: Learn Quilombo

getting_started.md
concepts.md
audits.md
```

```{toctree}
:maxdepth: 2
:caption: Integrate

mcp.md
import_export.md
plugin/index.md
plugin/evaluation.md
```

```{toctree}
:maxdepth: 1
:caption: Architecture

architecture.md
```

```{toctree}
:maxdepth: 2
:caption: Contribute

development.md
```

The generated REST schema is available from a running installation at `/api/docs/`.
