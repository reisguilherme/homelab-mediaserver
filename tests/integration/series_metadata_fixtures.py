"""Full canonical metadata for legacy scenarios exercising ongoing-season singles."""

from datetime import UTC, datetime, timedelta

import httpx


def ongoing_transport(handler):
    def route(request):
        response = handler(request)
        if request.method != "GET" or request.url.path != "/api/v3/episode":
            return response
        rows = response.json()
        seasons = {row["seasonNumber"] for row in rows if row.get("seasonNumber", 0) > 0}
        for season in seasons:
            numbers = {row["episodeNumber"] for row in rows if row["seasonNumber"] == season}
            for missing in set(range(1, max(numbers) + 1)) - numbers:
                rows.append({
                    "id": 910000 + season * 100 + missing,
                    "seasonNumber": season, "episodeNumber": missing,
                    "monitored": False, "hasFile": True,
                    "airDateUtc": (datetime.now(UTC) - timedelta(days=7)).isoformat(),
                })
            rows.append({
                "id": 900000 + season, "seasonNumber": season,
                "episodeNumber": max(numbers) + 1, "monitored": False, "hasFile": False,
                "airDateUtc": (datetime.now(UTC) + timedelta(days=7)).isoformat(),
            })
        return httpx.Response(response.status_code, json=rows)
    return httpx.MockTransport(route)
