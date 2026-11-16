"""Session occupancy observations, deliberately separate from billable usage."""

from dataclasses import dataclass
from datetime import datetime, timezone


@dataclass
class ContextUsage:
    min_peak: int = 64000
    drop_percent: int = 50
    used: int | None = None
    size: int | None = None
    peak: int | None = None
    suspected_compaction: bool = False
    _comparable: bool = False
    observed_at: str | None = None
    # Monitoring cannot attribute later samples to the selected model after an
    # unsolicited upstream model/config change. Restored by a new ACP session.
    model_known: bool = True

    def observe(self, update: dict) -> None:
        # Only ACP v1 usage_update.used/size has this meaning. inputTokens and
        # cache counters measure work, not context, and must never enter here.
        used, size = update.get("used"), update.get("size")
        if (
            type(used) is not int or type(size) is not int
            or not 0 < used <= size <= 2**53 - 1
        ):
            self._comparable = False
            self.used = self.size = self.peak = None
            self.observed_at = None
            return
        if self._comparable and size == self.size:
            if (
                self.peak is not None and self.peak >= self.min_peak
                and used * 100 < self.peak * self.drop_percent
            ):
                self.suspected_compaction = True
            self.peak = max(self.peak or used, used)
        else:
            # A changed window/model or invalid intervening sample breaks the
            # comparison chain; no cross-model/cross-session inference.
            self.peak = used
        self.used, self.size = used, size
        self.observed_at = datetime.now(timezone.utc).isoformat()
        self._comparable = True

    def summary(self) -> dict:
        return {
            "context_used": self.used,
            "context_window_size": self.size,
            "observed_at": self.observed_at,
            "context_model_known": self.model_known,
            "peak_context_used": self.peak,
            "upstream_compaction_detected": self.suspected_compaction,
        }
