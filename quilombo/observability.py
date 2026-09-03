"""Optional Sentry error and performance monitoring.

Sentry is activated only when ``SENTRY_DSN`` is set, so local development and the
test suite run without it. The DSN and every tuning knob come from the
environment (Render), never from the repository.
"""

import os

HEALTH_CHECK_PATH = "/health/"
HEALTH_CHECK_TRANSACTIONS = {"/health/", "health/", "health_check"}
DEFAULT_TRACES_SAMPLE_RATE = 0.05

# Field-name fragments whose values must never reach Sentry.
SENSITIVE_KEY_FRAGMENTS = (
    "password",
    "token",
    "secret",
    "authorization",
    "cookie",
    "api_key",
    "apikey",
    "api-key",
    "dsn",
    "credential",
)

FILTERED = "[Filtered]"


def _looks_sensitive(key):
    lowered = str(key).lower()
    return any(fragment in lowered for fragment in SENSITIVE_KEY_FRAGMENTS)


def _scrub(value, depth=0):
    if depth > 6:
        return value
    if isinstance(value, dict):
        return {
            key: FILTERED if _looks_sensitive(key) else _scrub(item, depth + 1)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_scrub(item, depth + 1) for item in value]
    return value


def scrub_event(event, _hint=None):
    """Drop request bodies/cookies and filter sensitive keys from an event."""
    request = event.get("request")
    if isinstance(request, dict):
        request.pop("data", None)
        request.pop("cookies", None)
        headers = request.get("headers")
        if isinstance(headers, dict):
            request["headers"] = {
                key: FILTERED if _looks_sensitive(key) else value for key, value in headers.items()
            }
    for section in ("extra", "contexts", "tags"):
        if section in event:
            event[section] = _scrub(event[section])
    return event


def before_send_transaction(event, _hint=None):
    if event.get("transaction") in HEALTH_CHECK_TRANSACTIONS:
        return None
    return scrub_event(event)


def traces_sample_rate():
    try:
        return float(os.environ.get("SENTRY_TRACES_SAMPLE_RATE", DEFAULT_TRACES_SAMPLE_RATE))
    except ValueError:
        return DEFAULT_TRACES_SAMPLE_RATE


def traces_sampler(sampling_context):
    asgi_scope = sampling_context.get("asgi_scope") or {}
    wsgi_environ = sampling_context.get("wsgi_environ") or {}
    path = asgi_scope.get("path") or wsgi_environ.get("PATH_INFO", "")
    if path == HEALTH_CHECK_PATH:
        return 0.0
    return traces_sample_rate()


def init_sentry(*, release, is_prod):
    """Initialise Sentry when a DSN is present. Returns whether it was enabled."""
    dsn = os.environ.get("SENTRY_DSN", "").strip()
    if not dsn:
        return False

    import sentry_sdk

    sentry_sdk.init(
        dsn=dsn,
        release=release,
        environment=os.environ.get(
            "SENTRY_ENVIRONMENT", "production" if is_prod else "development"
        ),
        send_default_pii=False,
        max_request_body_size="never",
        traces_sampler=traces_sampler,
        before_send=scrub_event,
        before_send_transaction=before_send_transaction,
    )
    return True
