from __future__ import annotations

from dataclasses import dataclass

from homeserver_control.adapters.subtitles import SubtitleAdapter


@dataclass(frozen=True)
class SubtitleDecision:
    state: str
    job_id: str | None = None


async def request_original_subtitle(
    adapter: SubtitleAdapter,
    *,
    media_key: str,
    language: str,
    already_present: bool,
) -> SubtitleDecision:
    if already_present:
        return SubtitleDecision(state="available")
    job_id = await adapter.request_missing(media_key=media_key, language=language)
    return SubtitleDecision(state="waiting_subtitles", job_id=job_id)
