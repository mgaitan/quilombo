from django.contrib import admin
from django.urls import include, path

# from rest_framework.schemas import get_schema_view
from drf_spectacular.views import SpectacularAPIView

urlpatterns = [
    path("admin/", admin.site.urls),
    # path("api/schema",
    #         get_schema_view(
    #             title="Quilombo", description="API for your quilombo …", version="1.0.0",
    #             url='https://quilombo-i1f4.onrender.com/'
    #         ),
    #         name="openapi-schema",
    #     ),
    path("api/schema/", SpectacularAPIView.as_view(), name="schema"),
    path("api/", include("inventory.urls")),
]
