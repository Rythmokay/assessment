import random
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from orders.models import Order, OrderItem, Product


class Command(BaseCommand):
    help = "Create a demo customer with N orders (default 250) for profiling the summary endpoint."

    def add_arguments(self, parser):
        parser.add_argument("--orders", type=int, default=250)
        parser.add_argument("--items-per-order", type=int, default=4)
        parser.add_argument("--username", default="demo")
        parser.add_argument("--password", default="demo-password")

    @transaction.atomic
    def handle(self, *args, **opts):
        rng = random.Random(42)
        user, _ = get_user_model().objects.get_or_create(username=opts["username"])
        user.set_password(opts["password"])
        user.save()
        user.orders.all().delete()

        products = [
            Product.objects.get_or_create(sku=f"SKU-{i:04d}", defaults={"name": f"Product {i}"})[0]
            for i in range(50)
        ]

        now = timezone.now()
        orders = Order.objects.bulk_create(
            Order(
                customer=user,
                status=rng.choice(Order.Status.values),
                created_at=now - timedelta(hours=i),
            )
            for i in range(opts["orders"])
        )
        OrderItem.objects.bulk_create(
            OrderItem(
                order=order,
                product=rng.choice(products),
                quantity=rng.randint(1, 5),
                unit_price=Decimal(rng.randint(199, 9999)) / 100,
            )
            for order in orders
            for _ in range(opts["items_per_order"])
        )

        self.stdout.write(
            self.style.SUCCESS(
                f"User '{user.username}' now has {len(orders)} orders "
                f"x {opts['items_per_order']} items."
            )
        )
