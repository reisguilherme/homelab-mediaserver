from __future__ import annotations

from copy import deepcopy

import pytest


@pytest.mark.parametrize(
    ('fields', 'expected'),
    [
        ({'Language': 'eng'}, 'English - US'),
        ({'Language': 'en-US'}, 'English - US'),
        ({'Language': 'eng', 'Title': 'English (UK)'}, 'English - GB'),
        ({'Language': 'en-AU'}, 'English - AU'),
        ({'Language': 'eng', 'Path': '/data/Movie.en-CA.srt'}, 'English - CA'),
        ({'Language': 'eng', 'Path': '/data/GB.Movie.en.srt'}, 'English - US'),
        ({'Language': 'pob'}, 'Português BR'),
        ({'Language': 'por', 'Title': 'Portuguese (Brazilian)'}, 'Português BR'),
        ({'Language': 'por', 'Path': '/data/Movie.pt-BR.srt'}, 'Português BR'),
        ({'Language': 'por'}, 'Português'),
        ({'Language': 'pop'}, 'Português PT'),
        ({'Language': 'pt-BR', 'Title': 'Portuguese (Portugal)'}, 'Português BR'),
        ({'Language': 'en-GB', 'Title': 'English US'}, 'English - GB'),
        ({'Language': 'eng', 'Title': 'English (British)'}, 'English - GB'),
        ({'Language': 'eng', 'Title': 'English (Canadian)'}, 'English - CA'),
        ({'Language': 'eng', 'Title': 'English (Australian)'}, 'English - AU'),
        ({'Language': 'eng', 'Title': 'English HI'}, 'English - US - SDH'),
        ({'Language': 'por', 'Title': 'Portuguese (Portugal)'}, 'Português PT'),
        ({'Language': 'pob', 'IsForced': True}, 'Português BR - Forçada'),
        ({'Language': 'eng', 'Title': 'SDH'}, 'English - US - SDH'),
        ({'Language': 'eng', 'IsHearingImpaired': True}, 'English - US - SDH'),
        ({'Language': 'pob', 'Path': '/data/Movie.pt-BR.forced.sdh.srt'},
         'Português BR - Forçada - SDH'),
        ({'Language': 'eng', 'Title': 'Signs & Songs'}, 'English - US - Signs & Songs'),
        ({'Language': 'eng', 'Title': 'Director Commentary'},
         'English - US - Director Commentary'),
        ({'Language': 'eng', 'Title': 'Forced Narrative'}, 'English - US - Forçada'),
        ({'Language': 'eng', 'Title': 'English - SDH - WEB-DL'},
         'English - US - SDH - WEB-DL'),
        ({'Language': 'eng', 'Title': 'English (US)', 'IsDefault': True},
         'English - US - Padrão'),
        ({'Language': 'spa', 'Title': 'Latin American'},
         'Latin American - Spanish - SUBRIP'),
        ({'Language': 'und'}, 'Native original - SUBRIP'),
    ],
)
def test_subtitle_display_preserves_identified_regions_and_track_roles(fields, expected):
    from homeserver_control.domain.jellyfin_labels import normalize_subtitle_labels

    stream = {'Type': 'Subtitle', 'Index': 4, 'Codec': 'subrip',
              'DisplayTitle': 'Native original - SUBRIP', **fields}
    if fields.get('Language') == 'spa':
        stream['DisplayTitle'] = 'Latin American - Spanish - SUBRIP'
    before = deepcopy(stream)
    payload = {'MediaSources': [{'MediaStreams': [stream]}]}
    normalize_subtitle_labels(payload)
    assert stream['DisplayTitle'] == expected
    assert {k: v for k, v in stream.items() if k != 'DisplayTitle'} == {
        k: v for k, v in before.items() if k != 'DisplayTitle'
    }


def test_normalization_is_idempotent_and_keeps_audio_and_arbitrary_objects():
    from homeserver_control.domain.jellyfin_labels import normalize_subtitle_labels

    audio = {'Type': 'Audio', 'Language': 'eng', 'DisplayTitle': 'AAC - Stereo'}
    arbitrary = {'Type': 'Subtitle', 'DisplayTitle': 'ordinary metadata'}
    payload = {'Items': [{'MediaStreams': [audio, {'Type': 'Subtitle', 'Language': 'pob',
                'DisplayTitle': 'Pob - SUBRIP - Externo'}]}], 'Other': arbitrary}
    assert normalize_subtitle_labels(payload) is True
    expected = deepcopy(payload)
    assert normalize_subtitle_labels(payload) is False
    assert payload == expected
    assert audio['DisplayTitle'] == 'AAC - Stereo'
    assert arbitrary['DisplayTitle'] == 'ordinary metadata'


def test_labels_are_configurable_and_embedded_title_overrides_are_conservative():
    from homeserver_control.domain.jellyfin_labels import normalize_subtitle_labels

    payload = {'NowPlayingItem': {'MediaStreams': [
        {'Type': 'Subtitle', 'Language': 'eng', 'DisplayTitle': 'English'},
        {'Type': 'Subtitle', 'Language': 'pob', 'DisplayTitle': 'Pob'},
        {'Type': 'Subtitle', 'Language': 'jpn', 'Title': 'English signs',
         'DisplayTitle': 'English signs - Japanese - ASS'},
    ]}}
    normalize_subtitle_labels(payload, english_us_label='English USA',
                              portuguese_br_label='PT Brasil')
    assert [s['DisplayTitle'] for s in payload['NowPlayingItem']['MediaStreams']] == [
        'English USA', 'PT Brasil', 'English signs - Japanese - ASS',
    ]


@pytest.mark.parametrize(('tracks', 'expected'), [
    ([{'Index': 3, 'Codec': 'subrip'}, {'Index': 4, 'Codec': 'PGSSUB'}],
     ['English - US - SUBRIP', 'English - US - PGSSUB']),
    ([{'Index': 3, 'Codec': 'subrip', 'IsExternal': False},
      {'Index': 4, 'Codec': 'subrip', 'IsExternal': True}],
     ['English - US - SUBRIP - Interno', 'English - US - SUBRIP - Externo']),
    ([{'Index': 3, 'Codec': 'subrip'}, {'Index': 4, 'Codec': 'subrip'}],
     ['English - US - SUBRIP - Interno - Faixa 3',
      'English - US - SUBRIP - Interno - Faixa 4']),
])
def test_colliding_track_labels_add_only_the_metadata_needed_to_distinguish(tracks, expected):
    from homeserver_control.domain.jellyfin_labels import normalize_subtitle_labels

    streams = [{'Type': 'Subtitle', 'Language': 'eng', 'DisplayTitle': 'native', **track}
               for track in tracks]
    before = deepcopy(streams)
    payload = {'MediaStreams': streams}
    assert normalize_subtitle_labels(payload)
    assert [s['DisplayTitle'] for s in streams] == expected
    assert [{k: v for k, v in s.items() if k != 'DisplayTitle'} for s in streams] == [
        {k: v for k, v in s.items() if k != 'DisplayTitle'} for s in before
    ]
    assert normalize_subtitle_labels(payload) is False


def test_collisions_are_local_to_a_media_source_and_do_not_expand_distinct_roles():
    from homeserver_control.domain.jellyfin_labels import normalize_subtitle_labels

    payload = {'MediaSources': [
        {'MediaStreams': [{'Type': 'Subtitle', 'Language': 'eng', 'Codec': 'subrip'},
                          {'Type': 'Subtitle', 'Language': 'eng', 'Codec': 'subrip',
                           'IsHearingImpaired': True}]},
        {'MediaStreams': [{'Type': 'Subtitle', 'Language': 'eng', 'Codec': 'subrip'}]},
    ]}
    normalize_subtitle_labels(payload)
    assert [s['DisplayTitle'] for source in payload['MediaSources']
            for s in source['MediaStreams']] == [
        'English - US', 'English - US - SDH', 'English - US',
    ]
