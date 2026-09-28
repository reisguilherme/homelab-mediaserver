from dataclasses import dataclass, field
from typing import Any

SECRET_WORDS = (
    "password",
    "adminpass",
    "username",
    "apikey",
    "api_key",
    "token",
    "secret",
    "credential",
    "account",
    # Native provider definitions carry arbitrary credential field names/URLs.
    "fields",
    "cookie",
    "passkey",
    "authorization",
)


def environment(settings) -> dict[str, str]:
    return dict(settings if isinstance(settings, dict) else settings.as_environment())


def is_secret(key: str) -> bool:
    return any(word in key.lower() for word in SECRET_WORDS)


def redact(value):
    if isinstance(value, dict):
        return {
            key: "[redacted]" if is_secret(key) else redact(item) for key, item in value.items()
        }
    if isinstance(value, list):
        return [
            ("[redacted]" if is_secret(str(item.get("name", ""))) else redact(item))
            if isinstance(item, dict)
            else redact(item)
            for item in value
        ]
    return value


@dataclass(frozen=True)
class SettingChange:
    service: str
    key: str
    before: Any
    after: Any
    restart_required: bool = False
    secret: bool = False


def plan_settings(service: str, current: dict, desired: dict) -> list[SettingChange]:
    return [
        SettingChange(
            service,
            key,
            "[redacted]" if is_secret(key) else redact(current.get(key)),
            "[redacted]" if is_secret(key) else redact(value),
            secret=is_secret(key),
        )
        for key, value in desired.items()
        if current.get(key) != value
    ]


@dataclass
class ServiceOutcome:
    service: str
    status: str
    changes: list[SettingChange] = field(default_factory=list)
    message: str = ""


class NativeConfigurationError(RuntimeError):
    """Safe error: never embed URLs, response bodies or exception messages."""
