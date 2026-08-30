# Labels and emerging facets

Quilombo separates a label's identity from the assertion that an item has that label. Structure can
grow as a workspace accumulates knowledge, without requiring an ontology before the evidence exists.

## Vocabulary

Canonicalization
: A deterministic transformation used to decide whether two inputs have the same label identity.
  It never makes a semantic claim.

Alias
: An explicit, workspace-scoped statement that a spelling or term redirects to a canonical label.
  Creating an alias requires a client or user to choose the canonical label.

Merge
: An explicit operation between two existing canonical labels. Unlike alias creation, a merge has
  to preserve both identities and be reversible. Merges are designed below but are not part of the
  first implementation.

Facet promotion
: A mapping from a label to a typed value such as `brand=Bosch`. Promotion is knowledge modeling,
  not normalization, and does not rewrite earlier label assertions.

The first implementation contains three workspace-owned models:

- `Label` stores a canonical display name and deterministic lookup keys.
- `LabelAlias` redirects an explicitly confirmed equivalent term to one `Label`.
- `ItemLabel` records that an item has a label, including the original input, its source, optional
  confidence, client provenance, and the authenticated actor.

Every relation is constrained to one workspace. Canonical names and aliases are shared only within
that workspace; Quilombo does not maintain a universal vocabulary across tenants.

## Deterministic identity

For identity matching, Quilombo applies Unicode NFKC normalization, collapses whitespace, and case
folds. The first observed value supplies a clean canonical display form, while `ItemLabel` preserves
the exact client input.

| Input difference | Identity behavior | Suggestion behavior |
| --- | --- | --- |
| `Bosch` / `BOSCH` | Same | Same |
| repeated or surrounding whitespace | Same | Same |
| compatibility forms such as full-width text | Same | Same |
| `electrica` / `eléctrica` | Different | Candidate match |
| `C` / `C++` | Different | Candidate match |
| `tool` / `tools` | Different | Candidate match |
| `ficción` / `fiction` | Different | No translation is inferred |

Accents and punctuation are folded only into a non-unique search key. Plurals and multilingual
equivalence are never inferred. This lets entry suggest a nearby term without silently collapsing
knowledge that may be different.

## Write and suggestion flow

`GET /api/workspaces/{workspace}/labels/?q=...` is read-only. It ranks an exact canonical identity,
an exact alias, accent/punctuation-folded matches, prefixes, and then other contained matches. It
returns at most 50 canonical candidates and their known aliases. Reading suggestions never creates
a label or alias.

`POST /api/workspaces/{workspace}/label-assertions/` accepts a bulk list with a required stable
`idempotency_key`:

```json
{
  "idempotency_key": "workshop-labels-2026-08-29",
  "provenance": {
    "source_kind": "agent",
    "source_reference": "conversation://inventory-42"
  },
  "assertions": [
    {
      "item_key": "orbital-sander",
      "value": "herramientas Bosch",
      "canonical_label_id": "7ba98a84-2a1e-4d8a-a9fe-86832099d46d",
      "source": "confirmation",
      "confidence": "1"
    }
  ]
}
```

Without `canonical_label_id`, an exact canonical or alias identity is reused; otherwise a new
canonical label is created. Supplying `canonical_label_id` is explicit confirmation that `value`
is equivalent to that label. If the normalized value differs, Quilombo creates an alias. A value
already owned by another canonical label returns a conflict and requires a future explicit merge.

The complete request runs in one database transaction. Items, canonical targets, labels, aliases,
assertions, and the inventory event are workspace-scoped. Repeating the same request returns the
original event without new writes; changing the payload under the same key returns a conflict. Any
invalid item or label rolls back the entire batch.

`source` describes the assertion itself: `user`, `agent`, `import`, or `confirmation`. Confidence is
optional and bounded from zero to one. Client provenance remains metadata: Quilombo neither fetches
nor processes a referenced photo, video, conversation, or import file.

## Workshop and library examples

In a workshop, `Bosch` and `bosch` deterministically share an identity. `herramientas Bosch` remains
separate until a user selects the existing `Bosch` suggestion; that confirmation creates an alias.
`acero` should not become a `material` facet merely because it appears frequently. Promotion is
justified when users need material-specific queries or validation across several item kinds.

In a library, a search for `ficcion` can suggest the canonical `Ficción` because accents are folded
for discovery. `Fiction` is a translation, so it becomes an alias only after explicit confirmation.
A recurring `primera edición` label may later justify an edition-related typed model if Quilombo
gains useful validation or behavior from it.

## Merge and facet design

A later merge operation should require source and target label IDs plus an idempotency key. It must
record a redirect rather than delete the source label or rewrite `ItemLabel` rows. Reads can project
the target identity while historical assertions keep the identity and original value they recorded.
Reversal removes the redirect and restores the two visible identities. Repeating the same merge or
reversal is a replay; attempting a different operation with the same key is a conflict.

Facet promotion should likewise add a workspace-scoped mapping such as `Bosch -> brand=Bosch`.
Existing labels, aliases, and assertions remain intact, and clients may query the projected typed
value before any optional materialization. Frequency alone is insufficient: promotion should add a
useful semantic dimension, explicit query behavior, validation, or constraints across item kinds.

The PoC deliberately does not implement canonical-label merge, merge reversal, facet tables, EAV,
automatic synonym discovery, stemming, translation, or ontology inference.
