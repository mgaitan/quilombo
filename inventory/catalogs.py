import json
import re
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from django.conf import settings
from django.core.cache import cache
from django.utils import timezone


class CatalogLookupError(Exception):
    pass


class CatalogRecordNotFound(CatalogLookupError):
    pass


def normalize_isbn(value):
    isbn = re.sub(r"[^0-9Xx]", "", value).upper()
    if not (re.fullmatch(r"[0-9]{9}[0-9X]", isbn) or re.fullmatch(r"[0-9]{13}", isbn)):
        raise ValueError("ISBN must contain 10 or 13 digits.")
    if not _valid_isbn_checksum(isbn):
        raise ValueError("ISBN checksum is invalid.")
    return isbn


def _valid_isbn_checksum(isbn):
    if len(isbn) == 10:
        digits = [10 if character == "X" else int(character) for character in isbn]
        return sum((10 - index) * digit for index, digit in enumerate(digits)) % 11 == 0
    return (
        sum(int(character) * (1 if index % 2 == 0 else 3) for index, character in enumerate(isbn))
        % 10
        == 0
    )


def _names(rows):
    return [row["name"] for row in rows or [] if row.get("name")]


def _text(value):
    if isinstance(value, dict):
        value = value.get("value")
    if isinstance(value, list):
        value = " ".join(str(part) for part in value if part)
    return value.strip() if isinstance(value, str) else ""


def lookup_book_by_isbn(value):
    isbn = normalize_isbn(value)
    cache_key = f"book-catalog:open-library:{isbn}"
    if cached := cache.get(cache_key):
        return cached

    bibkey = f"ISBN:{isbn}"
    query = urlencode({"bibkeys": bibkey, "jscmd": "data", "format": "json"})
    request = Request(
        f"https://openlibrary.org/api/books?{query}",
        headers={"User-Agent": settings.BOOK_CATALOG_USER_AGENT},
    )
    try:
        with urlopen(request, timeout=5) as response:
            payload = json.load(response)
    except (HTTPError, URLError, TimeoutError, ValueError) as error:
        raise CatalogLookupError("Open Library is temporarily unavailable.") from error

    book = payload.get(bibkey)
    if not book:
        raise CatalogRecordNotFound("No Open Library record was found for that ISBN.")

    identifiers = {key: values for key, values in (book.get("identifiers") or {}).items() if values}
    identifiers.setdefault("isbn", [isbn])
    description = _text(book.get("description") or book.get("notes"))
    description = description or book.get("subtitle", "")
    attributes = {
        "schema": "book",
        "identifiers": identifiers,
        "book": {
            "title": book.get("title", ""),
            "subtitle": book.get("subtitle", ""),
            "synopsis": description,
            "authors": _names(book.get("authors")),
            "publishers": _names(book.get("publishers")),
            "publish_date": book.get("publish_date", ""),
            "page_count": book.get("number_of_pages"),
            "subjects": _names(book.get("subjects"))[:20],
            "cover_url": (book.get("cover") or {}).get("medium", ""),
        },
    }
    result = {
        "provider": "open_library",
        "isbn": isbn,
        "source_url": book.get("url", f"https://openlibrary.org/isbn/{isbn}"),
        "retrieved_at": timezone.now().isoformat(),
        "suggested_item": {
            "name": book.get("title", ""),
            "description": description,
            "category": "books",
            "aliases": [],
            "attributes": attributes,
            "tracking_mode": "discrete",
            "unit": "copy",
        },
    }
    cache.set(cache_key, result, timeout=60 * 60 * 24)
    return result
