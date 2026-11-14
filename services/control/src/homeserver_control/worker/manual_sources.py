"""Execute explicit source requests once; automatic health policy is independent."""

from __future__ import annotations

import logging

from homeserver_control.persistence.source_requests import SourceReplacementRequests

LOGGER = logging.getLogger(__name__)


class ManualSourceReplacements:
    def __init__(
        self, *, requests: SourceReplacementRequests, permits, movie_acquirer, series_acquirer
    ) -> None:
        self.requests = requests
        self.permits = permits
        self.movie_acquirer = movie_acquirer
        self.series_acquirer = series_acquirer

    async def run_once(self) -> str:
        command = self.requests.claim()
        if command is None:
            return "idle"
        source = self.requests.source(command["infohash"])
        if (
            source is None
            or source["permit_id"] != command["permit_id"]
            or not self.requests.source_allowed(source)
        ):
            self.requests.finish(command["request_id"], state="stale", result="source_changed")
            return "source_changed"
        acquirer = (
            self.movie_acquirer
            if command["media_key"].startswith("movie:")
            else self.series_acquirer
        )
        if acquirer is None:
            self.requests.finish(
                command["request_id"], state="failed", result="acquirer_unavailable"
            )
            return "acquirer_unavailable"
        try:
            result = await acquirer.manual_replace(
                command["media_key"],
                command["reservation_id"],
                command["permit_id"],
                scope_key=command["scope_key"],
            )
        except Exception as error:
            # URLs, authentication and upstream response bodies stay out of job results/logs.
            LOGGER.error("Manual source replacement failed (%s)", type(error).__name__)
            self.requests.finish(command["request_id"], state="failed", result="search_failed")
            return "search_failed"
        state = (
            "succeeded"
            if result in {"replaced", "grabbed"}
            else "completed"
            if result in {"already_completed", "already_imported"}
            else "stale"
            if result in {"source_changed", "reservation_inactive"}
            else "failed"
            if result in {"replacement_uncertain", "source_uncertain"}
            else "no_candidate"
        )
        self.requests.finish(command["request_id"], state=state, result=result)
        return result
