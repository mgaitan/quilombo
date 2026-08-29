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
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        raise CatalogMalformedResponse("Open Library returned an invalid response.")
    return [row["name"] for row in rows if row.get("name")]


def _text(value):
    if value is None:
        return ""
    if isinstance(value, dict):
        value = value.get("value")
    if isinstance(value, list):
        value = " ".join(str(part) for part in value if part)
    return value.strip() if isinstance(value, str) else ""


def _string_list(value):
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(row, str) for row in value):
        raise CatalogMalformedResponse("Open Library returned an invalid response.")
    return [row.strip() for row in value if row.strip()]


def _request_json(url):
    request = Request(url, headers={"User-Agent": settings.BOOK_CATALOG_USER_AGENT})
    max_retries = min(max(settings.BOOK_CATALOG_MAX_RETRIES, 0), 2)
    attempts = max_retries + 1
    for attempt in range(attempts):
        try:
            with urlopen(request, timeout=settings.BOOK_CATALOG_TIMEOUT_SECONDS) as response:
                return json.load(response)
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
    raise CatalogLookupError("Open Library is temporarily unavailable.")


def _search_candidate(document, search_url):
    if not isinstance(document, dict):
        raise CatalogMalformedResponse("Open Library returned an invalid response.")
    title = document.get("title")
    if not isinstance(title, str) or not title.strip():
        return None
    edition_keys = _string_list(document.get("edition_key"))
    isbns = _string_list(document.get("isbn"))
    cover_id = document.get("cover_i")
    if cover_id is not None and (isinstance(cover_id, bool) or not isinstance(cover_id, int)):
        raise CatalogMalformedResponse("Open Library returned an invalid response.")
    publication_year = document.get("first_publish_year")
    if publication_year is not None and (
        isinstance(publication_year, bool) or not isinstance(publication_year, int)
    ):
        raise CatalogMalformedResponse("Open Library returned an invalid response.")
    pages = document.get("number_of_pages_median")
    if pages is not None and (isinstance(pages, bool) or not isinstance(pages, int)):
        raise CatalogMalformedResponse("Open Library returned an invalid response.")
    source_url = search_url
    if edition_keys:
        source_url = f"https://openlibrary.org/books/{edition_keys[0]}"
    return {
        "title": title.strip(),
        "authors": _string_list(document.get("author_name")),
        "publishers": _string_list(document.get("publisher")),
        "publication_year": publication_year,
        "page_count": pages,
        "identifiers": {"isbn": isbns} if isbns else {},
        "isbn": isbns,
        "openlibrary_edition": edition_keys[0] if edition_keys else "",
        "cover_url": (f"https://covers.openlibrary.org/b/id/{cover_id}-M.jpg" if cover_id else ""),
        "source_url": source_url,
    }


def search_books(*, title, authors=None, publishers=None, limit=5):
    if not isinstance(title, str) or not title.strip():
        raise ValueError("A book title is required for catalog lookup.")
    query_params = {
        "title": title.strip(),
        "limit": min(max(limit, 1), 10),
        "fields": (
            "title,author_name,publisher,first_publish_year,edition_key,isbn,cover_i,"
            "number_of_pages_median"
        ),
    }
    if authors:
        query_params["author"] = ", ".join(authors)
    if publishers:
        query_params["publisher"] = ", ".join(publishers)
    search_url = f"https://openlibrary.org/search.json?{urlencode(query_params)}"
    cache_key = f"book-catalog:open-library:search:{urlencode(query_params)}"
    if cached := cache.get(cache_key):
        return cached
    payload = _request_json(search_url)
    if not isinstance(payload, dict) or not isinstance(payload.get("docs"), list):
        raise CatalogMalformedResponse("Open Library returned an invalid response.")
    candidates = [
        candidate
        for document in payload["docs"]
        if (candidate := _search_candidate(document, search_url)) is not None
    ]
    if not candidates:
        raise CatalogRecordNotFound("No Open Library records matched that book.")
    result = {
        "provider": "open_library",
        "query": {
            "title": title.strip(),
            "authors": authors or [],
            "publishers": publishers or [],
        },
        "retrieved_at": timezone.now().isoformat(),
        "candidates": candidates,
    }
    cache.set(cache_key, result, timeout=60 * 60 * 24)
    return result


def lookup_book_by_isbn(value):
    isbn = normalize_isbn(value)
    cache_key = f"book-catalog:open-library:{isbn}"
    if cached := cache.get(cache_key):
        return cached

    bibkey = f"ISBN:{isbn}"
    query = urlencode({"bibkeys": bibkey, "jscmd": "data", "format": "json"})
    payload = _request_json(f"https://openlibrary.org/api/books?{query}")

    if not isinstance(payload, dict):
        raise CatalogMalformedResponse("Open Library returned an invalid response.")

    book = payload.get(bibkey)
    if book is None or book == {}:
        raise CatalogRecordNotFound("No Open Library record was found for that ISBN.")
    if not isinstance(book, dict):
        raise CatalogMalformedResponse("Open Library returned an invalid response.")

    raw_identifiers = book.get("identifiers") or {}
    if not isinstance(raw_identifiers, dict):
        raise CatalogMalformedResponse("Open Library returned an invalid response.")
    identifiers = {key: values for key, values in raw_identifiers.items() if values}
    identifiers.setdefault("isbn", [isbn])
    description = _text(book.get("description") or book.get("notes"))
    description = description or book.get("subtitle", "")
    cover = book.get("cover") or {}
    if not isinstance(cover, dict):
        raise CatalogMalformedResponse("Open Library returned an invalid response.")
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
            "cover_url": cover.get("medium", ""),
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
