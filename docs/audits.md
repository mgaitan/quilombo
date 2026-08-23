# Audits and freshness

An inventory fact can be correct when recorded and wrong months later. Quilombo tracks whether a
location or holding was confirmed, when it was observed, and which user authorized the audit. The
audit event keeps the client-supplied provenance without storing source photos or videos.

## Freshness states

Reads expose `verification_status`, `last_observed_at`, `last_observed_by`, and `freshness` for
locations and holdings. `freshness` has three useful values:

- `current`: confirmed within the configured window;
- `stale`: confirmed, but older than the configured window;
- `unknown`: never confirmed, explicitly marked unknown, or changed since confirmation.

The default window is 90 days. Deployments can change it with `INVENTORY_FRESHNESS_DAYS`. Changing
an item's quantity, location, approximation flag, or notes invalidates the holding's previous
verification. Changing a location's identity, description, metadata, or parent does the same for
the location. Delayed audits are rejected when their `observed_at` is older than the stored
observation.

## Re-verify while already there

Freshness is a prompt for selective maintenance, not a reason to question every search result. An
agent should suggest a related check only when all of these are true:

- the user is already opening or handling the exact location;
- another fact in that location is stale or unknown;
- checking it is quick and does not distract from the original task.

For example, suppose the user asks for screws stored in `drawer unit > drawer 1`. The same drawer
contains a white box whose nuts have not been verified for months. After answering where the
screws are, the agent can ask one short follow-up: “While drawer 1 is open, are the nuts still in
the white box?” If those nuts were inventoried recently, the agent should not ask. It should not
expand the check to other drawers or repeatedly ask about the same current facts.

A negative or uncertain answer is not proof that an item is absent. Mark the holding `unknown`
unless the user confirms a correction or deletion.

Search results include the freshness and stable holding ID of nearby items in the same location.
This lets a client make the selective suggestion without scanning unrelated locations. A scoped
`get_inventory_snapshot` remains available when a deliberate location-wide audit is requested.

## MCP audit

`audit_inventory` audits one known location and only the holdings explicitly listed. Omitted
holdings remain unchanged. Each holding needs its stable `holding_id` and a status of `confirmed`
or `unknown`; the same request may correct its quantity, approximation flag, or notes.

```json
{
  "location_key": "drawer-1",
  "location_status": "confirmed",
  "holdings": [
    {
      "holding_id": "2f0f08af-2d15-4ea1-8ef6-02e505b41db4",
      "status": "confirmed",
      "quantity": "24"
    }
  ],
  "idempotency_key": "audit-drawer-1-20260823",
  "provenance": {
    "client_actor": "inventory-agent/1.0",
    "source_kind": "manual",
    "source_reference": "Drawer 1 checked with the user",
    "observed_at": "2026-08-23T10:30:00-03:00"
  }
}
```

The operation is transactional and idempotent. It creates an immutable `audit` event. Reusing the
same idempotency key with a different payload is rejected.
