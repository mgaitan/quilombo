from django.contrib import admin
from django.urls import path, include
from rest_framework.schemas import get_schema_view


urlpatterns = [
    path('admin/', admin.site.urls),
    path("api/schema",
            get_schema_view(
                title="Quilombo", description="API for your quilombo …", version="1.0.0",
                url='https://quilombo-i1f4.onrender.com/api/'
            ),
            name="openapi-schema",

        ),
    path('api/', include('inventory.urls')),
]