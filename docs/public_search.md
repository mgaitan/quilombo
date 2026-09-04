# Public search links

A public search link is a revocable URL that lets anyone run a **read-only** inventory
search limited to one location (and, optionally, one category). Libraries and workshops
can share it with visitors without giving them an account or a workspace.

## Scope and safety

- The link is bound to one location. Results include that location and, by default, its
  descendants. An optional category narrows results further.
- Public requests are `GET` only. They cannot create, edit, move, delete, import, or
  export anything, and they never see other locations, tokens, membership, or holding
  notes.
- The URL carries an unguessable random secret. It is a capability URL: treat it like a
  password. It is returned only to workspace members and is never shown in the link list.
- A link can be given an expiry and can be revoked at any time. Rotating the secret
  invalidates the previous URL (and any QR that encodes it) immediately.
- Public search is rate limited (default `60/min` per client IP, configurable with
  `PUBLIC_SEARCH_THROTTLE_RATE`).
- Each successful request updates the link's `last_used_at` and `use_count` for a
  lightweight access log.

## Managing links (workspace members)

Signed-in members have a **Public links** page (`/app/{slug}/public-links/`) to create,
rotate, and revoke links and to view or download each link's QR. The full URL is shown once,
as a flash message, when a link is created or its secret is rotated.

| Method & path | Who | Purpose |
| --- | --- | --- |
| `GET /api/workspaces/{workspace}/public-search-links/` | any member | list links (no working URL) |
| `POST /api/workspaces/{workspace}/public-search-links/` | writers | create a link, returns the URL once |
| `GET /api/workspaces/{workspace}/public-search-links/{id}/` | any member | inspect one link |
| `DELETE /api/workspaces/{workspace}/public-search-links/{id}/` | writers | revoke the link |
| `POST /api/workspaces/{workspace}/public-search-links/{id}/rotate/` | writers | new secret, returns the new URL once |
| `GET /api/workspaces/{workspace}/public-search-links/{id}/qr/` | any member | QR code for the public URL |

Create request body:

```json
{
  "name": "Front desk catalog",
  "location_key": "reading-room",
  "include_descendants": true,
  "category": "book",
  "expires_at": "2027-01-01T00:00:00Z"
}
```

`category` and `expires_at` are optional. The response includes `url`, the full public
link. Save it: the list endpoint returns link metadata but never the URL or secret.

## QR codes

`GET /api/workspaces/{workspace}/public-search-links/{id}/qr/` renders a QR code that
encodes **only** the public URL — never inventory data, tokens, or session credentials.

- `?format=svg` (default) or `?format=png` for the bare code.
- `?label=true` returns a printable SVG that adds the link's scope name under the code.
- A revoked or expired link returns `404`; rotating the secret changes the URL, so any
  previously printed QR stops working.

## Using a link (public visitor)

```
GET /api/public/search/{secret}/?q=cordless%20drill&page=1&page_size=20
```

`q` is optional; an empty query lists everything in scope. Pagination is bounded
(`page_size` up to 50). A revoked, expired, or unknown secret returns `404`.

The response uses a dedicated read-only serializer:

```json
{
  "scope": "Front desk catalog",
  "query": "cordless drill",
  "count": 1,
  "truncated": false,
  "pagination": { "count": 1, "page": 1, "page_size": 20, "total_pages": 1, "next": null, "previous": null },
  "results": [
    {
      "item_name": "Cordless drill",
      "item_category": "power-tools",
      "item_description": "18V, two batteries",
      "item_aliases": ["taladro"],
      "item_attributes": {},
      "quantity": "1.000000",
      "approximate": false,
      "unit": "unit",
      "location_name": "Reading room",
      "location_path": [{ "key": "library", "name": "Library" }, { "key": "reading-room", "name": "Reading room" }],
      "search": { "score": 100.0, "matched_terms": ["cordless", "drill"], "match_type": "complete" }
    }
  ]
}
```
