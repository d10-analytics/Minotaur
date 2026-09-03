"""The billing subsystem: charging orders once they are complete."""

from shop.ledger import record


def charge(order):
    record(("charged", order))
    return {"order": order, "status": "charged"}
