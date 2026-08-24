from django.conf import settings


def runtime(request):
    return {"APP_VERSION": settings.APP_VERSION}


def social_auth(request):
    providers = settings.SOCIALACCOUNT_PROVIDERS
    return {
        "github_login_enabled": bool(providers["github"]["APPS"]),
        "google_login_enabled": bool(providers["google"]["APPS"]),
    }
