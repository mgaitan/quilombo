from django.db import models

class Product(models.Model):
    name = models.CharField(max_length=100)
    picture = models.ImageField(upload_to='products', blank=True)
    description = models.TextField()
    locations = models.ManyToManyField("Location", through="Stock")

    @property
    def quantity(self):
        return self.stock_set.aggregate(total=models.Sum('quantity'))['total'] or 0

    def __str__(self):
        return self.name


class Location(models.Model):
    name = models.CharField(max_length=100)
    container = models.ForeignKey(
        "self", null=True, blank=True, related_name="sublocation", on_delete=models.CASCADE
    )
    picture = models.ImageField(upload_to='locations', blank=True)
    description = models.TextField()

    def __str__(self):
        return " - ".join(str(loc) for loc in (self.container, self.name) if loc)


class Stock(models.Model):
    product = models.ForeignKey(Product, on_delete=models.CASCADE)
    location = models.ForeignKey(Location, on_delete=models.CASCADE)
    quantity = models.FloatField()
    approximate = models.BooleanField(default=False)

    def save(self, *args, **kwargs):
        # Validate that quantity is non-negative
        if self.quantity < 0:
            raise ValueError("Quantity cannot be negative")
        super().save(*args, **kwargs)

    def __str__(self):
        approx_indicator = " (approx.)" if self.approximate else ""
        return f"{self.product.name} @ {self.location.name} ({self.quantity}) {approx_indicator}"


class StockTransaction(models.Model):
    product = models.ForeignKey(Product, on_delete=models.CASCADE)
    location = models.ForeignKey(Location, on_delete=models.CASCADE)
    quantity_change = models.FloatField()
    timestamp = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.product.name} @ {self.location.name} ({self.quantity_change}) on {self.timestamp}"
