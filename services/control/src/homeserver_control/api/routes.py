"""Route inventory shared by deploy reviews and API contract tests.

Handlers live in :mod:`homeserver_control.api.app` so the executable entrypoint
can assemble one state object and one authentication boundary.  Keeping this
allowlist in a small module makes accidental route expansion visible in code
review.
"""

PUBLIC_ROUTES = frozenset({"GET /health/live", "GET /health/ready"})
ADMIN_ROUTES = frozenset(
    {
        "GET /api/v1/queue",
        "GET /api/v1/capacity",
        "POST /api/v1/deletions/preview",
        "POST /api/v1/deletions",
        "GET /api/v1/operations/{operation_id}",
        "GET /api/v1/telemetry",
    }
)
COLLECTOR_ROUTES = frozenset(
    {
        "GET /internal/v1/events",
        "POST /internal/v1/events/ack",
        "POST /internal/v1/seed-limit",
    }
)
