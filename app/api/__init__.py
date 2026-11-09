"""FastAPI routers: sessions, org scoping, idempotency, commands, read models.

No model calls or long work inside a request. No endpoint accepts a tax
treatment, label, direction or tax-decision amount (hard rule H5).
"""
