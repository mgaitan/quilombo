# Agent Plugin

The repository root is a portable [Agent Plugins 1.0](https://agent-plugins.org/) package and an
OpenAI plugin package. Both formats discover the existing `skills/` directory, so the curated
inventory workflow has one source of truth.

## Package layout

- `plugin.json` identifies the portable package.
- `mcp.json` declares the hosted Streamable HTTP endpoint. OAuth discovery and credentials remain
  client-managed and are never stored in the package.
- `.codex-plugin/plugin.json` provides the OpenAI listing and component metadata.
- `.app.json` maps the package to the Quilombo MCP connection registered in OpenAI developer mode.
- `skills/manage-quilombo-inventory/` supplies the inventory workflow and safety policy.

Run both schema and package validation before changing the package:

```bash
uv run python scripts/validate_plugin_package.py
```

The repository-owned validator checks both published Agent Plugins schemas and the OpenAI package
paths. During development, the package should also pass the `plugin-creator` validator available
inside ChatGPT and Codex.

## Versioning and ownership

The plugin version follows the Quilombo application version in `pyproject.toml`. A plugin metadata,
skill, or MCP contract change is released with the application using the normal GitHub Release
workflow. Martin Gaitan owns the public listing, OpenAI developer identity, domain verification,
and publication from the submission portal.

The portable package can be installed by compatible clients directly from this repository. The
OpenAI package can be loaded from the repository root for local testing; public ChatGPT and Codex
availability begins only after OpenAI approves and the owner publishes the submission.
