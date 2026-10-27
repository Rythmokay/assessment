from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from rest_framework.test import APIClient

from orders.models import Order, OrderItem, Product

ITEMS_PER_ORDER = 3


def make_customer(username, order_count):
    user = get_user_model().objects.create_user(username=username, password="x")
    products = [Product.objects.create(name=f"P{i}", sku=f"{username}-{i}") for i in range(3)]
    for _ in range(order_count):
        order = Order.objects.create(customer=user)
        for product in products[:ITEMS_PER_ORDER]:
            OrderItem.objects.create(
                order=order, product=product, quantity=2, unit_price=Decimal("5.00")
            )
    return user


def count_queries(user, url_name):
    client = APIClient()
    client.force_authenticate(user)
    with CaptureQueriesContext(connection) as ctx:
        response = client.get(reverse(url_name))
    assert response.status_code == 200
    return len(ctx), response.json()


@pytest.mark.django_db
def test_fixed_summary_uses_constant_number_of_queries():
    small = make_customer("small", 5)
    large = make_customer("large", 60)

    small_queries, _ = count_queries(small, "order-summary")
    large_queries, body = count_queries(large, "order-summary")

    # One query for orders, one for items joined to products.
    assert small_queries == large_queries == 2
    assert body["order_count"] == 60
    assert body["total_spent"] == str(Decimal("10.00") * ITEMS_PER_ORDER * 60)


@pytest.mark.django_db
def test_before_fix_query_count_grows_with_orders_and_items():
    user = make_customer("regressed", 20)

    queries, _ = count_queries(user, "order-summary-before-fix")

    # 1 (orders) + per order: items for build_summary's total, items for the
    # serializer's total, items for the nested serializer, and one product
    # lookup per item.
    assert queries == 1 + 20 * (3 + ITEMS_PER_ORDER)


@pytest.mark.django_db
def test_fix_does_not_change_the_response():
    user = make_customer("same", 10)

    _, before = count_queries(user, "order-summary-before-fix")
    _, after = count_queries(user, "order-summary")

    assert before == after


@pytest.mark.django_db
def test_summary_only_includes_the_requesting_users_orders():
    make_customer("other", 7)
    user = make_customer("me", 2)

    _, body = count_queries(user, "order-summary")

    assert body["order_count"] == 2
