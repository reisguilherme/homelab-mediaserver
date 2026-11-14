from homeserver_common.env import load_settings
from homeserver_common.render import render_stack


def test_jellyfin_presentation_defaults_and_literal_labels(tmp_path):
    path = tmp_path / '.env'
    path.write_text('', encoding='utf-8')
    env = load_settings(path).as_environment()
    assert env['HOMESERVER_JELLYFIN_SUBTITLE_LABELS_ENABLED'] == 'true'
    assert env['HOMESERVER_JELLYFIN_SUBTITLE_LABEL_EN_US'] == 'English - US'
    assert env['HOMESERVER_JELLYFIN_SUBTITLE_LABEL_PT_BR'] == 'Português BR'
    assert env['HOMESERVER_JELLYFIN_COLLECTION_COVERS_ENABLED'] == 'true'
    assert env['HOMESERVER_JELLYFIN_COLLECTION_COVERS_INTERVAL_SECONDS'] == '300'
    path.write_text(
        'HOMESERVER_JELLYFIN_SUBTITLE_LABEL_PT_BR="Português do Brasil"\n'
        'HOMESERVER_JELLYFIN_SUBTITLE_LABELS_ENABLED=false\n'
        'HOMESERVER_JELLYFIN_COLLECTION_COVERS_ENABLED=false\n'
        'HOMESERVER_JELLYFIN_COLLECTION_COVERS_INTERVAL_SECONDS=600\n',
        encoding='utf-8',
    )
    env = load_settings(path).as_environment()
    assert env['HOMESERVER_JELLYFIN_SUBTITLE_LABEL_PT_BR'] == 'Português do Brasil'
    assert env['HOMESERVER_JELLYFIN_SUBTITLE_LABELS_ENABLED'] == 'false'
    assert env['HOMESERVER_JELLYFIN_COLLECTION_COVERS_ENABLED'] == 'false'
    assert env['HOMESERVER_JELLYFIN_COLLECTION_COVERS_INTERVAL_SECONDS'] == '600'


def test_control_api_query_tokens_are_not_access_logged(tmp_path):
    path = tmp_path / '.env'
    path.write_text('', encoding='utf-8')
    services = render_stack(load_settings(path))['services']
    assert '--no-access-log' in services['control-api']['command']
    # The renderer continues to pass preferences to the processes that consume them.
    for name in ('control-api', 'control-worker'):
        assert services[name]['environment'][
            'HOMESERVER_JELLYFIN_COLLECTION_COVERS_ENABLED'
        ] == 'true'
