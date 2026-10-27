from decimal import Decimal

from django.conf import settings
from django.db import models
from django.utils import timezone


class Product(models.Model):
    name = models.CharField(max_length=200)
    sku = models.CharField(max_length=40, unique=True)

    def __str__(self):
        return self.name


class Order(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending"
        PAID = "paid"
        SHIPPED = "shipped"
        CANCELLED = "cancelled"

    customer = models.ForeignKey(
        settings.AUTH_USER_MODEL, related_name="orders", on_delete=models.CASCADE
    )
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        indexes = [models.Index(fields=["customer", "-created_at"])]

    def __str__(self):
        return f"Order #{self.pk}"

    @property
    def total(self):
        # Reads self.items.all(): one query per call unless the queryset that
        # loaded this order used prefetch_related("items").
        return sum((item.line_total for item in self.items.all()), Decimal("0.00"))


class OrderItem(models.Model):
    order = models.ForeignKey(Order, related_name="items", on_delete=models.CASCADE)
    product = models.ForeignKey(Product, related_name="+", on_delete=models.PROTECT)
    quantity = models.PositiveIntegerField()
    unit_price = models.DecimalField(max_digits=10, decimal_places=2)

    @property
    def line_total(self):
        return self.unit_price * self.quantity
