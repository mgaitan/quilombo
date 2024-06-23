from django.contrib import admin
from django.urls import path, include
from rest_framework.schemas import get_schema_view


urlpatterns = [
    path('admin/', admin.site.urls),
    path("api/schema",
            get_schema_view(
                title="Quilombo", description="API for your quilombo …", version="1.0.0"
            ),
            name="openapi-schema",
            url='https://quilombo-i1f4.onrender.com/api/'

        ),
    path('api/', include('inventory.urls')),
]