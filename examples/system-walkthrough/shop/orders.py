"""The orders subsystem: creating and completing orders."""

from shop.billing import charge
from shop.ledger import record


def create_order(cart):
    order = {"cart": cart, "status": "new"}
    record(("created", order))
    return order


def complete_order(order):
    charge(order)
    order["status"] = "paid"
    return order
