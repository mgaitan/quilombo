from copy import deepcopy

ATTRIBUTE_PROFILES = {
    "book": {
        "category": "book",
        "version": "1.0",
        "description": "Minimum user-provided attributes for books and catalog lookup.",
        "preserve_unknown_attributes": True,
        "minimum_for_catalog_lookup": ["title"],
        "recommended_for_disambiguation": ["authors", "publishers"],
        "fields": [
            {
                "key": "title",
                "type": "string",
                "label": "Title",
                "optional": False,
                "recommended": True,
                "lookup_role": "required",
            },
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
        ],
    }
}


def get_attribute_profile(category):
    normalized_category = category.strip().lower()
    if normalized_category == "books":
        normalized_category = "book"
    profile = ATTRIBUTE_PROFILES.get(normalized_category)
    return deepcopy(profile) if profile else None
