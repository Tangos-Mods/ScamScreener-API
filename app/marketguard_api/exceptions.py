from __future__ import annotations


class MarketGuardError(Exception):
    pass


class HypixelUpstreamError(MarketGuardError):
    pass


class HypixelAuthenticationError(HypixelUpstreamError):
    """The configured Hypixel API key is missing, rejected, or otherwise unusable."""


class MarketGuardStorageError(MarketGuardError):
    pass


class LowestBinHistoryError(MarketGuardStorageError):
    pass


class HypixelSnapshotDriftError(HypixelUpstreamError):
    pass


class HypixelRateLimitError(HypixelUpstreamError):
    def __init__(self, message: str, retry_after_seconds: int | None = None) -> None:
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


class MojangUpstreamError(MarketGuardError):
    pass
