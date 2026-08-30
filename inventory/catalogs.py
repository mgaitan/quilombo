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


def normalize_edition_key(value):
    if not isinstance(value, str):
        raise ValueError("Open Library edition must be a valid edition key.")
    edition_key = value.strip().removeprefix("/books/")
    if not re.fullmatch(r"OL[0-9]+M", edition_key):
        raise ValueError("Open Library edition must be a valid edition key.")
    return edition_key


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


def _edition_names(value):
    if value is None:
        return []
    if not isinstance(value, list):
        raise CatalogMalformedResponse("Open Library returned an invalid response.")
    names = []
    for row in value:
        if isinstance(row, str):
            name = row.strip()
        elif isinstance(row, dict):
            name = row.get("name", "")
            if not isinstance(name, str):
                raise CatalogMalformedResponse("Open Library returned an invalid response.")
            name = name.strip()
        else:
            raise CatalogMalformedResponse("Open Library returned an invalid response.")
        if name:
            names.append(name)
    return names


def _edition_url(edition_key):
    return f"https://openlibrary.org/books/{normalize_edition_key(edition_key)}.json"


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


def _edition_year(value):
    if value is None:
        return None
    if not isinstance(value, str):
        raise CatalogMalformedResponse("Open Library returned an invalid response.")
    match = re.search(r"(?:^|[^0-9])(1[0-9]{3}|20[0-9]{2})(?:[^0-9]|$)", value)
    return int(match.group(1)) if match else None


def _edition_isbns(edition):
    raw_identifiers = edition.get("identifiers") or {}
    if not isinstance(raw_identifiers, dict):
        raise CatalogMalformedResponse("Open Library returned an invalid response.")
    values = []
    for field in ("isbn_13", "isbn_10", "isbn"):
        field_values = raw_identifiers.get(field, [])
        if isinstance(field_values, str):
            field_values = [field_values]
        if not isinstance(field_values, list) or not all(
            isinstance(value, str) for value in field_values
        ):
            raise CatalogMalformedResponse("Open Library returned an invalid response.")
        values.extend(value.strip() for value in field_values if value.strip())
    return list(dict.fromkeys(values))


def _edition_cover_id(edition):
    covers = edition.get("covers")
    if covers is None:
        return None
    if not isinstance(covers, list) or not all(
        isinstance(value, int) and not isinstance(value, bool) for value in covers
    ):
        raise CatalogMalformedResponse("Open Library returned an invalid response.")
    return covers[0] if covers else None


def _search_candidate(document, search_url, *, edition_key="", edition=None):
    if not isinstance(document, dict):
        raise CatalogMalformedResponse("Open Library returned an invalid response.")
    edition = edition or {}
    title = edition.get("title") or document.get("title")
    if not isinstance(title, str) or not title.strip():
        return None
    edition_keys = [edition_key] if edition_key else _string_list(document.get("edition_key"))
    isbns = _edition_isbns(edition) if edition else _string_list(document.get("isbn"))
    cover_id = _edition_cover_id(edition) if edition else document.get("cover_i")
    if cover_id is not None and (isinstance(cover_id, bool) or not isinstance(cover_id, int)):
        raise CatalogMalformedResponse("Open Library returned an invalid response.")
    publication_year = (
        _edition_year(edition.get("publish_date"))
        if edition
        else document.get("first_publish_year")
    )
    if publication_year is not None and (
        isinstance(publication_year, bool) or not isinstance(publication_year, int)
    ):
        raise CatalogMalformedResponse("Open Library returned an invalid response.")
    pages = edition.get("number_of_pages") if edition else document.get("number_of_pages_median")
    if pages is not None and (isinstance(pages, bool) or not isinstance(pages, int)):
        raise CatalogMalformedResponse("Open Library returned an invalid response.")
    source_url = search_url
    if edition_keys:
        source_url = f"https://openlibrary.org/books/{normalize_edition_key(edition_keys[0])}"
    return {
        "title": title.strip(),
        "authors": _string_list(document.get("author_name")),
        "publishers": (
            _edition_names(edition.get("publishers"))
            if edition and edition.get("publishers") is not None
            else _string_list(document.get("publisher"))
        ),
        "publication_year": publication_year,
        "page_count": pages,
        "identifiers": {"isbn": isbns} if isbns else {},
        "isbn": isbns,
        "openlibrary_edition": (normalize_edition_key(edition_keys[0]) if edition_keys else ""),
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
    candidates = []
    for document in payload["docs"]:
        if not isinstance(document, dict):
            raise CatalogMalformedResponse("Open Library returned an invalid response.")
        edition_keys = _string_list(document.get("edition_key"))
        if not edition_keys:
            candidate = _search_candidate(document, search_url)
            if candidate is not None:
                candidates.append(candidate)
            continue
        for edition_key in edition_keys:
            if len(candidates) >= 10:
                break
            edition = None
            if len(edition_keys) > 1:
                edition = _request_json(_edition_url(edition_key))
                if not isinstance(edition, dict) or not edition:
                    raise CatalogMalformedResponse("Open Library returned an invalid response.")
            candidate = _search_candidate(
                document,
                search_url,
                edition_key=edition_key,
                edition=edition,
            )
            if candidate is not None:
                candidates.append(candidate)
        if len(candidates) >= 10:
            break
    if not candidates:
        raise CatalogRecordNotFound("No Open Library records matched that book.")
    result = {
        "provider": "open_library",
        "source_url": search_url,
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

    return _book_result(book, isbn=isbn, cache_key=cache_key)


def _book_detail_result(result):
    return {
        "isbn": result["isbn"],
        "provider": result["provider"],
        "source_url": result["source_url"],
        "retrieved_at": result["retrieved_at"],
        "details": result["suggested_item"]["attributes"]["book"],
        "identifiers": result["suggested_item"]["attributes"]["identifiers"],
    }


def lookup_books_by_isbn(values):
    if not isinstance(values, list) or not values:
        raise ValueError("At least one ISBN is required for catalog lookup.")
    if len(values) > 100:
        raise ValueError("At most 100 ISBNs can be looked up at once.")

    normalized = []
    duplicates = []
    for value in values:
        isbn = normalize_isbn(value)
        if isbn in normalized:
            duplicates.append(isbn)
        else:
            normalized.append(isbn)

    results = {}
    missing = []
    for isbn in normalized:
        cache_key = f"book-catalog:open-library:{isbn}"
        cached = cache.get(cache_key)
        if cached:
            results[isbn] = {"status": "found", **_book_detail_result(cached)}
        else:
            missing.append(isbn)

    for start in range(0, len(missing), 20):
        chunk = missing[start : start + 20]
        bibkeys = ",".join(f"ISBN:{isbn}" for isbn in chunk)
        query = urlencode({"bibkeys": bibkeys, "jscmd": "data", "format": "json"})
        payload = _request_json(f"https://openlibrary.org/api/books?{query}")
        if not isinstance(payload, dict):
            raise CatalogMalformedResponse("Open Library returned an invalid response.")
        for isbn in chunk:
            bibkey = f"ISBN:{isbn}"
            book = payload.get(bibkey)
            if book is None or book == {}:
                results[isbn] = {
                    "isbn": isbn,
                    "status": "not_found",
                    "message": "No Open Library record was found for that ISBN.",
                }
                continue
            if not isinstance(book, dict):
                raise CatalogMalformedResponse("Open Library returned an invalid response.")
            cache_key = f"book-catalog:open-library:{isbn}"
            catalog_result = _book_result(book, isbn=isbn, cache_key=cache_key)
            results[isbn] = {"status": "found", **_book_detail_result(catalog_result)}

    return {
        "provider": "open_library",
        "requested": normalized,
        "duplicates": list(dict.fromkeys(duplicates)),
        "results": [results[isbn] for isbn in normalized],
        "retrieved_at": timezone.now().isoformat(),
    }


def _book_result(book, *, isbn="", edition_key="", cache_key=None):
    raw_identifiers = book.get("identifiers") or {}
    if not isinstance(raw_identifiers, dict):
        raise CatalogMalformedResponse("Open Library returned an invalid response.")
    identifiers = {key: values for key, values in raw_identifiers.items() if values}
    if isbn:
        identifiers.setdefault("isbn", [isbn])
    if edition_key:
        identifiers.setdefault("openlibrary_edition", [edition_key])
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
            "authors": _edition_names(book.get("authors")),
            "publishers": _edition_names(book.get("publishers")),
            "publish_date": book.get("publish_date", ""),
            "page_count": book.get("number_of_pages"),
            "subjects": _edition_names(book.get("subjects"))[:20],
            "cover_url": cover.get("medium", ""),
        },
    }
    source_url = book.get("url")
    if not isinstance(source_url, str) or not source_url:
        source_url = (
            f"https://openlibrary.org/books/{edition_key}"
            if edition_key
            else f"https://openlibrary.org/isbn/{isbn}"
        )
    result = {
        "provider": "open_library",
        **({"isbn": isbn} if isbn else {}),
        **({"openlibrary_edition": edition_key} if edition_key else {}),
        "source_url": source_url,
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
    if cache_key:
        cache.set(cache_key, result, timeout=60 * 60 * 24)
    return result


def lookup_book_by_edition(value):
    edition_key = normalize_edition_key(value)
    cache_key = f"book-catalog:open-library:edition:{edition_key}"
    if cached := cache.get(cache_key):
        return cached
    payload = _request_json(_edition_url(edition_key))
    if not isinstance(payload, dict) or not payload.get("title"):
        raise CatalogRecordNotFound("No Open Library record was found for that edition.")
    return _book_result(payload, edition_key=edition_key, cache_key=cache_key)


def lookup_book_details(*, title, authors=None, publishers=None, isbn="", edition=""):
    if isbn:
        result = lookup_book_by_isbn(isbn)
        return {
            "match_method": "isbn",
            **_book_detail_result(result),
        }
    if edition:
        result = lookup_book_by_edition(edition)
        return {
            "match_method": "edition",
            "provider": result["provider"],
            "openlibrary_edition": result["openlibrary_edition"],
            "source_url": result["source_url"],
            "retrieved_at": result["retrieved_at"],
            "details": result["suggested_item"]["attributes"]["book"],
            "identifiers": result["suggested_item"]["attributes"]["identifiers"],
        }
    result = search_books(title=title, authors=authors, publishers=publishers)
    return {
        "match_method": "metadata",
        "provider": result["provider"],
        "source_url": result["source_url"],
        "query": result["query"],
        "retrieved_at": result["retrieved_at"],
        "candidates": result["candidates"],
    }
