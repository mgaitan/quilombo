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
    "author": "Alejandro Dolina",
    "isbn": "978-950-07-1234-5"
  },
  "description": "Blue edition, medium height"
}
```

Quilombo stores these facts; the client or agent decides how to identify a book and whether an
external catalog should be consulted.

## Search

Search is deterministic and workspace-scoped. It normalizes accents and punctuation, ranks matches,
and reports which terms and fields matched. Short technical codes are exact tokens, so `AA` does
not match `AAA`. The API does not invent semantic synonyms; clients can add known aliases or issue
separate translated searches.

## Provenance

Quilombo stores provenance metadata supplied by the client, such as “processed from a workshop
photo on 2026-08-15”. It does not upload or retain the source photo or video.
