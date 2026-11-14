"""Shared download profile identities and visual categories, best first."""

from types import MappingProxyType

PROFILE_NAMES = ("Max Quality", "Standard", "Dublado")
DEFAULT_PROFILE_NAME = "Standard"
PROFILE_AUDIO_PREFERENCES = MappingProxyType({
    "Max Quality": ("original", "dual"),
    "Standard": ("original", "dual"),
    "Dublado": ("dual", "original"),
})
PROFILE_QUALITY_TIERS = MappingProxyType({
    "Max Quality": (
        (2160, ("remux",)), (2160, ("bluray",)),
        (2160, ("webdl", "bdrip", "brrip")),
        (1080, ("remux",)), (1080, ("bluray",)),
        (1080, ("webdl", "bdrip", "brrip")),
    ),
    "Standard": ((1080, ("bluray",)), (1080, ("webdl", "bdrip", "brrip"))),
    "Dublado": ((1080, ("bluray",)), (1080, ("webdl", "bdrip", "brrip"))),
})
# BDRip/BRRip are title distinctions within native Bluray quality, not native IDs.
PROFILE_QUALITIES = MappingProxyType({
    "Max Quality": (
        (2160, "remux"), (2160, "bluray"), (2160, "webdl"),
        (1080, "remux"), (1080, "bluray"), (1080, "webdl"),
    ),
    "Standard": ((1080, "bluray"), (1080, "webdl")),
    "Dublado": ((1080, "bluray"), (1080, "webdl")),
})
