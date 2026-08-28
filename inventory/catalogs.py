import json
import re
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen

from django.conf import settings
from django.core.cache import cache
from django.utils import timezone


class CatalogLookupError(Exception):
    pass


class CatalogRecordNotFound(CatalogLookupError):
    pass


class CatalogRateLimitError(CatalogLookupError):
    pass


class CatalogTimeoutError(CatalogLookupError):
    pass


class CatalogMalformedResponse(CatalogLookupError):
    pass


def normalize_isbn(value):
    if not isinstance(value, str):
        raise ValueError("ISBN must be a string containing 10 or 13 digits.")
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
    if rows is None:
        return []
    if not isinstance(rows, list):
        raise CatalogMalformedResponse("Open Library returned an invalid response.")
    names = []
    for row in rows:
        if isinstance(row, str):
            name = row
        elif isinstance(row, dict):
            name = row.get("name", "")
        else:
            raise CatalogMalformedResponse("Open Library returned an invalid response.")
        if name and not isinstance(name, str):
            raise CatalogMalformedResponse("Open Library returned an invalid response.")
        if name:
            names.append(name)
    return names


def _text(value):
    if value is None:
        return ""
    if isinstance(value, dict):
        value = value.get("value")
    if isinstance(value, list):
        if not all(isinstance(part, str) for part in value if part):
            raise CatalogMalformedResponse("Open Library returned an invalid response.")
        value = " ".join(part for part in value if part)
    if value and not isinstance(value, str):
        raise CatalogMalformedResponse("Open Library returned an invalid response.")
    return value.strip() if value else ""


def _page_count(value):
    if value is None:
        return None
    if isinstance(value, bool):
        raise CatalogMalformedResponse("Open Library returned an invalid response.")
    if isinstance(value, int):
        page_count = value
    elif isinstance(value, str) and value.strip().isdigit():
        page_count = int(value.strip())
    else:
        raise CatalogMalformedResponse("Open Library returned an invalid response.")
    if page_count < 0:
        raise CatalogMalformedResponse("Open Library returned an invalid response.")
    return page_count


def _identifiers(value):
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise CatalogMalformedResponse("Open Library returned an invalid response.")
    identifiers = {}
    for key, values in value.items():
        if not isinstance(key, str) or not isinstance(values, list):
            raise CatalogMalformedResponse("Open Library returned an invalid response.")
        if not all(isinstance(identifier, str) for identifier in values):
            raise CatalogMalformedResponse("Open Library returned an invalid response.")
        normalized = [identifier.strip() for identifier in values if identifier.strip()]
        if normalized:
            identifiers[key] = normalized
    return identifiers


def _url(value, fallback=""):
    if value is None or value == "":
        return fallback
    if not isinstance(value, str):
        raise CatalogMalformedResponse("Open Library returned an invalid response.")
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise CatalogMalformedResponse("Open Library returned an invalid response.")
    return value


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
    max_retries = min(max(settings.BOOK_CATALOG_MAX_RETRIES, 0), 2)
    attempts = max_retries + 1
    for attempt in range(attempts):
        try:
            with urlopen(request, timeout=settings.BOOK_CATALOG_TIMEOUT_SECONDS) as response:
                payload = json.load(response)
            break
        except HTTPError as error:
            if error.code == 429:
                if attempt + 1 < attempts:
                    continue
                raise CatalogRateLimitError(
                    "Open Library rate limit reached. Try again later."
                ) from error
            retryable = error.code in {408, 429} or 500 <= error.code < 600
            if retryable and attempt + 1 < attempts:
                continue
            raise CatalogLookupError("Open Library is temporarily unavailable.") from error
        except (URLError, TimeoutError) as error:
            if attempt + 1 < attempts:
                continue
            if isinstance(error, TimeoutError) or isinstance(
                getattr(error, "reason", None), TimeoutError
            ):
                raise CatalogTimeoutError(
                    "Open Library request timed out. Try again later."
                ) from error
            raise CatalogLookupError("Open Library is temporarily unavailable.") from error
        except ValueError as error:
            raise CatalogMalformedResponse("Open Library returned an invalid response.") from error

    if not isinstance(payload, dict):
        raise CatalogMalformedResponse("Open Library returned an invalid response.")

    book = payload.get(bibkey)
    if book is None or book == {}:
        raise CatalogRecordNotFound("No Open Library record was found for that ISBN.")
    if not isinstance(book, dict):
        raise CatalogMalformedResponse("Open Library returned an invalid response.")

    title = _text(book.get("title"))
    if not title:
        raise CatalogMalformedResponse("Open Library returned an invalid response.")
    subtitle = _text(book.get("subtitle"))
    authors = _names(book.get("authors"))
    publishers = _names(book.get("publishers"))
    publish_date = _text(book.get("publish_date"))
    edition = _text(book.get("edition_name") or book.get("edition"))
    physical_format = _text(book.get("physical_format") or book.get("format"))
    subjects = _names(book.get("subjects"))[:20]
    page_count = _page_count(book.get("number_of_pages"))
    cover = book.get("cover")
    if cover is None:
        cover = {}
    elif not isinstance(cover, dict):
        raise CatalogMalformedResponse("Open Library returned an invalid response.")
    cover_urls = {
        size: _url(cover.get(size)) for size in ("small", "medium", "large") if cover.get(size)
    }
    source_url = _url(book.get("url"), f"https://openlibrary.org/isbn/{isbn}")
    description = _text(book.get("description"))
    if not description:
        description = _text(book.get("notes"))
    description = description or subtitle
    identifiers = _identifiers(book.get("identifiers"))
    identifiers.setdefault("isbn", [isbn])
    retrieved_at = timezone.now().isoformat()
    cover_url = cover_urls.get("medium", "")
    attributes = {
        "schema": "book",
        "identifiers": identifiers,
        "book": {
            "title": title,
            "subtitle": subtitle,
            "description": description,
            "synopsis": description,
            "authors": authors,
            "publishers": publishers,
            "publish_date": publish_date,
            "publication_date": publish_date,
            "edition": edition,
            "format": physical_format,
            "page_count": page_count,
            "subjects": subjects,
            "cover": cover_url,
            "cover_url": cover_url,
            "source_url": source_url,
            "retrieved_at": retrieved_at,
        },
    }
    result = {
        "provider": "open_library",
        "isbn": isbn,
        "source_url": source_url,
        "retrieved_at": retrieved_at,
        "provenance": {
            "source_kind": "other",
            "source_reference": source_url,
            "metadata": {
                "provider": "open_library",
                "isbn": isbn,
                "retrieved_at": retrieved_at,
            },
        },
        "suggested_item": {
            "name": title,
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
