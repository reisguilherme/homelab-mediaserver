"""Execute configured subtitle attempts without losing language priority."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from .subtitle_language import SubtitlePolicy


@dataclass(frozen=True)
class RecoveredSubtitle:
    language: str
    match_mode: str
    content: bytes


async def recover_subtitle(
    policy: SubtitlePolicy,
    fetch: Callable[[str, str], Awaitable[bytes | None]],
    *,
    already_present: Callable[[str], bool] | None = None,
) -> RecoveredSubtitle | None:
    for language, match_mode in policy.attempts:
        if already_present is not None and already_present(language):
            return RecoveredSubtitle(language, "present", b"")
        if "subdl" not in policy.providers:
            continue
        content = await fetch(language, match_mode)
        if content is not None:
            return RecoveredSubtitle(language, match_mode, content)
    return None
