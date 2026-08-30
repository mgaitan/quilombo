# Concepts

## Workspaces

A workspace is an isolated inventory. Every location, item, holding, token, and event belongs to
exactly one workspace. A user can have several workspaces, such as `Home`, `Workshop`, or
`Library`; queries never cross that boundary.

## Locations

Locations form a tree and can describe anything from a room to a drawer compartment:

```text
Library
└── Shelf 2
    └── Left section
```

Relative relations such as `left_of`, `above`, `below`, and `near` add spatial clues when a rigid
coordinate system is not available. A search can be limited to a location and, by default, its
descendants.

## Items and holdings

An `Item` is the thing being tracked. `name` is the human-facing name or title; `category`,
`aliases`, `description`, and `attributes` provide additional vocabulary and structured detail.
A `Holding` says where an item is and how much is there. The same item can have holdings in
multiple locations.

For a book, a useful record might look like:

```json
{
  "name": "The Gray Angel Chronicles",
  "category": "book",
  "attributes": {
    "schema": "book",
    "book": {
      "title": "The Gray Angel Chronicles",
      "authors": ["Alejandro Dolina"],
      "publishers": []
    }
  },
  "description": "Blue edition, medium height"
}
```

Quilombo stores these user-provided facts; the client or agent decides whether an external catalog
should be consulted. The title is the minimum useful input for a later Open Library search; known
authors and publishers improve disambiguation.

## Search

Search is deterministic and workspace-scoped. It normalizes accents and punctuation, ranks matches,
and reports which terms and fields matched. Short technical codes are exact tokens, so `AA` does
not match `AAA`. The API does not invent semantic synonyms; clients can add known aliases or issue
separate translated searches.

## Labels

Labels add open vocabulary without conflating canonical identity with an assertion about an item.
Deterministic normalization handles case, Unicode compatibility forms, and whitespace; aliases and
multilingual equivalence require explicit confirmation. See [Labels and emerging facets](labels.md)
for the model, API flow, and facet-promotion design.

## Provenance

Quilombo stores provenance metadata supplied by the client, such as “processed from a workshop
photo on 2026-08-15”. It does not upload or retain the source photo or video.

## History and undo

The workspace history shows immutable inventory events and their provenance. Recent bulk upserts,
imports, and moves can be undone only when they are the latest event and the inventory still
matches the state produced by that event. Undo always requires a preview, restores the preceding
state, and appends a compensating event; it never edits or deletes the original event.

## Freshness

Locations and holdings can be confirmed by an audit, marked unknown, or become stale after the
configured freshness window. A later inventory mutation invalidates the earlier verification
unless it carries a new observation. See [Audits and freshness](audits.md) for the agent behavior
and MCP contract.
