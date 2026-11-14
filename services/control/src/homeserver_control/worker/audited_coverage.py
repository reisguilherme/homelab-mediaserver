"""Protect every sealed operator target from normal Seerr source selection."""

from __future__ import annotations

import re
from dataclasses import dataclass

_SCOPE = re.compile(r'S(\d+)E(\d+)')


@dataclass(frozen=True)
class AuditedCoverage:
    movies: frozenset[int] = frozenset()
    seasons: frozenset[tuple[int, int]] = frozenset()
    series: frozenset[int] = frozenset()
    reservations: frozenset[str] = frozenset()

    @classmethod
    def from_manifest(cls, manifest: dict) -> AuditedCoverage:
        """Consume the validated, sealed snapshot, including already imported files."""
        movies, seasons, series, reservations = set(), set(), set(), set()
        for job in manifest['jobs']:
            reservations.add(job['reservation_id'])
            tmdb_id = job['tmdb_id']
            if job['category'] == 'radarr':
                movies.add(tmdb_id)
                continue
            series.add(tmdb_id)
            for target in job['files']:
                for scope in target['scopes']:
                    match = _SCOPE.fullmatch(scope)
                    if match is None:
                        raise ValueError('audited episode scope is invalid')
                    # Any protected episode excludes its entire season from pack selection.
                    seasons.add((tmdb_id, int(match[1])))
        return cls(frozenset(movies), frozenset(seasons), frozenset(series),
                   frozenset(reservations))

    def protects(self, media_key: str, reservation_id: str | None = None) -> bool:
        if reservation_id in self.reservations:
            return True
        parts = media_key.split(':')
        if len(parts) not in (3, 4) or parts[1] != 'tmdb':
            return False
        try:
            tmdb_id = int(parts[2])
            if parts[0] == 'movie' and len(parts) == 3:
                return tmdb_id in self.movies
            if parts[0] == 'season' and len(parts) == 4:
                return (tmdb_id, int(parts[3])) in self.seasons
        except ValueError:
            pass
        return False
