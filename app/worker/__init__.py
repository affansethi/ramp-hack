"""In-process job poller (one gunicorn worker, so one poller).

Atomic claim, lease_until leases, retry limits, expired-lease recovery,
transactional result write + completion. Never approves anything;
survives restart without duplicate effects.
"""

from app.worker.poller import JobPoller

__all__ = ["JobPoller"]
