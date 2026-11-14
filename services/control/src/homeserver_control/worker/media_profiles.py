"""Identify opt-in media preferences from explicit native catalog metadata."""

import unicodedata
from collections.abc import Mapping


def is_animation(media: object) -> bool:
    """Require an animation genre, never infer it from a title or series type."""
    if not isinstance(media, Mapping):
        return False
    genres = media.get("genres")
    if not isinstance(genres, (list, tuple)):
        return False
    for genre in genres:
        if isinstance(genre, Mapping):
            if type(genre.get("id")) is int and genre["id"] == 16:
                return True
            genre = genre.get("name")
        if isinstance(genre, str):
            plain = unicodedata.normalize("NFKD", genre).encode("ascii", "ignore").decode()
            if plain.strip().casefold() in {"animation", "animacao", "anime"}:
                return True
    return False
