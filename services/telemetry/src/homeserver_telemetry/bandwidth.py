class SeedBandwidthPolicy:
    """Conservative upload policy with a 60-second hysteresis window."""

    REMOTE_LIMIT_BPS = 625_000
    IDLE_LIMIT_BPS = 2_500_000

    def __init__(self) -> None:
        self._last_remote_at: float | None = None

    def update(self, *, remote_count: int | None, source_fresh: bool, now: float) -> int:
        if not source_fresh or remote_count is None:
            return self.REMOTE_LIMIT_BPS
        if remote_count > 0:
            self._last_remote_at = now
            return self.REMOTE_LIMIT_BPS
        if self._last_remote_at is None or now - self._last_remote_at >= 60:
            return self.IDLE_LIMIT_BPS
        return self.REMOTE_LIMIT_BPS
