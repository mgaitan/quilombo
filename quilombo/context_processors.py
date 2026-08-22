from django.conf import settings


def runtime(request):
    return {"APP_VERSION": settings.APP_VERSION}
