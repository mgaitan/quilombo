# inventory/views.py

from rest_framework import filters, viewsets
from rest_framework.decorators import api_view
from rest_framework.response import Response

from .models import Location, Product, Stock, StockTransaction
from .serializers import (
    LocationSerializer,
    ProductSerializer,
    StockSerializer,
    StockTransactionSerializer,
)


class ProductViewSet(viewsets.ModelViewSet):
    queryset = Product.objects.all()
    serializer_class = ProductSerializer
    filter_backends = [filters.SearchFilter]
    search_fields = ["name", "description"]


class LocationViewSet(viewsets.ModelViewSet):
    queryset = Location.objects.all()
    serializer_class = LocationSerializer
    filter_backends = [filters.SearchFilter]
    search_fields = ["name", "description"]


class StockViewSet(viewsets.ModelViewSet):
    queryset = Stock.objects.all()
    serializer_class = StockSerializer


class StockTransactionViewSet(viewsets.ModelViewSet):
    queryset = StockTransaction.objects.all()
    serializer_class = StockTransactionSerializer


@api_view(["POST"])
def update_stock(request):
    product_id = request.data.get("product_id")
    location_id = request.data.get("location_id")
    quantity_change = float(request.data.get("quantity_change"))
    approximate = request.data.get("approximate", False)

    stock = Stock.objects.filter(product_id=product_id, location_id=location_id).first()

    if stock:
        stock.quantity += quantity_change
        stock.approximate = approximate
        stock.save()

        StockTransaction.objects.create(
            product_id=product_id, location_id=location_id, quantity_change=quantity_change
        )

        return Response({"message": "Stock updated successfully"})
    else:
        return Response({"error": "Stock not found"}, status=404)
