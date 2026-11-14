"""Read explicit audio claims without treating subtitles or Arr defaults as tracks.

`audioEvidence` contains dictionaries with `source` and `languages`. Its presence
replaces legacy `languages`; discovery must use an empty list when an Arr response
only repeats the movie's original language. Older callers can additionally mark
that metadata with `audioLanguagesInferred=True`.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping
from html.parser import HTMLParser

from .indexers import release_indexer

_LANGUAGE_FIELDS = ("code", "isoCode", "iso6391", "iso6392", "languageCode", "name")
_ALIASES = {
    "english": "en", "eng": "en", "ingles": "en",
    "portuguese": "pt", "portugues": "pt", "por": "pt",
    "brazilian portuguese": "pt-br", "portuguese (brazil)": "pt-br",
    "portugues brasileiro": "pt-br", "portugues (brasil)": "pt-br",
    "portugues brasil": "pt-br", "portugues-br": "pt-br",
    "portuguesebr": "pt-br", "brazilian": "pt-br", "pob": "pt-br",
    "ptbr": "pt-br", "por-br": "pt-br",
    "european portuguese": "pt-pt", "portuguese (portugal)": "pt-pt",
    "portugues (portugal)": "pt-pt", "portugues europeu": "pt-pt",
    "french": "fr", "fra": "fr", "fre": "fr", "frances": "fr",
    "spanish": "es", "spa": "es", "espanhol": "es",
    "german": "de", "deu": "de", "ger": "de", "alemao": "de",
    "italian": "it", "ita": "it", "italiano": "it",
    "japanese": "ja", "jpn": "ja", "japones": "ja",
    "chinese": "zh", "zho": "zh", "chi": "zh", "chines": "zh",
    "korean": "ko", "kor": "ko", "coreano": "ko",
    "russian": "ru", "rus": "ru", "russo": "ru",
    "latvian": "lv", "lav": "lv",
}
_LABELS = sorted(
    {*_ALIASES, "pt-br", "pt-pt", "en", "pt", "fr", "es", "de", "it", "ja", "zh", "ko", "ru"},
    key=len, reverse=True,
)
_BRAZILIAN = re.compile(
    r"(?<![a-z0-9])(?:pt[ ._-]*br|por[ ._-]*br|"
    r"portuguese[ ._-]*(?:\([ ._-]*brazil[ ._-]*\)|"
    r"\[\s*brazilian\s*\]|brazil|br)|"
    r"brazilian[ ._-]+portuguese|portugues[ ._-]+(?:brasileiro|brasil)|"
    r"dublado[ ._-]+br)(?![a-z0-9])", re.I,
)
_SUBTITLE = re.compile(
    r"(?<![a-z0-9])(?:sub|subs|subtitle|subtitles|legenda|legendas|legendado)(?![a-z0-9])",
    re.I,
)
_AUDIO_LIST = re.compile(
    r"(?<![a-z0-9])(?:(?:dual|multi)[ ._-]*audio|audio[ ._-]*:)(?![a-z0-9])", re.I,
)
_ASC_AUDIO = re.compile(
    r"(?<![a-z0-9])(?:dual(?:[ ._-]*audio(?:[ ._-]+brazilian[ ._-]+original)?)?|"
    r"dublado brazilian)(?![a-z0-9])",
    re.I,
)
_ASC_RELEASE_CONTEXT = re.compile(
    r"(?<![a-z0-9])(?:(?:19|20)\d{2}|(?:480|576|720|1080|2160)[pi]?|"
    r"blu[ ._-]*ray|web[ ._-]*dl|bd[ ._-]*rip|br[ ._-]*rip|"
    r"s\d{1,2}e\d{1,3})(?![a-z0-9])", re.I,
)
_ASC_NACIONAL = re.compile(r"(?<![a-z0-9])nacional(?![a-z0-9])", re.I)
_ASC_QUALITY_CONTEXT = re.compile(
    r"(?<![a-z0-9])(?:(?:480|576|720|1080|2160)[pi]|blu[ ._-]*ray|"
    r"web[ ._-]*dl|bd[ ._-]*rip|br[ ._-]*rip)(?![a-z0-9])", re.I,
)
_SECTION = re.compile(
    r"^(audio|idiomas?|video|text|subtitles?(?:\(s\))?|subs?|legendas?|general|menu|"
    r"chapters?|sinopse|synopsis|source|fontes?)"
    r"(?:\s*#?\s*\d+)?\s*(?::\s*(.*)|\((.*)\)\s*:?)?$", re.I,
)
_COMMENTARY = re.compile(
    r"\b(?:commentary|comentarios?|audio[ -]description|descriptive[ -]audio)\b", re.I,
)
_AUDIO_SUMMARY = re.compile(
    r"^(?:opus|aac|e-?ac-?3|ac-?3|ddp?|dts(?:-hd)?|truehd|flac|mp3|pcm)\b"
    r"[\w .+-]*\s[-|]\s*\d+(?:\.\d+)?\s*(?:kbit/s|kb/s|kbps|mbps)\s[-|]\s*(.*)$",
    re.I,
)
_AUDIO_SUFFIX = re.compile(
    r"[\])}\s]*(?:[\[(](?:canadian|latin america|latam|castilian|us|uk|original|"
    r"dub|dublado|stereo|(?:opus|aac|e-?ac-?3|ac-?3|dts(?:-hd)?|truehd|flac|pcm)"
    r"(?:\s+\d(?:\.\d)?)?|\d(?:\.\d)?(?:\s*ch)?)[\])][\])}\s]*)*", re.I,
)


def _normalized(value: str) -> str:
    return "".join(
        character for character in unicodedata.normalize("NFKD", value)
        if not unicodedata.combining(character)
    ).lower()


def declared_language(value: object) -> str | None:
    """Normalize named languages and ISO tags, retaining explicit Portuguese region."""
    if isinstance(value, Mapping):
        languages = [declared_language(value.get(key)) for key in _LANGUAGE_FIELDS]
        if "pt-pt" in languages:
            return "pt-pt"  # Conflicting regions cannot establish Brazilian audio.
        if "pt-br" in languages:
            return "pt-br"
        return next((language for language in languages if language), None)
    if not isinstance(value, str):
        return None
    normalized = _normalized(value.strip()).replace("_", "-")
    normalized = _ALIASES.get(normalized, normalized)
    if normalized == "und" or not re.fullmatch(r"[a-z]{2,3}(?:-[a-z0-9]{2,8})*", normalized):
        return None
    return normalized


def _language_list(text: str, *, title: bool) -> tuple[set[str], int]:
    """Consume a bounded consecutive list, never words elsewhere in a movie title."""
    languages: set[str] = set()
    offset = 0
    while offset < len(text):
        separator = re.match(r"[\s._:+,/&|-]*", text[offset:])[0]
        offset += len(separator)
        # Opening brackets may wrap the list itself, but a later [SUBS ...] starts
        # a different block and must not extend an already consumed audio list.
        if not languages and text[offset:offset + 1] in ("[", "(", "{"):
            offset += 1
            continue
        remaining = text[offset:]
        brazilian = _BRAZILIAN.match(remaining)
        if brazilian:
            languages.add("pt-br")
            offset += brazilian.end()
            continue
        matched = next(
            ((label, match) for label in _LABELS
             if (match := re.match(re.escape(label) + r"(?![a-z0-9])", remaining, re.I))),
            None,
        )
        if matched is None:
            break
        label, match = matched
        following = remaining[match.end():].lstrip(" ._-")
        if (
            title and len(label) > 3 and following and following[0] not in "])[({0123456789"
            and not any(character in separator for character in "+,/&|")
            and not re.match(r"\s*[+/,;&|]", remaining[match.end():])
        ):
            # `English Teacher` may be a movie name, not an audio declaration.
            break
        if language := declared_language(label):
            languages.add(language)
        offset += match.end()
        if not title and (conjunction := re.match(r"\s+(?:e|and)\s+", text[offset:])):
            offset += conjunction.end()
    return languages, offset


def _subtitle_claim(title: str, start: int, end: int) -> bool:
    """Exclude regional labels in subtitle blocks, both `PTBR SUBS` and `SUBS PTBR`."""
    before, after = title[:start], title[end:]
    if re.match(r"[\s._:/+-]*" + _SUBTITLE.pattern, after, re.I):
        return True
    last_subtitle = list(_SUBTITLE.finditer(before))
    if not last_subtitle:
        return False
    between = before[last_subtitle[-1].end():]
    return not re.search(r"[\]\)}]|" + _AUDIO_LIST.pattern, between, re.I)


def title_audio_languages(title: object) -> set[str]:
    """Read explicit title tags while keeping subtitle-only claims out of audio."""
    if not isinstance(title, str):
        return set()
    title = _normalized(title)
    languages: set[str] = set()
    for audio in _AUDIO_LIST.finditer(title):
        claimed, _ = _language_list(title[audio.end():], title=True)
        languages |= claimed
    for match in _BRAZILIAN.finditer(title):
        if not _subtitle_claim(title, match.start(), match.end()):
            languages.add("pt-br")
    return languages


class _DescriptionText(HTMLParser):
    """Prefer indexers' description blocks over comments and related releases."""

    _VOID_TAGS = {
        "area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta",
        "param", "source", "track", "wbr",
    }

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.description_parts: list[str] | None = None
        self.description_depth: int | None = None
        self.tags: list[str] = []
        self.full_document = False
        self.hidden_depth = 0

    def _append(self, text):
        self.parts.append(text)
        if self.description_depth is not None:
            self.description_parts.append(text)

    def handle_starttag(self, tag, attrs):
        self.full_document |= tag in {"html", "body"}
        if tag not in self._VOID_TAGS:
            self.tags.append(tag)
        identifier = dict(attrs).get("id")
        if (
            not self.hidden_depth and self.description_parts is None
            and isinstance(identifier, str)
            and identifier.casefold() in {"description", "descrbody"}
        ):
            self.description_parts = []
            self.description_depth = len(self.tags)
        if tag in {"script", "style", "template"}:
            self.hidden_depth += 1
        elif not self.hidden_depth and tag in {
            "br", "p", "div", "pre", "li", "tr", "h1", "h2", "h3", "h4", "hr",
        }:
            self._append("\n")
        elif not self.hidden_depth and tag in {"td", "th"}:
            self._append(" ")

    def handle_endtag(self, tag):
        if tag in {"script", "style", "template"}:
            self.hidden_depth = max(0, self.hidden_depth - 1)
        elif not self.hidden_depth and tag in {
            "p", "div", "pre", "li", "tr", "h1", "h2", "h3", "h4",
        }:
            self._append("\n")
        if tag in self.tags:
            index = len(self.tags) - 1 - self.tags[::-1].index(tag)
            del self.tags[index:]
            if self.description_depth is not None and len(self.tags) < self.description_depth:
                self.description_depth = None

    def handle_data(self, data):
        if not self.hidden_depth:
            self._append(data)

    def handle_decl(self, decl):
        self.full_document |= decl.casefold().startswith("doctype")

    def text(self):
        if self.description_parts is not None:
            return "".join(self.description_parts)
        return "" if self.full_document else "".join(self.parts)


def _description_audio_list(value: str) -> set[str]:
    """Read language-first lists or a bounded codec/bitrate/language summary."""
    if _COMMENTARY.search(value) or _SUBTITLE.search(value):
        return set()
    summary = _AUDIO_SUMMARY.fullmatch(value)
    if summary:
        # Flag emoji decorate a language claim; they do not establish its region.
        value = re.sub(r"^[\U0001f1e6-\U0001f1ff\s]+", "", summary[1])
    languages, offset = _language_list(value, title=False)
    # Only bounded track qualifiers may follow a list, never parenthesized prose.
    if not _AUDIO_SUFFIX.fullmatch(value[offset:]):
        return set()
    return languages


def description_audio_languages(description: object) -> set[str]:
    """Read audio sections/MediaInfo; generic Portuguese stays region-unknown.

    Website descriptions are claims, not a verification of the downloaded file.
    Only labelled audio/idioma lists or language fields inside an Audio section
    count. Text/subtitle tracks, prose, script/style content and commentary do not.
    """
    if not isinstance(description, str) or len(description) > 1_000_000:
        return set()
    parser = _DescriptionText()
    parser.feed(description)
    text = parser.text()
    if len(text) > 512_000:
        return set()
    text = _normalized(text)
    languages: set[str] = set()
    block: set[str] = set()
    audio_section = False
    section_kind = None
    commentary = False
    brazilian_title = False
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        section = _SECTION.fullmatch(line)
        if section and section[1] in {"idioma", "idiomas"} and section_kind is not None:
            section = None  # Within an existing track this is a field, not a new audio section.
        if section:
            if audio_section and not commentary:
                languages |= block
            block = set()
            commentary = False
            brazilian_title = False
            section_kind = section[1]
            audio_section = section[1] in {"audio", "idioma", "idiomas"}
            if audio_section:
                value = section[2] or section[3] or ""
                block = _description_audio_list(value)
                commentary = bool(_COMMENTARY.search(value))
            continue
        if not audio_section:
            continue
        field = re.match(r"^(language|idiomas?|title|titulo)\s*:\s*(.*)$", line)
        value = field[2] if field else line
        if field and field[1] in {"title", "titulo"}:
            commentary |= bool(_COMMENTARY.search(value))
            brazilian_title |= bool(
                _BRAZILIAN.match(value)
                or re.match(r"^(?:brazilian|brasileiro)\s+(?:dub|dublado)\b", value)
            )
        if not field or field[1] in {"language", "idioma", "idiomas"}:
            block |= _description_audio_list(value)
        if "pt" in block and brazilian_title and "pt-pt" not in block:
            block.discard("pt")
            block.add("pt-br")
    if audio_section and not commentary:
        languages |= block
    return languages


def _release_audio_claims(release: Mapping[str, object]) -> tuple[set[str], set[str] | None]:
    values: list[object] = []
    observed: list[object] | None = None
    if "audioEvidence" in release:
        evidence = release["audioEvidence"]
        if isinstance(evidence, (list, tuple)):
            for item in evidence:
                if not isinstance(item, Mapping) or item.get("source") not in {
                    "arr-parser", "description", "ffprobe", "title",
                }:
                    continue
                declared = item.get("languages")
                if isinstance(declared, (list, tuple)):
                    values.extend(declared)
                    if item.get("source") == "ffprobe":
                        if observed is None:
                            observed = []
                        observed.extend(declared)
    elif not release.get("audioLanguagesInferred"):
        declared = release.get("languages")
        if isinstance(declared, (list, tuple)):
            values.extend(declared)
    return (
        {declared_language(value) for value in values} - {None},
        {declared_language(value) for value in observed} - {None} if observed is not None else None,
    )


def qualified_title_audio_languages(
    release: Mapping[str, object], *, original_language: object = None,
) -> set[str]:
    """Trust ASC Dual labels as Brazilian plus known original audio claims."""
    title = release.get("title")
    languages = title_audio_languages(title)
    claimed, observed = _release_audio_claims(release)
    if release_indexer(release) == "amigosshare":
        if observed is not None:
            return set()  # Observed tracks outrank the indexer's title declaration.
        if isinstance(title, str):
            title = _normalized(title)
            for match in _ASC_AUDIO.finditer(title):
                before, after = title[:match.start()], title[match.end():]
                if match[0] == "dual" and not (
                    _ASC_RELEASE_CONTEXT.search(before)
                    or before.rstrip().endswith("[") and after.lstrip().startswith("]")
                ):
                    continue
                if _subtitle_claim(title, match.start(), match.end()):
                    continue
                languages.add("pt-br")
                if match[0].startswith("dual"):
                    if original := declared_language(original_language):
                        languages.add(original)
            if declared_language(original_language) == "pt":
                for match in _ASC_NACIONAL.finditer(title):
                    if (_ASC_QUALITY_CONTEXT.search(title[:match.start()])
                            and not _subtitle_claim(title, match.start(), match.end())):
                        # ASC's Nacional release label claims the Portuguese
                        # original. It proves no Brazilian region or dual track.
                        languages.add("pt")
    if "pt-pt" in claimed:
        languages.discard("pt-br")
    return languages


def release_audio_languages(
    release: Mapping[str, object], *, original_language: object = None,
) -> set[str]:
    """Combine qualified evidence with title tags, preserving legacy callers."""
    languages, observed = _release_audio_claims(release)
    if observed is not None and release_indexer(release) == "amigosshare":
        return observed
    title_languages = qualified_title_audio_languages(release, original_language=original_language)
    if "pt-pt" in languages:
        title_languages.discard("pt-br")
    return languages | title_languages


def preferred_audio_tier(
    languages: set[str], original_language: object,
    preferences: tuple[str, ...] = ("dual", "pt-BR", "original"),
) -> str | None:
    """Classify a declared or observed soundtrack using the same profile rules."""
    original = declared_language(original_language)
    original_tracks = {
        language for language in languages
        if original is not None and (
            language == original or "-" not in original and language.startswith(original + "-")
        )
    }
    has_brazilian = "pt-br" in languages
    distinct_original = original not in {None, "pt", "por", "pt-br"} and any(
        language != "pt-br" for language in original_tracks
    )
    matches = {
        "dual": has_brazilian and distinct_original,
        "pt-BR": has_brazilian,
        "original": bool(original_tracks),
    }
    return next((preference for preference in preferences if matches.get(preference)), None)
