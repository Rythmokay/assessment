from decimal import Decimal

from django.db.models import Prefetch
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import Order, OrderItem
from .serializers import OrderSerializer


def build_summary(orders):
    orders = list(orders)
    return {
        "order_count": len(orders),
        "total_spent": str(sum((o.total for o in orders), Decimal("0.00"))),
        "orders": OrderSerializer(orders, many=True).data,
    }


class OrderSummaryBeforeFixView(APIView):
    """The regressed endpoint, kept so the profiler comparison is reproducible.

    The view itself is unchanged from before the incident. What changed is
    what it renders: Order.total became a property over order.items and
    OrderSerializer started nesting items with product names. Against a plain
    queryset each of those is a lazy relation, so the number of queries grows
    with orders x items.
    """

    def get(self, request):
        orders = Order.objects.filter(customer=request.user).order_by("-created_at")
        return Response(build_summary(orders))


class OrderSummaryView(APIView):
    def get(self, request):
        orders = (
            Order.objects.filter(customer=request.user)
            .order_by("-created_at")
            .prefetch_related(
                Prefetch("items", queryset=OrderItem.objects.select_related("product"))
            )
        )
        return Response(build_summary(orders))
