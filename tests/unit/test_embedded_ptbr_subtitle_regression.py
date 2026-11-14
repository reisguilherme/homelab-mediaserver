"""Brazilian embedded subtitles satisfy the same strict language requirement as sidecars."""
import pytest

from homeserver_control.domain.media_probe import MediaProbe
from homeserver_control.worker import subtitle_language


@pytest.mark.parametrize(
    'tags,disposition,codec,expected',
    [
        ({'language': 'por', 'title': 'Brazilian Portuguese'}, {'forced': 0}, 'subrip', True),
        ({'language': 'pt-BR'}, {'forced': 0}, 'ass', True),
        ({'LANGUAGE': 'pob', 'TITLE': 'SDH'}, {}, 'subrip', True),
        ({'language': 'por'}, {}, 'subrip', False),
        ({'language': 'pt-PT', 'title': 'Brazilian Portuguese'}, {}, 'subrip', False),
        ({'language': 'eng', 'title': 'Brazilian Portuguese'}, {}, 'subrip', False),
        ({'language': 'und', 'title': 'Brazilian Portuguese'}, {}, 'subrip', False),
        ({'language': 'por', 'title': 'Brazilian Portuguese [Forced]'}, {}, 'subrip', False),
        ({'language': 'por', 'title': 'Brazilian Portuguese'}, {'forced': 1}, 'subrip', False),
        ({'language': 'pt-BR', 'title': 'Commentary'}, {}, 'subrip', False),
        ({'language': 'pt-BR'}, {'comment': 1}, 'subrip', False),
        ({'language': 'pt-BR', 'title': 'Signs & Songs'}, {}, 'subrip', False),
        ({'language': 'pt-BR', 'title': 'European Portuguese'}, {}, 'subrip', False),
        ({'language': 'pt-BR'}, {}, 'hdmv_pgs_subtitle', False),
    ],
)
def test_embedded_ptbr_requires_explicit_region_and_full_text(tags, disposition, codec, expected):
    stream = {
        'codec_type': 'subtitle', 'codec_name': codec, 'tags': tags, 'disposition': disposition,
    }
    probe = MediaProbe(1920, 1080, ('eng',), ('por',), {'streams': [stream]})
    assert subtitle_language.has_embedded_brazilian_subtitle(probe) is expected


def test_forced_brazilian_track_does_not_hide_the_full_track():
    probe = MediaProbe(3840, 2160, ('por', 'eng'), ('por', 'por'), {'streams': [
        {'codec_type': 'subtitle', 'codec_name': 'subrip',
         'tags': {'language': 'por', 'title': 'Brazilian Portuguese [Forced]'},
         'disposition': {'forced': 1}},
        {'codec_type': 'subtitle', 'codec_name': 'subrip',
         'tags': {'language': 'por', 'title': 'Brazilian Portuguese'},
         'disposition': {'forced': 0}},
    ]})
    assert subtitle_language.has_embedded_brazilian_subtitle(probe)


@pytest.mark.parametrize('language,title', [
    ('por', 'Brazilian'),
    ('pt', 'Brazil'),
    ('portuguese', 'Brasil'),
    ('por', 'Brasileiro'),
    ('por', 'Brasileira'),
    ('por', 'Brazilian [SDH]'),
    ('por', 'Brazilian CC'),
])
def test_portuguese_language_tag_and_standalone_brazilian_region_form_one_identity(language, title):
    # Native S04E01 evidence: por + Brazilian, subrip, not forced/commentary.
    stream = {'codec_type': 'subtitle', 'codec_name': 'subrip',
              'tags': {'language': language, 'title': title},
              'disposition': {'forced': 0, 'comment': 0}}
    probe = MediaProbe(1920, 804, ('eng',), (language,), {'streams': [stream]})
    assert subtitle_language.has_embedded_brazilian_subtitle(probe)


@pytest.mark.parametrize('language,title,disposition,codec', [
    ('por', 'Iberian', {}, 'subrip'),
    ('por', 'European', {}, 'subrip'),
    ('por', 'Brazilian / Iberian', {}, 'subrip'),
    ('por', 'Not Brazilian', {}, 'subrip'),
    ('por', 'Brazilian commentary', {}, 'subrip'),
    ('por', 'Brazilian forced', {}, 'subrip'),
    ('por', 'Brazilian signs and songs', {}, 'subrip'),
    ('por', 'Brazilian partial', {}, 'subrip'),
    ('por', 'Brazilian', {'forced': 1}, 'subrip'),
    ('por', 'Brazilian', {'comment': 1}, 'subrip'),
    ('por', 'Brazilian', {}, 'hdmv_pgs_subtitle'),
    ('pt-PT', 'Brazilian', {}, 'subrip'),
    ('und', 'Brazilian', {}, 'subrip'),
    ('eng', 'Brazilian', {}, 'subrip'),
])
def test_standalone_region_does_not_accept_conflicting_or_incomplete_subtitles(
    language, title, disposition, codec,
):
    stream = {'codec_type': 'subtitle', 'codec_name': codec,
              'tags': {'language': language, 'title': title}, 'disposition': disposition}
    probe = MediaProbe(1920, 804, ('eng',), (language,), {'streams': [stream]})
    assert not subtitle_language.has_embedded_brazilian_subtitle(probe)


@pytest.mark.parametrize('title', ['Brazilian', 'Brazil', 'Brasil'])
def test_subtitle_region_fix_does_not_expand_original_audio_exemption(title):
    stream = {'codec_type': 'audio', 'tags': {'language': 'por', 'title': title},
              'disposition': {'original': 1}}
    probe = MediaProbe(1920, 804, ('por',), (), {'streams': [stream]})
    assert not subtitle_language.audio_is_brazilian_portuguese(probe)
