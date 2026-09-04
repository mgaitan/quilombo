from django.conf import settings


def runtime(request):
    context = {
        "APP_VERSION": settings.APP_VERSION,
        "APP_REVISION": settings.APP_REVISION,
        "APP_VERSION_LINK": "",
        "APP_VERSION_LABEL": f"v{settings.APP_VERSION}",
    }
    if settings.IS_STAGING and settings.APP_REVISION:
        context["APP_VERSION_LABEL"] = settings.APP_REVISION[:9]
        context["APP_VERSION_LINK"] = (
            f"{settings.APP_SOURCE_URL}/compare/v{settings.APP_VERSION}...{settings.APP_REVISION}"
        )
    return context


def social_auth(request):
    providers = settings.SOCIALACCOUNT_PROVIDERS
    return {
        "github_login_enabled": bool(providers["github"]["APPS"]),
        "google_login_enabled": bool(providers["google"]["APPS"]),
    }
