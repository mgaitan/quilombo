# MCP integration

The hosted Streamable HTTP endpoint is:

```text
https://quilombo-v1-mgaitan.onrender.com/mcp
```

OAuth-capable clients can discover the authorization endpoints and ask the user to authorize a
workspace. Clients that do not support OAuth can use a workspace bearer token in the `Authorization`
header.

## Tools

| Tool | Purpose |
| --- | --- |
| `find_inventory` | Find ranked holdings and their locations. |
| `get_inventory_snapshot` | Read locations, relations, and holdings together. |
| `bulk_upsert_inventory` | Transactionally create or replace related inventory facts. |
| `move_inventory` | Move a holding between locations. |

The mutation tools write immediately. Drafts, human confirmation, and interpretation of photos or
language belong to the client skill. Always provide a unique idempotency key and provenance for
mutations.

## ChatGPT and Claude

Use the web app's `/connect/` guide for current client-specific setup steps. Keep the MCP URL stable
when deploying new server versions; refresh the client's tool list if it caches action metadata.
The curated workflow is available as [`manage-quilombo-inventory`](https://github.com/mgaitan/quilombo/blob/main/skills/manage-quilombo-inventory/SKILL.md)
in the repository.
