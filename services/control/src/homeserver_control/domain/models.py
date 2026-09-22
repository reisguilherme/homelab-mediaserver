from dataclasses import dataclass


@dataclass(frozen=True)
class EpisodeFile:
    episode_key: str
    path: str
    size_bytes: int


@dataclass(frozen=True)
class ReleaseRef:
    release_id: str
    resolution: int
    language: str
    files: tuple[EpisodeFile, ...]


@dataclass(frozen=True)
class SelectionPlan:
    files: tuple[EpisodeFile, ...]
    total_bytes: int
    reason: str = "selected"
