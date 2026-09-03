"""The storefront checkout: drives order creation and payment."""

from shop.billing import charge
from shop.orders import create_order


def checkout(cart):
    order = create_order(cart)
    return charge(order)
