"""Presentation labels; native stream identity and language remain untouched."""

from __future__ import annotations

import re
import unicodedata
from collections import defaultdict
from pathlib import PurePosixPath
from typing import Any

_ENGLISH = {'en', 'eng', 'english'}
_PORTUGUESE = {'pt', 'por', 'portuguese', 'portugues', 'pob', 'pop', 'ptbr'}


def _plain(value: object) -> str:
    if not isinstance(value, str):
        return ''
    return unicodedata.normalize('NFKD', value).encode('ascii', 'ignore').decode().casefold()


def _filename_metadata(stream: dict[str, Any]) -> str:
    """Use language suffixes, never country words in a movie/directory name."""
    stem = PurePosixPath(str(stream.get('Path') or '').replace('\\', '/')).stem
    match = re.search(
        r'(?:^|\.)(?:pt-br|pt-pt|ptbr|pob|pop|por|pt|en(?:[-_][a-z]{2})?|eng|english)'
        r'(?=\.|$)', stem, re.IGNORECASE,
    )
    return stem[match.start():] if match else ''


def _language_label(stream: dict[str, Any], english_us: str, portuguese_br: str) -> str | None:
    language = _plain(stream.get('Language')).replace('_', '-')
    title = _plain(stream.get('Title'))
    filename = _plain(_filename_metadata(stream))
    primary = language.split('-')[0]
    # Unknown/other native languages keep their existing label and evidence.
    if primary in _ENGLISH:
        region = language.split('-', 1)[1].upper() if '-' in language else None
        if region is None:
            match = re.search(r'\b(?:english|en|eng)\s*[-_( .]+(us|uk|gb|au|ca|nz)\b',
                              title + ' ' + filename)
            if match:
                region = match.group(1).upper()
        if region is None:
            match = re.fullmatch(r'\s*(us|uk|gb|au|ca|nz)\s*', title)
            if match:
                region = match.group(1).upper()
        if region is None:
            for description, country in (
                ('british|united kingdom', 'GB'), ('canadian|canada', 'CA'),
                ('australian|australia', 'AU'), ('american|united states', 'US'),
            ):
                if re.search(r'\b(?:' + description + r')\b', title):
                    region = country
                    break
        region = 'GB' if region == 'UK' else region
        return english_us if region in (None, 'US') else f'English - {region}'
    if primary in _PORTUGUESE:
        evidence = title + ' ' + filename
        if language in {'pob', 'ptbr', 'pt-br'}:
            return portuguese_br
        if language in {'pop', 'pt-pt'}:
            return 'Português PT'
        if '-' in language:
            return 'Português ' + language.split('-', 1)[1].upper()
        if re.search(r'\bpt[-_. ]pt\b|\bportugal\b', evidence):
            return 'Português PT'
        if re.search(r'\b(?:pob|ptbr|pt[-_. ]br|brazilian|brazil|brasil|brasileir[oa])\b',
                     evidence):
            return portuguese_br
        return 'Português'
    return None


def _subtitle_label(stream: dict[str, Any], english_us: str, portuguese_br: str) -> str | None:
    base = _language_label(stream, english_us, portuguese_br)
    if base is None:
        return None
    title = str(stream.get('Title') or '').strip()
    evidence = _plain(title + ' ' + _filename_metadata(stream))
    forced = stream.get('IsForced') is True or bool(
        re.search(r'\b(?:forced|foreign|forcad[oa])\b', evidence)
    )
    hearing = stream.get('IsHearingImpaired') is True or bool(
        re.search(r'\b(?:sdh|cc|hi|hearing impaired|deficiencia auditiva)\b', evidence)
    )
    parts = [base]
    if forced:
        parts.append('Forçada')
    if hearing:
        parts.append('SDH')
    if stream.get('IsDefault') is True:
        parts.append('Padrão')
    # Keep distinct tracks such as commentary, signs/songs and named sources.
    special = bool(re.search(r'\b(?:commentary|comments?|signs?|songs?|lyrics|partial)\b',
                             _plain(title)))
    if special:
        detail = title
    else:
        detail = re.sub(
            r'\b(?:English|Portuguese|Português|Portugues|Brazilian|Brazil|Brasil|Portugal|'
            r'Brasileir[oa]|British|United Kingdom|Canadian|Canada|Australian|Australia|'
            r'American|United States|US|UK|GB|AU|CA|NZ|pt[-_]BR|pt[-_]PT|en[-_][A-Z]{2}|'
            r'pob|por|eng|SDH|CC|HI|forced(?:\s+narrative)?|foreign|Forçad[oa]|'
            r'hearing\s+impaired|default|Padrão)\b', '', title, flags=re.IGNORECASE,
        )
        detail = re.sub(r'[()\[\]]', '', detail)
        detail = re.sub(r'(?:\s+-\s*){2,}', ' - ', detail).strip(' .-_')
    if detail and _plain(detail) not in {_plain(part) for part in parts}:
        parts.append(detail)
    return ' - '.join(parts)


def _distinct_labels(streams: list[Any], english_us: str, portuguese_br: str) -> dict[int, str]:
    labels = {}
    for position, stream in enumerate(streams):
        if isinstance(stream, dict) and stream.get('Type') == 'Subtitle':
            label = _subtitle_label(stream, english_us, portuguese_br)
            if label is not None:
                labels[position] = label
    # Expand only the labels that still collide after each discriminator.
    for discriminator in ('format', 'origin', 'index'):
        groups: dict[str, list[int]] = defaultdict(list)
        for position, label in labels.items():
            groups[label].append(position)
        for positions in groups.values():
            if len(positions) < 2:
                continue
            for position in positions:
                stream = streams[position]
                if discriminator == 'format':
                    suffix = str(stream.get('Codec') or PurePosixPath(
                        str(stream.get('Path') or '')
                    ).suffix.lstrip('.') or 'Legenda').upper()
                elif discriminator == 'origin':
                    suffix = 'Externo' if stream.get('IsExternal') is True else 'Interno'
                else:
                    index = stream.get('Index')
                    suffix = f'Faixa {index if index is not None else position}'
                labels[position] += ' - ' + suffix
    return labels


def normalize_subtitle_labels(
    payload: Any, *, english_us_label: str = 'English - US',
    portuguese_br_label: str = 'Português BR',
) -> bool:
    """Mutate only DisplayTitle inside MediaStreams arrays; report a real change."""
    changed = False
    pending = [payload]
    while pending:
        value = pending.pop()
        if isinstance(value, list):
            pending.extend(value)
        elif isinstance(value, dict):
            streams = value.get('MediaStreams')
            if isinstance(streams, list):
                labels = _distinct_labels(streams, english_us_label, portuguese_br_label)
                for position, label in labels.items():
                    stream = streams[position]
                    if label != stream.get('DisplayTitle'):
                        stream['DisplayTitle'] = label
                        changed = True
            pending.extend(value.values())
    return changed
