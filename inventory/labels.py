import re
import unicodedata

LABEL_SEARCH_TOKEN_RE = re.compile(r"[\w]+", re.UNICODE)


def label_display_value(value):
    """Return a stable display form while the assertion keeps the original input."""
    return " ".join(unicodedata.normalize("NFKC", str(value or "")).split())


def normalize_label_identity(value):
    """Normalize only differences that are safe to treat as identical."""
    return label_display_value(value).casefold()


def normalize_label_search(value):
    """Fold accents and punctuation for suggestions, never for label identity."""
    folded = unicodedata.normalize("NFKD", str(value or ""))
    without_accents = "".join(char for char in folded if not unicodedata.combining(char))
    return " ".join(LABEL_SEARCH_TOKEN_RE.findall(without_accents.casefold()))
