"""Job poller skeleton. Durability comes from the Postgres jobs table:
atomic claim, lease_until leases, retry limits, expired-lease recovery.
Restarting the worker or replaying a job must have no duplicate effects."""


class JobPoller:
    """Polls the jobs table for due work. Implementation lands with the
    intake pipeline; the seam exists so the app can start one poller
    thread per process."""

    def poll_once(self) -> int:
        """Claim and run due jobs once. Returns the number of jobs run."""
        raise NotImplementedError
