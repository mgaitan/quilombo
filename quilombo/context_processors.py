from django.conf import settings


def runtime(request):
    """Footer version link.

    Normally the ``v<version>`` label links to its GitHub release. On staging it
    instead shows the deployed commit's short hash linking to that commit's diff
    against the released version running in production.
    """
    version = settings.APP_VERSION
    if settings.IS_STAGING and settings.APP_REVISION:
        label = settings.APP_REVISION[:9]
        link = f"{settings.APP_SOURCE_URL}/compare/v{version}...{settings.APP_REVISION}"
        title = f"Changes since v{version}"
    else:
        label = f"v{version}"
        link = f"{settings.APP_SOURCE_URL}/releases/tag/v{version}"
        title = f"Release notes for v{version}"
    return {
        "APP_VERSION": version,
        "APP_REVISION": settings.APP_REVISION,
        "APP_VERSION_LABEL": label,
        "APP_VERSION_LINK": link,
        "APP_VERSION_TITLE": title,
    }


def social_auth(request):
    providers = settings.SOCIALACCOUNT_PROVIDERS
    return {
        "github_login_enabled": bool(providers["github"]["APPS"]),
        "google_login_enabled": bool(providers["google"]["APPS"]),
    }
