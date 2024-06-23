from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import ProductViewSet, LocationViewSet, StockViewSet, StockTransactionViewSet, update_stock

router = DefaultRouter()
router.register(r'products', ProductViewSet)
router.register(r'locations', LocationViewSet)
router.register(r'stocks', StockViewSet)
router.register(r'stocktransactions', StockTransactionViewSet)

urlpatterns = [
    path('', include(router.urls)),
    path('update_stock/', update_stock),
]
