# inventory/serializers.py

from rest_framework import serializers
from .models import Product, Location, Stock, StockTransaction

class ProductSerializer(serializers.ModelSerializer):
    quantity = serializers.ReadOnlyField()

    class Meta:
        model = Product
        fields = '__all__'


class LocationSerializer(serializers.ModelSerializer):
    class Meta:
        model = Location
        fields = '__all__'


class StockSerializer(serializers.ModelSerializer):
    class Meta:
        model = Stock
        fields = '__all__'


class StockTransactionSerializer(serializers.ModelSerializer):
    class Meta:
        model = StockTransaction
        fields = '__all__'
