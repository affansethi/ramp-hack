"""Ramp-shaped simulator seam (sandbox later via RAMP_API_BASE).

HMAC-SHA256 verification of X-Ramp-Signature, event dedup, transaction
and receipt fetches. Webhook intake stores the event and returns 200;
all work happens in the worker.
"""

from typing import Protocol


class RampClient(Protocol):
    """Simulator/sandbox client boundary. Implementation lands with the
    webhook-intake work item."""
