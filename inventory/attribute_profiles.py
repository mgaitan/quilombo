from copy import deepcopy

ATTRIBUTE_PROFILES = {
    "book": {
        "category": "book",
        "version": "1.1",
        "description": "Book-specific attributes for catalog lookup and disambiguation.",
        "preserve_unknown_attributes": True,
        "tracking_mode": "discrete",
        "unit": "copy",
        "minimum_for_catalog_lookup": [],
        "recommended_for_disambiguation": ["authors", "publishers"],
        "fields": [
            {
                "key": "authors",
                "type": "string_list",
                "label": "Author(s)",
                "optional": True,
                "recommended": True,
                "lookup_role": "disambiguation",
            },
            {
                "key": "publishers",
                "type": "string_list",
                "label": "Publisher(s)",
                "optional": True,
                "recommended": True,
                "lookup_role": "disambiguation",
            },
            {
                "key": "publication_date",
                "type": "string",
                "label": "Publication date",
                "optional": True,
            },
            {
                "key": "publication_year",
                "type": "integer",
                "label": "Publication year",
                "optional": True,
            },
            {
                "key": "edition",
                "type": "string",
                "label": "Edition",
                "optional": True,
            },
            {
                "key": "language",
                "type": "string",
                "label": "Language",
                "optional": True,
            },
            {
                "key": "page_count",
                "type": "integer",
                "label": "Pages",
                "optional": True,
            },
        ],
        "identifier_fields": [
            {
                "key": "isbn",
                "type": "isbn",
                "label": "ISBN",
                "optional": True,
            },
            {
                "key": "openlibrary_edition",
                "type": "string",
                "label": "Open Library edition",
                "optional": True,
            },
        ],
    }
}

BOOK_CATEGORIES = frozenset({"book", "books", "libro", "libros"})


def normalize_item_attributes(attributes, category=""):
    if isinstance(attributes, dict):
        normalized = deepcopy(attributes)
    elif attributes is None:
        normalized = {}
    else:
        normalized = {"legacy_attributes": deepcopy(attributes)}
    if not normalized.get("schema") and category.strip().casefold() in BOOK_CATEGORIES:
        normalized["schema"] = "book"
    return normalized


def schema_item_defaults(attributes, category=""):
    attributes = normalize_item_attributes(attributes, category)
    if not isinstance(attributes, dict):
        return {}
    profile = ATTRIBUTE_PROFILES.get(attributes.get("schema"))
    if not profile:
        return {}
    return {field: profile[field] for field in ("tracking_mode", "unit") if field in profile}


def get_attribute_profile(category):
    normalized_category = category.strip().lower()
    if normalized_category == "books":
        normalized_category = "book"
    profile = ATTRIBUTE_PROFILES.get(normalized_category)
    return deepcopy(profile) if profile else None
